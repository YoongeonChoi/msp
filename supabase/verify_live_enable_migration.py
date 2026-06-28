from __future__ import annotations

import argparse
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
        _verify_security_definer_rpc_grants(container)
    finally:
        if args.keep_container:
            print(f"Container kept for inspection: {container}")
        else:
            _run(["docker", "rm", "-f", container], check=False)

    print("FINAL=PASS live_enable_consumed_once rpc_hardening")
    return 0


def _docker_ready() -> bool:
    result = _run(["docker", "info", "--format", "{{.ServerVersion}}"], check=False)
    return result.returncode == 0


def _wait_for_postgres(container: str, timeout_sec: int) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        result = _run(["docker", "exec", container, "pg_isready", "-U", "postgres"], check=False)
        if result.returncode == 0:
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
            "create role service_role nologin;",
            "create publication supabase_realtime;",
            "create or replace function auth.uid()",
            "returns uuid",
            "language sql",
            "stable",
            "as $$",
            "  select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid;",
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
            " '%live_order_allowed_requires_fresh_accepted_manual_command%' then",
            "        raise;",
            "      end if;",
            "  end;",
            "end $$;",
        ],
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
