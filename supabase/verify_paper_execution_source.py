#!/usr/bin/env python3
"""Verify the durable Paper execution source on disposable PostgreSQL.

The verifier applies every repository migration and then runs the
source-specific SQL behavior contract.  It never connects to a hosted
Supabase project and always removes the disposable container.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import time
from uuid import uuid4


ROOT = Path(__file__).resolve().parent
MIGRATIONS = ROOT / "migrations"
BEHAVIOR = ROOT / "tests" / "paper_execution_source_behavior.sql"
SOURCE_MIGRATION = "20260714155117_paper_execution_source.sql"
POSTGRES_IMAGE = "postgres:16-alpine"
DB_PASSWORD = "paper-source-disposable-only"


class VerificationError(RuntimeError):
    """Raised when a disposable verification command fails."""


def run(
    args: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        input=input_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise VerificationError(
            f"command failed ({result.returncode}): {' '.join(args)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def psql(container: str, sql: str) -> subprocess.CompletedProcess[str]:
    return run(
        [
            "docker",
            "exec",
            "-i",
            container,
            "psql",
            "-X",
            "-q",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            "-At",
        ],
        input_text=sql,
    )


def wait_for_postgres(container: str) -> None:
    for _ in range(120):
        probe = run(
            ["docker", "exec", container, "pg_isready", "-U", "postgres"],
            check=False,
        )
        logs = run(["docker", "logs", container], check=False)
        ready_count = (logs.stdout + logs.stderr).count(
            "database system is ready to accept connections"
        )
        if probe.returncode == 0 and ready_count >= 2:
            return
        time.sleep(0.25)
    logs = run(["docker", "logs", container], check=False)
    raise VerificationError(
        "PostgreSQL did not become ready:\n" + logs.stdout + logs.stderr
    )


def bootstrap_sql() -> str:
    return """
create schema auth;
create table auth.users (id uuid primary key, email text);
create role anon nologin;
create role authenticated nologin;
create role service_role nologin bypassrls;
create role authenticator noinherit login password 'paper-source-disposable-only';
grant anon, authenticated, service_role to authenticator;
create publication supabase_realtime;
create function auth.uid() returns uuid language sql stable as $$
  select coalesce(
    nullif(current_setting('request.jwt.claim.sub', true), '')::uuid,
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb->>'sub')::uuid
  );
$$;
create function auth.role() returns text language sql stable as $$
  select coalesce(
    nullif(current_setting('request.jwt.claim.role', true), ''),
    nullif(current_setting('request.jwt.claims', true), '')::jsonb->>'role',
    current_user
  );
$$;
create function auth.jwt() returns jsonb language sql stable as $$
  select coalesce(
    nullif(current_setting('request.jwt.claims', true), '')::jsonb,
    '{}'::jsonb
  );
$$;
"""


def main() -> int:
    container = f"msp-paper-source-{uuid4().hex[:10]}"
    try:
        run(["docker", "info"])
        run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container,
                "-e",
                f"POSTGRES_PASSWORD={DB_PASSWORD}",
                POSTGRES_IMAGE,
            ]
        )
        wait_for_postgres(container)
        psql(container, bootstrap_sql())
        migrations = sorted(MIGRATIONS.glob("*.sql"))
        if not any(path.name == SOURCE_MIGRATION for path in migrations):
            raise VerificationError("paper source migration is missing")
        for migration in migrations:
            psql(container, migration.read_text(encoding="utf-8"))
        print(
            f"PASS fresh migration apply ({len(migrations)} files; "
            f"includes {SOURCE_MIGRATION})"
        )
        result = psql(container, BEHAVIOR.read_text(encoding="utf-8"))
        if result.stdout.strip():
            print(result.stdout.strip())
        print("PASS disposable Paper execution source behavioral contract")
        return 0
    except VerificationError as error:
        print(f"FAIL {error}")
        return 1
    finally:
        run(["docker", "rm", "-f", container], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
