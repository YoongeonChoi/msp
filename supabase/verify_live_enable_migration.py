from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "supabase" / "migrations"
SEED = ROOT / "supabase" / "seed.sql"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify live enable migration behavior in a disposable Postgres container.",
    )
    parser.add_argument("--image", default="postgres:16-alpine")
    parser.add_argument("--keep-container", action="store_true")
    parser.add_argument("--timeout-sec", type=int, default=60)
    args = parser.parse_args()

    if shutil.which("docker") is None:
        print("FINAL=SKIP docker_cli_unavailable")
        print("Docker CLI is not installed. Install Docker and rerun this verifier.")
        return 2
    if not _docker_ready():
        print("FINAL=SKIP docker_daemon_unavailable")
        print(
            "Docker CLI exists but the daemon is not reachable. "
            "Start Docker and rerun this verifier."
        )
        return 2

    container = f"msp-live-migration-{uuid4().hex[:12]}"
    try:
        _run(
            [
                "docker",
                "run",
                "--rm",
                "-d",
                "--name",
                container,
                "-e",
                "POSTGRES_PASSWORD=postgres",
                args.image,
            ],
        )
        _wait_for_postgres(container, args.timeout_sec)
        _psql(container, _supabase_stub_sql())
        for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
            _psql(container, migration.read_text(encoding="utf-8"), label=migration.name)
        _psql(container, SEED.read_text(encoding="utf-8"), label=SEED.name)
        _psql(container, _live_enable_once_sql(), label="live_enable_once_probe")
        _psql(container, _deployment_lock_sql(), label="deployment_lock_probe")
        _verify_security_definer_rpc_grants(container)
    finally:
        if args.keep_container:
            print(f"Container kept for inspection: {container}")
        else:
            _run(["docker", "rm", "-f", container], check=False)

    print("FINAL=PASS live_enable_consumed_once rpc_hardening")
    return 0


def _docker_ready() -> bool:
    try:
        result = _run(["docker", "info", "--format", "{{.ServerVersion}}"], check=False)
    except OSError:
        return False
    return result.returncode == 0


def _wait_for_postgres(container: str, timeout_sec: int) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        ready = _run(
            ["docker", "exec", container, "pg_isready", "-U", "postgres"],
            check=False,
        )
        if ready.returncode == 0:
            connection = _run(
                [
                    "docker",
                    "exec",
                    container,
                    "psql",
                    "-U",
                    "postgres",
                    "-d",
                    "postgres",
                    "-c",
                    "select 1;",
                ],
                check=False,
            )
            if connection.returncode == 0:
                return
        time.sleep(1)
    raise RuntimeError("postgres_container_not_ready")


def _psql(container: str, sql: str, label: str = "sql") -> None:
    result = _run(
        [
            "docker",
            "exec",
            "-i",
            container,
            "psql",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
        ],
        input_text=sql,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"{label} failed:\n{result.stdout}\n{result.stderr}")


def _psql_expect_failure(
    container: str,
    sql: str,
    *,
    label: str,
    required_fragments: tuple[str, ...],
) -> None:
    result = _run(
        [
            "docker",
            "exec",
            "-i",
            container,
            "psql",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
        ],
        input_text=sql,
        check=False,
    )
    if result.returncode == 0:
        raise RuntimeError(f"{label} unexpectedly succeeded:\n{result.stdout}")
    output = f"{result.stdout}\n{result.stderr}".lower()
    missing = [fragment for fragment in required_fragments if fragment not in output]
    if missing:
        raise RuntimeError(
            f"{label} failed for an unexpected reason; missing {missing}:\n"
            f"{result.stdout}\n{result.stderr}",
        )


def _verify_security_definer_rpc_grants(container: str) -> None:
    denied_checks = [
        (
            "anon_run_retention_cleanup_denied",
            "set role anon;\nselect public.run_retention_cleanup(true);\n",
            ("permission denied", "run_retention_cleanup"),
        ),
        (
            "authenticated_run_retention_cleanup_denied",
            "set role authenticated;\nselect public.run_retention_cleanup(true);\n",
            ("permission denied", "run_retention_cleanup"),
        ),
        (
            "anon_database_size_denied",
            "set role anon;\nselect public.database_size_bytes();\n",
            ("permission denied", "database_size_bytes"),
        ),
        (
            "authenticated_database_size_denied",
            "set role authenticated;\nselect public.database_size_bytes();\n",
            ("permission denied", "database_size_bytes"),
        ),
        (
            "anon_begin_worker_deployment_denied",
            "set role anon;\nselect public.begin_worker_deployment('"
            + ("a" * 40)
            + "');\n",
            ("permission denied", "begin_worker_deployment"),
        ),
        (
            "authenticated_complete_worker_deployment_denied",
            "set role authenticated;\nselect public.complete_worker_deployment('"
            + ("a" * 40)
            + "', 300);\n",
            ("permission denied", "complete_worker_deployment"),
        ),
        (
            "authenticated_mark_worker_deployment_denied",
            "set role authenticated;\nselect public.mark_worker_deployment_triggered('"
            + ("a" * 40)
            + "');\n",
            ("permission denied", "mark_worker_deployment_triggered"),
        ),
        (
            "authenticated_abort_worker_deployment_denied",
            "set role authenticated;\nselect public.abort_worker_deployment('"
            + ("a" * 40)
            + "');\n",
            ("permission denied", "abort_worker_deployment"),
        ),
    ]
    for label, sql, fragments in denied_checks:
        _psql_expect_failure(container, sql, label=label, required_fragments=fragments)

    _psql(
        container,
        "\n".join(
            [
                "set role authenticated;",
                "select public.is_admin();",
                "reset role;",
                "set role service_role;",
                "select public.database_size_bytes();",
                "select public.run_retention_cleanup(true);",
                "select has_function_privilege(current_user,",
                "  'public.begin_worker_deployment(text)', 'EXECUTE');",
                "select has_function_privilege(current_user,",
                "  'public.complete_worker_deployment(text,integer)', 'EXECUTE');",
                "select has_function_privilege(current_user,",
                "  'public.mark_worker_deployment_triggered(text)', 'EXECUTE');",
                "select has_function_privilege(current_user,",
                "  'public.abort_worker_deployment(text)', 'EXECUTE');",
                "reset role;",
            ],
        ),
        label="security_definer_rpc_allowed_probe",
    )


def _supabase_stub_sql() -> str:
    return "\n".join(
        [
            "create schema if not exists auth;",
            "create table if not exists auth.users (",
            "  id uuid primary key,",
            "  email text",
            ");",
            "create role anon nologin;",
            "create role authenticated nologin;",
            "create role service_role nologin bypassrls;",
            "create publication supabase_realtime;",
            "create or replace function auth.uid()",
            "returns uuid",
            "language sql",
            "stable",
            "as $$",
            "  select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid;",
            "$$;",
            "create or replace function auth.role()",
            "returns text",
            "language sql",
            "stable",
            "as $$",
            "  select coalesce(",
            "    nullif(current_setting('request.jwt.claim.role', true), ''),",
            "    current_user",
            "  );",
            "$$;",
        ],
    )


def _live_enable_once_sql() -> str:
    requester = uuid4()
    reviewer = uuid4()
    return "\n".join(
        [
            "insert into auth.users (id, email)",
            "values",
            f"  ('{requester}', 'requester@example.invalid'),",
            f"  ('{reviewer}', 'reviewer@example.invalid');",
            f"select set_config('request.jwt.claim.sub', '{requester}', false);",
            "insert into public.manual_commands (",
            "  command_type,",
            "  status,",
            "  expires_at,",
            "  payload",
            ")",
            "values (",
            "  'request_live_enable',",
            "  'pending',",
            "  now() + interval '30 minutes',",
            "  '{"
            '"provider_contract_version":"toss-openapi-1.1.5",'
            '"risk_report_id":"risk-2026-06-28",'
            '"release_version":"release-1"'
            "}'::jsonb",
            ");",
            f"select set_config('request.jwt.claim.sub', '{reviewer}', false);",
            "update public.manual_commands",
            "set status = 'accepted'",
            "where command_type = 'request_live_enable'",
            "  and status = 'pending';",
            "update public.bot_settings",
            "set enabled = true,",
            "    mode = 'live',",
            "    live_order_allowed = true",
            "where id = 'singleton';",
            "do $$",
            "declare",
            "  applied_count integer;",
            "begin",
            "  select count(*)",
            "  into applied_count",
            "  from public.manual_commands",
            "  where command_type = 'request_live_enable'",
            "    and status = 'applied'",
            "    and applied_at is not null;",
            "  if applied_count <> 1 then",
            "    raise exception 'expected_exactly_one_applied_live_enable_command, got %',"
            " applied_count;",
            "  end if;",
            "end $$;",
            "update public.bot_settings",
            "set live_order_allowed = false",
            "where id = 'singleton';",
            "do $$",
            "begin",
            "  begin",
            "    update public.bot_settings",
            "    set live_order_allowed = true",
            "    where id = 'singleton';",
            "    raise exception 'expected_sticky_live_reenable_to_require_new_approval';",
            "  exception",
            "    when check_violation then",
            "      if sqlerrm not like",
            " '%live_execution_requires_fresh_accepted_manual_command%' then",
            "        raise;",
            "      end if;",
            "  end;",
            "end $$;",
            "update public.bot_settings",
            "set enabled = false,",
            "    mode = 'paper',",
            "    live_order_allowed = false",
            "where id = 'singleton';",
            "do $$",
            "begin",
            "  begin",
            "    update public.bot_settings",
            "    set enabled = true,",
            "        mode = 'live',",
            "        live_order_allowed = true",
            "    where id = 'singleton';",
            "    raise exception 'expected_second_live_enable_to_fail_without_new_approval';",
            "  exception",
            "    when check_violation then",
            "      if sqlerrm not like"
            " '%live_execution_requires_fresh_accepted_manual_command%' then",
            "        raise;",
            "      end if;",
            "  end;",
            "end $$;",
        ],
    )


def _deployment_lock_sql() -> str:
    target_sha = "a" * 40
    return "\n".join(
        [
            "set role service_role;",
            "select set_config('request.jwt.claim.role', 'service_role', false);",
            "do $$",
            "begin",
            "  begin",
            "    perform public.begin_worker_deployment(null);",
            "    raise exception 'expected_null_deployment_target_to_fail';",
            "  exception",
            "    when invalid_parameter_value then",
            "      if sqlerrm not like '%deployment_target_sha_invalid%' then",
            "        raise;",
            "      end if;",
            "  end;",
            "  begin",
            "    update public.bot_settings",
            "    set deployment_lock = true,",
            "        deployment_target_sha = null,",
            "        deployment_started_at = now()",
            "    where id = 'singleton';",
            "    raise exception 'expected_null_locked_target_constraint_to_fail';",
            "  exception",
            "    when check_violation then null;",
            "  end;",
            "end $$;",
            f"select public.begin_worker_deployment('{target_sha}');",
            "reset role;",
            "do $$",
            "declare",
            "  settings public.bot_settings%rowtype;",
            "begin",
            "  select * into settings from public.bot_settings where id = 'singleton';",
            "  if settings.deployment_lock is not true",
            "     or settings.deployment_target_sha <> '" + target_sha + "'",
            "     or settings.enabled is true",
            "     or settings.live_order_allowed is true then",
            "    raise exception 'deployment_lock_did_not_fail_closed';",
            "  end if;",
            "end $$;",
            "set role service_role;",
            "select set_config('request.jwt.claim.role', 'service_role', false);",
            f"select public.mark_worker_deployment_triggered('{target_sha}');",
            "insert into public.worker_heartbeats (status, details, created_at)",
            "values ('ok', '"
            + '{"release_sha":"'
            + target_sha
            + '","deployment_lock":true,"deployment_target_sha":"'
            + target_sha
            + '"}'
            + "'::jsonb, now() + interval '1 second');",
            "do $$",
            "begin",
            "  begin",
            f"    perform public.complete_worker_deployment('{target_sha}', null);",
            "    raise exception 'expected_null_deployment_heartbeat_max_age_to_fail';",
            "  exception",
            "    when invalid_parameter_value then",
            "      if sqlerrm not like '%deployment_heartbeat_max_age_invalid%' then",
            "        raise;",
            "      end if;",
            "  end;",
            "end $$;",
            f"select public.complete_worker_deployment('{target_sha}', 300);",
            "reset role;",
            "do $$",
            "declare",
            "  settings public.bot_settings%rowtype;",
            "begin",
            "  select * into settings from public.bot_settings where id = 'singleton';",
            "  if settings.deployment_lock is true",
            "     or settings.deployment_target_sha is not null",
            "     or settings.deployment_completed_at is null",
            "     or settings.enabled is true",
            "     or settings.live_order_allowed is true then",
            "    raise exception 'deployment_lock_did_not_release_safely';",
            "  end if;",
            "end $$;",
        ]
    )


def _run(
    command: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        input=input_text,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(command)}\n{result.stdout}\n{result.stderr}",
        )
    return result


if __name__ == "__main__":
    sys.exit(main())
