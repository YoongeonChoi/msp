#!/usr/bin/env python3
"""Disposable PostgreSQL/PostgREST verifier for migration replay and upgrades.

The verifier never connects to a hosted project. It creates isolated Docker
containers, applies every migration and the non-secret development seed, runs
catalog and behavioral assertions, then destroys its containers/network. It
verifies raw and controlled non-superuser PG17 fresh installs plus populated
public- and extensions-based 0015 cutoffs and a populated 0023 cutoff."""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import tomllib
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4


ROOT = Path(__file__).resolve().parent
MIGRATIONS = ROOT / "migrations"
SEED = ROOT / "seed.sql"
CONFIG = ROOT / "config.toml"
PGCRYPTO_PREFLIGHT = ROOT / "preflight" / "pgcrypto_replay_preflight.sql"
PGCRYPTO_CONVERGENCE = "20260718165749_pgcrypto_schema_convergence.sql"
REPOSITORY_SAFETY = ROOT.parent / ".github" / "scripts" / "repository_safety.py"
POSTGRES_IMAGE = "postgres:17-alpine"
POSTGREST_IMAGE = "postgrest/postgrest:v12.2.8"
DB_PASSWORD = "g1-g2-disposable-only"
JWT_SECRET = "g1-g2-disposable-jwt-secret-32-bytes-minimum"

ADMIN_1 = "11111111-1111-4111-8111-111111111111"
ADMIN_2 = "22222222-2222-4222-8222-222222222222"
OPERATOR = "33333333-3333-4333-8333-333333333333"
RISK = "44444444-4444-4444-8444-444444444444"
SUBJECT = "55555555-5555-4555-8555-555555555555"
VIEWER = "66666666-6666-4666-8666-666666666666"
STRATEGY = "67676767-6767-4767-8767-676767676767"
AUDITOR = "68686868-6868-4868-8868-686868686868"
RELEASE_MANAGER = "69696969-6969-4969-8969-696969696969"
NON_AUDITOR_HUMAN_ROLES = (
    ("platform_admin", ADMIN_1),
    ("operator", OPERATOR),
    ("risk_approver", RISK),
    ("strategy_reviewer", STRATEGY),
    ("release_manager", RELEASE_MANAGER),
    ("viewer", VIEWER),
)
SNAPSHOT_EVIDENCE_INTENT_ID = "87878787-8787-4787-8787-878787878787"
SNAPSHOT_AUDIT_RESOURCE_ID = "94949494-9494-4949-8949-949494949494"
LEGACY_USER = "77777777-7777-4777-8777-777777777778"
EVIDENCE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CONTRACT_EVIDENCE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
ACCOUNT_COMMAND = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
ACCESS_REQUEST = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
ACCESS_REVIEW = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
RELEASE_SHA = "a" * 40
NEXT_RELEASE_SHA = "b" * 40
OPENAPI_SHA256 = "2c54ebfd038a8c135f4b7f9036c42934d8ab9906c026251a7ae827b81e8e6aa8"


class VerificationError(RuntimeError):
    pass


def run(args: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
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


def psql(
    container: str,
    sql: str,
    *,
    check: bool = True,
    user: str = "postgres",
) -> subprocess.CompletedProcess[str]:
    return run(
        [
            "docker", "exec", "-i", container, "psql", "-X", "-q",
            "-U", user, "-d", "postgres", "-v", "ON_ERROR_STOP=1",
            "-At",
        ],
        input_text=sql,
        check=check,
    )


def wait_for_postgres(container: str) -> None:
    """Wait past the image's temporary init server and for the final server."""
    for _ in range(120):
        probe = run(
            ["docker", "exec", container, "psql", "-U", "postgres", "-Atc", "select 1"],
            check=False,
        )
        logs = run(["docker", "logs", container], check=False)
        ready_count = (logs.stdout + logs.stderr).count(
            "database system is ready to accept connections"
        )
        if probe.returncode == 0 and ready_count >= 2:
            return
        time.sleep(0.25)
    raise VerificationError(f"PostgreSQL did not become ready: {container}")


def verify_repository_inputs() -> None:
    safety = run(
        [
            sys.executable,
            str(REPOSITORY_SAFETY),
            "migrations",
            "--repo-root",
            str(ROOT.parent),
        ]
    )
    if safety.stdout.strip():
        print(safety.stdout.strip())

    try:
        with CONFIG.open("rb") as config_file:
            config = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise VerificationError(f"cannot read Supabase config: {error}") from error
    db_config = config.get("db")
    if not isinstance(db_config, dict):
        raise VerificationError("Supabase config is missing the [db] table")
    configured_major = db_config.get("major_version")
    image_match = re.fullmatch(r"postgres:([0-9]+)(?:[-:].*)?", POSTGRES_IMAGE)
    if image_match is None:
        raise VerificationError(f"cannot determine PostgreSQL major from {POSTGRES_IMAGE}")
    image_major = int(image_match.group(1))
    if configured_major != image_major:
        raise VerificationError(
            "Supabase config and verifier PostgreSQL majors differ: "
            f"config={configured_major!r}, image={image_major}"
        )
    print(f"PASS repository migration safety and PostgreSQL {image_major} contract")


def expect_failure(
    container: str,
    sql: str,
    *fragments: str,
    user: str = "postgres",
) -> None:
    result = psql(container, sql, check=False, user=user)
    if result.returncode == 0:
        raise VerificationError("negative assertion unexpectedly succeeded")
    output = (result.stdout + "\n" + result.stderr).lower()
    missing = [item for item in fragments if item.lower() not in output]
    if missing:
        raise VerificationError(f"negative assertion missed {missing}:\n{output}")


def bootstrap_sql() -> str:
    return """
create schema auth;
create table auth.users (id uuid primary key, email text);
create role anon nologin;
create role authenticated nologin;
create role service_role nologin bypassrls;
create role authenticator noinherit login password 'g1-g2-disposable-only';
grant anon, authenticated, service_role to authenticator;
create publication supabase_realtime;
create schema supabase_migrations;
create table supabase_migrations.schema_migrations (
  version text not null primary key,
  statements text[],
  name text
);
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


def migration_files() -> list[Path]:
    migrations = sorted(MIGRATIONS.glob("*.sql"))
    if not migrations:
        raise VerificationError("no repository migrations found")
    return migrations


def migration_position(migrations: list[Path], name: str) -> int:
    positions = [index for index, migration in enumerate(migrations) if migration.name == name]
    if len(positions) != 1:
        raise VerificationError(f"migration boundary is not unique: {name}")
    return positions[0]


def migration_identity(migration: Path) -> tuple[str, str]:
    match = re.fullmatch(r"([0-9]+)_([a-z0-9_]+)\.sql", migration.name)
    if match is None:
        raise VerificationError(f"invalid migration filename: {migration.name}")
    return match.group(1), match.group(2)


def migration_runner_guard_sql(user: str) -> str:
    expected_user = user.replace("'", "''")
    return f"""
do $migration_runner_contract$
begin
  if current_user <> '{expected_user}' then
    raise exception 'migration runner identity mismatch';
  end if;
  if (current_schemas(false))[1] is distinct from 'public' then
    raise exception 'migration runner search_path must begin with public';
  end if;
end;
$migration_runner_contract$;
"""


def apply_migration(
    container: str,
    migration: Path,
    *,
    user: str = "postgres",
) -> None:
    version, name = migration_identity(migration)
    migration_sql = migration.read_text(encoding="utf-8")
    psql(container, migration_runner_guard_sql(user) + migration_sql, user=user)
    statement_base64 = base64.b64encode(migration_sql.encode("utf-8")).decode("ascii")
    psql(
        container,
        migration_runner_guard_sql(user)
        + "insert into supabase_migrations.schema_migrations(version,statements,name) "
        f"values ('{version}',array[convert_from(decode('{statement_base64}',"
        f"'base64'),'UTF8')]::text[],'{name}');",
        user=user,
    )
    print(f"PASS migration {migration.name}")


def apply_migration_range(
    container: str,
    migrations: list[Path],
    *,
    start: int = 0,
    stop: int | None = None,
    user: str = "postgres",
) -> None:
    for migration in migrations[start:stop]:
        apply_migration(container, migration, user=user)


def verify_migration_ledger_fixture(
    container: str,
    migrations: list[Path],
) -> None:
    actual = json.loads(psql(container, """
select coalesce(jsonb_agg(
  jsonb_build_object(
    'version', version,
    'name', name,
    'statement_count', cardinality(statements),
    'statement_sha256', encode(
      extensions.digest(convert_to(statements[1], 'UTF8'), 'sha256'),
      'hex'
    )
  ) order by version
), '[]'::jsonb)
from supabase_migrations.schema_migrations;
""").stdout.strip())
    expected = []
    for migration in migrations:
        version, name = migration_identity(migration)
        expected.append({
            "version": version,
            "name": name,
            "statement_count": 1,
            "statement_sha256": hashlib.sha256(
                migration.read_text(encoding="utf-8").encode("utf-8")
            ).hexdigest(),
        })
    if actual != expected:
        if len(actual) != len(expected):
            detail = f"row_count expected={len(expected)}, actual={len(actual)}"
        else:
            mismatch = next(
                (
                    (expected_row, actual_row)
                    for expected_row, actual_row in zip(expected, actual, strict=True)
                    if expected_row != actual_row
                ),
                None,
            )
            detail = f"first_mismatch={mismatch}"
        raise VerificationError(
            "Supabase migration ledger fixture does not preserve repository inputs: "
            + detail
        )
    print("PASS Supabase CLI-shaped migration ledger preserves repository inputs")


def run_pgcrypto_preflight(container: str, *, user: str = "postgres") -> None:
    psql(
        container,
        PGCRYPTO_PREFLIGHT.read_text(encoding="utf-8"),
        user=user,
    )
    print("PASS PG17 pgcrypto replay preflight")


def pgcrypto_preflight_sql() -> str:
    return PGCRYPTO_PREFLIGHT.read_text(encoding="utf-8")


def pgcrypto_post_relocation_failure_sql() -> str:
    preflight = pgcrypto_preflight_sql()
    marker = "  alter extension pgcrypto set schema public;\n"
    if preflight.count(marker) != 1:
        raise VerificationError(
            "pgcrypto preflight relocation marker is missing or ambiguous"
        )
    return preflight.replace(
        marker,
        marker
        + "  raise exception "
        + "'verifier-injected failure after pgcrypto relocation';\n",
    )


def migration_ledger_snapshot(container: str) -> str:
    return psql(container, r'''
select coalesce(
  jsonb_agg(to_jsonb(migration) order by migration.version),
  '[]'::jsonb
)::text
from supabase_migrations.schema_migrations as migration;
''').stdout.strip()


def expect_pgcrypto_preflight_failure_unchanged(
    container: str,
    fixture_sql: str,
    *fragments: str,
    connection_user: str = "postgres",
    preflight_sql: str | None = None,
) -> None:
    before = pgcrypto_convergence_snapshot(container)
    ledger_before = migration_ledger_snapshot(container)
    expect_failure(
        container,
        "begin;\n" + fixture_sql.rstrip() + "\n"
        + (preflight_sql or pgcrypto_preflight_sql()),
        *fragments,
        user=connection_user,
    )
    after = pgcrypto_convergence_snapshot(container)
    ledger_after = migration_ledger_snapshot(container)
    if after != before or ledger_after != ledger_before:
        raise VerificationError(
            "rejected pgcrypto preflight did not preserve the exact pgcrypto "
            "and migration-ledger state: "
            f"before={before}, after={after}, "
            f"ledger_before={ledger_before}, ledger_after={ledger_after}"
        )


def extension_schema(container: str) -> str:
    return psql(container, """
select coalesce((
  select namespace.nspname
  from pg_extension as extension
  join pg_namespace as namespace on namespace.oid=extension.extnamespace
  where extension.extname='pgcrypto'
), '<absent>');
""").stdout.strip()


def verify_empty_pgcrypto_acl_guards(container: str) -> None:
    cases = (
        (
            "direct public CREATE",
            "grant create on schema public to authenticated;",
        ),
        (
            "inherited public CREATE",
            """
create role supabase_admin nologin;
create role preflight_inherited_create login inherit;
grant create on schema public to supabase_admin;
grant supabase_admin to preflight_inherited_create
  with inherit true, set false;
""",
        ),
        (
            "NOINHERIT SET-role public CREATE",
            """
create role supabase_admin nologin;
create role preflight_set_create login noinherit;
grant create on schema public to supabase_admin;
grant supabase_admin to preflight_set_create
  with inherit false, set true;
""",
        ),
    )
    for label, fixture in cases:
        expect_pgcrypto_preflight_failure_unchanged(
            container,
            fixture,
            "untrusted role can create in schema public",
        )
        print(f"PASS empty PG17 preflight rejects {label}")


def verify_empty_pgcrypto_runner_prerequisites(container: str) -> None:
    expect_pgcrypto_preflight_failure_unchanged(
        container,
        r'''
create role raw_operator login nosuperuser noinherit;
grant usage on schema supabase_migrations to raw_operator;
grant select on supabase_migrations.schema_migrations to raw_operator;
set role raw_operator;
''',
        "preflight role cannot create required replay objects",
    )
    print(
        "PASS empty PG17 preflight rejects ledger-only raw runner before mutation"
    )

    before = pgcrypto_convergence_snapshot(container)
    ledger_before = migration_ledger_snapshot(container)
    psql(container, r'''
create role supabase_admin login nosuperuser noinherit;
grant create on database postgres to supabase_admin;
grant usage, create on schema public to supabase_admin;
grant usage on schema supabase_migrations to supabase_admin;
grant select on supabase_migrations.schema_migrations to supabase_admin;
''')
    benign_fixture_installed = False
    try:
        install_pgcrypto_benign_unicode_callers(container)
        benign_fixture_installed = True
        run_pgcrypto_preflight(container, user="supabase_admin")
    finally:
        if benign_fixture_installed:
            remove_pgcrypto_benign_unicode_callers(container)
        psql(container, r'''
revoke select on supabase_migrations.schema_migrations
  from supabase_admin;
revoke usage on schema supabase_migrations from supabase_admin;
revoke usage, create on schema public from supabase_admin;
revoke create on database postgres from supabase_admin;
drop role supabase_admin;
''')
    after = pgcrypto_convergence_snapshot(container)
    ledger_after = migration_ledger_snapshot(container)
    if after != before or ledger_after != ledger_before:
        raise VerificationError(
            "approved empty runner preflight changed extension or ledger state: "
            f"before={before}, after={after}, "
            f"ledger_before={ledger_before}, ledger_after={ledger_after}"
        )
    print(
        "PASS empty PG17 preflight accepts approved supabase_admin runner and "
        "inert U&/comment/dollar-quoted caller text"
    )


def verify_empty_pgcrypto_future_caller_guard(container: str) -> None:
    expect_pgcrypto_preflight_failure_unchanged(
        container,
        r'''
create schema preflight_empty_future_caller_probe;
create function preflight_empty_future_caller_probe.public_digest_caller()
returns text
language plpgsql
as $function$
begin
  return pg_catalog.encode(
    public.digest('empty-future-caller-probe','sha256'), 'hex'
  );
end;
$function$;
''',
        "unsafe pgcrypto caller",
        "preflight_empty_future_caller_probe.public_digest_caller()",
    )
    print(
        "PASS empty PG17 preflight rejects future public.digest static caller"
    )


def pgcrypto_same_name_nonmember_fixture(
    schema: str,
    shape: str,
    *,
    member_name: str = "digest",
    create_schema: bool,
) -> str:
    if schema not in ("public", "extensions"):
        raise VerificationError(f"invalid pgcrypto collision schema: {schema}")
    schema_sql = f"create schema {schema};\n" if create_schema else ""
    member_shapes = {
        "digest": {
            "exact": (
                "input text, algorithm text",
                "bytea",
                "pg_catalog.convert_to(input || algorithm, 'UTF8')",
            ),
            "default": (
                "input text, algorithm text, compatibility text default ''",
                "bytea",
                "pg_catalog.convert_to("
                "input || algorithm || compatibility, 'UTF8')",
            ),
            "variadic": (
                "input text, variadic algorithms text[]",
                "bytea",
                "pg_catalog.convert_to("
                "input || pg_catalog.array_to_string(algorithms, ','), 'UTF8')",
            ),
        },
        "crypt": {
            "exact": (
                "input text, salt text",
                "text",
                "input || salt",
            ),
            "default": (
                "input text, salt text, compatibility text default ''",
                "text",
                "input || salt || compatibility",
            ),
            "variadic": (
                "input text, variadic salts text[]",
                "text",
                "input || pg_catalog.array_to_string(salts, ',')",
            ),
        },
    }
    if member_name not in member_shapes:
        raise VerificationError(
            f"invalid pgcrypto collision member name: {member_name}"
        )
    if shape not in member_shapes[member_name]:
        raise VerificationError(f"invalid pgcrypto collision shape: {shape}")
    signature, return_type, expression = member_shapes[member_name][shape]
    return schema_sql + f"""
create function {schema}.{member_name}({signature})
returns {return_type}
language sql
immutable
as $function$
  select {expression};
$function$;
"""


def verify_empty_pgcrypto_future_target_collisions(container: str) -> None:
    cases = (
        ("public", "exact", False),
        ("public", "default", False),
        ("public", "variadic", False),
        ("extensions", "exact", True),
        ("extensions", "default", True),
        ("extensions", "variadic", True),
    )
    for schema, shape, create_schema in cases:
        fixture = pgcrypto_same_name_nonmember_fixture(
            schema,
            shape,
            create_schema=create_schema,
        )
        expect_pgcrypto_preflight_failure_unchanged(
            container,
            fixture,
            "pgcrypto target member name conflicts with",
            f"{schema}.digest",
        )
        print(
            f"PASS empty PG17 preflight rejects {schema} {shape} "
            "digest nonmember"
        )
    expect_pgcrypto_preflight_failure_unchanged(
        container,
        r'''
create function public.crypt(input text, salt text)
returns text
language sql
immutable
as $function$
  select input || salt;
$function$;
''',
        "pgcrypto target member name conflicts with",
        "public.crypt",
    )
    print("PASS empty PG17 preflight rejects public crypt nonmember")


def verify_pgcrypto_member_contract_guards(container: str) -> None:
    cases = (
        (
            "untrusted pgcrypto member owner",
            """
create role preflight_untrusted_member_owner nologin;
alter function extensions.digest(text,text)
  owner to preflight_untrusted_member_owner;
set role migration_operator;
""",
            "pgcrypto member has an untrusted owner",
        ),
        (
            "added pgcrypto member",
            """
set role supabase_admin;
create function extensions.pgcrypto_preflight_added_member()
returns integer language sql immutable as 'select 1';
alter extension pgcrypto add function
  extensions.pgcrypto_preflight_added_member();
reset role;
set role migration_operator;
""",
            "pgcrypto 1.3 member catalog does not match the postgresql 17 contract",
        ),
        (
            "missing pgcrypto member",
            """
set role supabase_admin;
alter extension pgcrypto drop function extensions.hmac(text,text,text);
reset role;
set role migration_operator;
""",
            "pgcrypto target member name conflicts with",
        ),
        (
            "altered pgcrypto member metadata",
            """
set role supabase_admin;
alter function extensions.digest(text,text) cost 997;
reset role;
set role migration_operator;
""",
            "pgcrypto 1.3 member catalog does not match the postgresql 17 contract",
        ),
    )
    for label, fixture, fragment in cases:
        expect_pgcrypto_preflight_failure_unchanged(
            container,
            fixture,
            fragment,
        )
        print(f"PASS preflight rejects {label}")


def verify_pgcrypto_comment_caller_guards(container: str) -> None:
    cases = (
        (
            "schema-qualified comment-separated caller",
            """
create schema preflight_comment_probe;
create function preflight_comment_probe.qualified_comment_caller()
returns text
language plpgsql
as $function$
begin
  return encode(extensions./* verifier split */digest(
    'qualified-comment-probe','sha256'
  ),'hex');
end;
$function$;
set role migration_operator;
""",
        ),
        (
            "quoted schema-qualified caller",
            """
create schema preflight_comment_probe;
create function preflight_comment_probe.quoted_static_caller()
returns text
language sql
as $function$
  select encode(
    "extensions"."digest"('quoted-caller-probe','sha256'),
    'hex'
  );
$function$;
set role migration_operator;
""",
        ),
        (
            "nested-comment-separated caller",
            """
create schema preflight_comment_probe;
create function preflight_comment_probe.nested_comment_caller()
returns text
language sql
as $function$
  select encode(
    extensions./* outer /* nested */ tail */digest(
      'nested-comment-probe','sha256'
    ),
    'hex'
  );
$function$;
set role migration_operator;
""",
        ),
        (
            "search-path comment-separated caller",
            """
create schema preflight_comment_probe;
create function preflight_comment_probe.search_path_comment_caller()
returns text
language plpgsql
set search_path = extensions, pg_temp
as $function$
begin
  return encode(digest/* verifier split */(
    'search-path-comment-probe','sha256'
  ),'hex');
end;
$function$;
set role migration_operator;
""",
        ),
        (
            "string-literal line-comment marker before static caller",
            """
create schema preflight_comment_probe;
create function preflight_comment_probe.string_marker_static_caller()
returns table(marker text, digest_value bytea)
language sql
as $function$
  select '--'::text, extensions.digest('string-marker-probe','sha256');
$function$;
set role migration_operator;
""",
        ),
        (
            "standard-strings-off escaped quote before static caller",
            """
create schema preflight_comment_probe;
create function preflight_comment_probe.escaped_quote_static_caller()
returns text
language plpgsql
set standard_conforming_strings = off
set search_path = extensions, pg_temp
as $function$
begin
  perform 'x\\'--still string';
  return encode(digest('escaped-quote-probe','sha256'),'hex');
end;
$function$;
select preflight_comment_probe.escaped_quote_static_caller();
set role migration_operator;
""",
        ),
        (
            "standard-strings-on trailing backslash before static caller",
            """
create schema preflight_comment_probe;
create function preflight_comment_probe.trailing_backslash_static_caller()
returns text
language plpgsql
set standard_conforming_strings = on
set search_path = extensions, pg_temp
as $function$
begin
  perform 'ends-with-backslash\\';
  return encode(digest('trailing-backslash-probe','sha256'),'hex');
end;
$function$;
select preflight_comment_probe.trailing_backslash_static_caller();
set role migration_operator;
""",
        ),
        (
            "Unicode-escaped schema-qualified caller",
            """
create schema preflight_comment_probe;
create function preflight_comment_probe.unicode_schema_static_caller()
returns text
language plpgsql
as $function$
begin
  return encode(
    U&"extens\\0069ons".digest('unicode-schema-probe','sha256'),
    'hex'
  );
end;
$function$;
select preflight_comment_probe.unicode_schema_static_caller();
set role migration_operator;
""",
        ),
    )
    for label, fixture in cases:
        expect_pgcrypto_preflight_failure_unchanged(
            container,
            fixture,
            "unsafe pgcrypto caller",
        )
        print(f"PASS preflight rejects {label}")


def install_pgcrypto_benign_unicode_callers(container: str) -> None:
    psql(container, r'''
create schema preflight_unicode_benign_probe;
create function preflight_unicode_benign_probe.string_literal_only()
returns text
language sql
as $function$
  select 'U&"extensions".digest is data, not executable SQL'::text;
$function$;
create function preflight_unicode_benign_probe.comment_only()
returns integer
language sql
as $function$
  -- U&"extensions".digest('comment-only','sha256')
  select 1;
$function$;
create function preflight_unicode_benign_probe.nested_comment_only()
returns integer
language sql
as $function$
  /* outer extensions.digest( /* public.crypt( */ 'fake', 'bf') */
  select 2;
$function$;
create function preflight_unicode_benign_probe.dollar_quoted_literal_only()
returns text
language plpgsql
as $function$
begin
  return $payload$U&"extensions".digest is dollar-quoted data$payload$;
end;
$function$;
select preflight_unicode_benign_probe.string_literal_only();
select preflight_unicode_benign_probe.comment_only();
select preflight_unicode_benign_probe.nested_comment_only();
select preflight_unicode_benign_probe.dollar_quoted_literal_only();
''')


def remove_pgcrypto_benign_unicode_callers(container: str) -> None:
    psql(container, "drop schema preflight_unicode_benign_probe cascade;")


def verify_pgcrypto_catalog_search_path_guards(container: str) -> None:
    function_fixture = """
create schema preflight_catalog_probe;
create function preflight_catalog_probe.unqualified_digest_caller()
returns text
language plpgsql
as $function$
begin
  return encode(digest('catalog-default-probe','sha256'),'hex');
end;
$function$;
create role preflight_catalog_runtime login;
grant usage on schema preflight_catalog_probe to preflight_catalog_runtime;
"""
    cases = (
        (
            "ALTER DATABASE setrole=0 default",
            "alter database postgres set search_path = extensions, pg_temp;",
        ),
        (
            "global ALTER ROLE default",
            "alter role preflight_catalog_runtime "
            "set search_path = extensions, pg_temp;",
        ),
        (
            "quoted multi-entry role/database default",
            "alter role preflight_catalog_runtime in database postgres "
            "set search_path = \"$user\", public, \"extensions\", pg_temp;",
        ),
    )
    for label, setting in cases:
        expect_pgcrypto_preflight_failure_unchanged(
            container,
            function_fixture + setting + "\nset role migration_operator;",
            "unsafe pgcrypto caller",
            "catalog_default",
        )
        print(f"PASS preflight rejects {label}")


def require_public_first_search_path(
    container: str,
    *,
    user: str,
    label: str,
) -> None:
    receipt = json.loads(psql(
        container,
        "select jsonb_build_object("
        "'current_user',current_user,"
        "'schemas',current_schemas(false)"
        ");",
        user=user,
    ).stdout.strip())
    schemas = receipt.get("schemas")
    if not isinstance(schemas, list) or not schemas or schemas[0] != "public":
        raise VerificationError(
            f"{label} is not public-first: receipt={receipt}"
        )
    print(
        f"PASS {label} uses public-first default search_path as "
        f"{receipt.get('current_user')}"
    )


def verify_migration_runner_override_guard(
    container: str,
    *,
    user: str,
) -> None:
    before = pgcrypto_convergence_snapshot(container)
    ledger_before = migration_ledger_snapshot(container)
    expect_failure(
        container,
        "set search_path = extensions, pg_temp;\n"
        + migration_runner_guard_sql(user),
        "migration runner search_path must begin with public",
        user=user,
    )
    expect_failure(
        container,
        migration_runner_guard_sql("unexpected_migration_runner"),
        "migration runner identity mismatch",
        user=user,
    )
    after = pgcrypto_convergence_snapshot(container)
    ledger_after = migration_ledger_snapshot(container)
    if after != before or ledger_after != ledger_before:
        raise VerificationError(
            "migration runner override rejection changed extension or ledger "
            f"state: before={before}, after={after}, "
            f"ledger_before={ledger_before}, ledger_after={ledger_after}"
        )
    print(
        "PASS migration runner rejects identity mismatch and a public-free "
        "per-session search_path before applying SQL"
    )


def verify_preconvergence_extensions_guards(container: str) -> None:
    for schema in ("extensions", "public"):
        shapes = (
            ("default", "variadic")
            if schema == "extensions"
            else ("exact", "default", "variadic")
        )
        for shape in shapes:
            fixture = pgcrypto_same_name_nonmember_fixture(
                schema,
                shape,
                create_schema=False,
            ) + "\nset role migration_operator;"
            expect_pgcrypto_preflight_failure_unchanged(
                container,
                fixture,
                "pgcrypto target member name conflicts with",
                f"{schema}.digest",
            )
            print(
                "PASS retained extensions preflight rejects "
                f"{schema} {shape} digest nonmember"
            )
    expect_pgcrypto_preflight_failure_unchanged(
        container,
        r'''
create role preflight_evil_extensions login;
grant create on schema extensions to preflight_evil_extensions;
set role migration_operator;
''',
        "untrusted role can CREATE in schema extensions",
    )
    print(
        "PASS retained extensions preflight rejects future-target CREATE access"
    )
    expect_pgcrypto_preflight_failure_unchanged(
        container,
        "set role migration_operator;\n"
        "set search_path = extensions, pg_temp;",
        "preflight search_path must begin with public before pgcrypto relocation",
    )
    print(
        "PASS retained extensions preflight rejects public-free runner search_path"
    )
    require_public_first_search_path(
        container,
        user="migration_operator",
        label="retained extensions preflight connection",
    )


def verify_converged_pgcrypto_acl_guards(
    container: str,
    *,
    preflight_user: str,
) -> None:
    trusted_creator_setup = """
do $create_supabase_admin_if_missing$
begin
  if not exists (
    select 1 from pg_roles where rolname='supabase_admin'
  ) then
    execute 'create role supabase_admin nologin';
  end if;
end;
$create_supabase_admin_if_missing$;
revoke create on schema public from supabase_admin;
grant create on schema extensions to supabase_admin;
"""
    cases = (
        (
            "direct extensions CREATE",
            "grant create on schema extensions to anon;",
        ),
        (
            "inherited extensions CREATE",
            trusted_creator_setup + """
create role preflight_extensions_inherited login inherit;
grant supabase_admin to preflight_extensions_inherited
  with inherit true, set false;
""",
        ),
        (
            "NOINHERIT SET-role extensions CREATE",
            trusted_creator_setup + """
create role preflight_extensions_set login noinherit;
grant supabase_admin to preflight_extensions_set
  with inherit false, set true;
""",
        ),
    )
    assume_role = (
        "\nset role migration_operator;"
        if preflight_user == "migration_operator"
        else ""
    )
    for label, fixture in cases:
        expect_pgcrypto_preflight_failure_unchanged(
            container,
            fixture + assume_role,
            "untrusted role can create in schema extensions",
        )
        print(f"PASS converged preflight rejects {label}")


def verify_converged_pgcrypto_caller_guards(
    container: str,
    *,
    preflight_user: str,
) -> None:
    assume_role = (
        "\nset role migration_operator;"
        if preflight_user == "migration_operator"
        else ""
    )
    for member_name in ("digest", "crypt"):
        for schema in ("extensions", "public"):
            shapes = (
                ("default", "variadic")
                if schema == "extensions"
                else ("exact", "default", "variadic")
            )
            for shape in shapes:
                fixture = pgcrypto_same_name_nonmember_fixture(
                    schema,
                    shape,
                    member_name=member_name,
                    create_schema=False,
                )
                expect_pgcrypto_preflight_failure_unchanged(
                    container,
                    fixture + assume_role,
                    "pgcrypto target member name conflicts with",
                    f"{schema}.{member_name}",
                )
                print(
                    "PASS converged preflight rejects "
                    f"{schema} {shape} {member_name} nonmember"
                )
    expect_pgcrypto_preflight_failure_unchanged(
        container,
        r'''
create schema preflight_final_caller_probe;
create function preflight_final_caller_probe.public_qualified_digest_caller()
returns text
language plpgsql
as $function$
begin
  return pg_catalog.encode(
    public.digest('final-public-qualified-probe','sha256'), 'hex'
  );
end;
$function$;
''' + assume_role,
        "final application pgcrypto boundary is inconsistent",
        "preflight_final_caller_probe.public_qualified_digest_caller()",
    )
    print(
        "PASS converged preflight rejects custom-schema public.digest caller"
    )
    before = pgcrypto_convergence_snapshot(container)
    ledger_before = migration_ledger_snapshot(container)
    psql(container, r'''
create schema preflight_final_benign_probe;
create function preflight_final_benign_probe.digest(input text, algorithm text)
returns text
language sql
immutable
as $function$
  select input || ':' || algorithm;
$function$;
create function preflight_final_benign_probe.explicit_custom_digest_caller()
returns text
language sql
as $function$
  select preflight_final_benign_probe.digest(
    'explicit-custom-probe', 'not-pgcrypto'
  );
$function$;
select preflight_final_benign_probe.explicit_custom_digest_caller();
''')
    try:
        run_pgcrypto_preflight(container, user=preflight_user)
    finally:
        psql(container, "drop schema preflight_final_benign_probe cascade;")
    after = pgcrypto_convergence_snapshot(container)
    ledger_after = migration_ledger_snapshot(container)
    if after != before or ledger_after != ledger_before:
        raise VerificationError(
            "benign custom digest caller preflight changed pgcrypto or ledger "
            f"state: before={before}, after={after}, "
            f"ledger_before={ledger_before}, ledger_after={ledger_after}"
        )
    print(
        "PASS converged preflight accepts explicit non-pgcrypto custom digest caller"
    )
    before = pgcrypto_convergence_snapshot(container)
    ledger_before = migration_ledger_snapshot(container)
    install_pgcrypto_benign_unicode_callers(container)
    try:
        run_pgcrypto_preflight(container, user=preflight_user)
    finally:
        remove_pgcrypto_benign_unicode_callers(container)
    after = pgcrypto_convergence_snapshot(container)
    ledger_after = migration_ledger_snapshot(container)
    if after != before or ledger_after != ledger_before:
        raise VerificationError(
            "final benign lexer fixture changed pgcrypto or migration-ledger "
            f"state: before={before}, after={after}, "
            f"ledger_before={ledger_before}, ledger_after={ledger_after}"
        )
    print(
        "PASS converged preflight accepts inert literal, flat/nested comment, "
        "dollar-quoted U&, digest, and crypt text"
    )


def verify_pgcrypto_preflight_guards(
    container: str,
    *,
    preinstalled: bool,
    user: str = "postgres",
) -> None:
    if preinstalled:
        psql(container, """
insert into supabase_migrations.schema_migrations(version,name)
values
  ('0001','schema'),
  ('0015','paper_order_execution_details');
""")
        expect_failure(
            container,
            PGCRYPTO_PREFLIGHT.read_text(encoding="utf-8"),
            "migration history is not an exact repository prefix",
            user=user,
        )
        if extension_schema(container) != "extensions":
            raise VerificationError("history-gap rejection changed pgcrypto schema")
        psql(
            container,
            "delete from supabase_migrations.schema_migrations;",
        )
        public_acl_before = psql(
            container,
            "select coalesce(nspacl::text,'<default>') "
            "from pg_namespace where nspname='public';",
        ).stdout.strip()
        psql(container, "grant create on schema public to authenticated;")
        expect_failure(
            container,
            PGCRYPTO_PREFLIGHT.read_text(encoding="utf-8"),
            "untrusted role can create in schema public",
            user=user,
        )
        psql(container, "revoke create on schema public from authenticated;")
        public_acl_after = psql(
            container,
            "select coalesce(nspacl::text,'<default>') "
            "from pg_namespace where nspname='public';",
        ).stdout.strip()
        if public_acl_after != public_acl_before:
            raise VerificationError("public ACL probe did not restore its fixture")
        if extension_schema(container) != "extensions":
            raise VerificationError("public-ACL rejection changed pgcrypto schema")
        psql(container, """
create schema preflight_probe;
create function preflight_probe.unsafe_digest_caller()
returns text
language plpgsql
as $function$
begin
  return encode(extensions.digest('unsafe-probe','sha256'),'hex');
end;
$function$;
""")
        expect_failure(
            container,
            PGCRYPTO_PREFLIGHT.read_text(encoding="utf-8"),
            "unsafe pgcrypto caller",
            user=user,
        )
        if extension_schema(container) != "extensions":
            raise VerificationError("unsafe-caller rejection changed pgcrypto schema")
        psql(container, "drop function preflight_probe.unsafe_digest_caller();")
        psql(container, """
create function preflight_probe.unsafe_search_path_digest_caller()
returns text
language plpgsql
set search_path = extensions, pg_temp
as $function$
begin
  return encode(digest('unsafe-search-path-probe','sha256'),'hex');
end;
$function$;
""")
        expect_failure(
            container,
            PGCRYPTO_PREFLIGHT.read_text(encoding="utf-8"),
            "unsafe pgcrypto caller",
            user=user,
        )
        if extension_schema(container) != "extensions":
            raise VerificationError("search-path rejection changed pgcrypto schema")
        psql(container, """
create role preflight_runtime login;
grant usage on schema preflight_probe to preflight_runtime;
create function preflight_probe.unsafe_role_default_digest_caller()
returns text
language plpgsql
as $function$
begin
  return encode(digest('unsafe-role-default-probe','sha256'),'hex');
end;
$function$;
alter role preflight_runtime in database postgres
  set search_path = extensions, pg_temp;
""")
        expect_failure(
            container,
            PGCRYPTO_PREFLIGHT.read_text(encoding="utf-8"),
            "unsafe pgcrypto caller",
            "catalog_default",
            user=user,
        )
        if extension_schema(container) != "extensions":
            raise VerificationError("role-default rejection changed pgcrypto schema")
        psql(container, """
alter role preflight_runtime in database postgres reset search_path;
revoke usage on schema preflight_probe from preflight_runtime;
drop role preflight_runtime;
""")
        psql(container, "drop schema preflight_probe cascade;")
        print(
            "PASS pgcrypto preflight rejects ledger gaps, unsafe ACLs, qualified "
            "callers, and function/role search-path callers"
        )
    else:
        verify_empty_pgcrypto_acl_guards(container)
        verify_empty_pgcrypto_future_caller_guard(container)
        verify_empty_pgcrypto_future_target_collisions(container)
        verify_empty_pgcrypto_runner_prerequisites(container)
        psql(container, """
insert into supabase_migrations.schema_migrations(version,name)
values ('0001','schema');
""")
        expect_failure(
            container,
            PGCRYPTO_PREFLIGHT.read_text(encoding="utf-8"),
            "pgcrypto is missing from a retained repository database",
        )
        psql(
            container,
            "delete from supabase_migrations.schema_migrations where version='0001';",
        )
        print("PASS pgcrypto preflight rejects a missing retained extension")


def apply_repository(container: str, *, preinstall_pgcrypto: bool = False) -> None:
    psql(container, bootstrap_sql())
    preflight_user = "postgres"
    migration_user = "postgres"
    if preinstall_pgcrypto:
        psql(container, """
create role supabase_admin nologin nosuperuser bypassrls;
create role migration_operator login nosuperuser inherit bypassrls;
grant supabase_admin to migration_operator;
grant create on database postgres to supabase_admin;
grant create on schema public to supabase_admin;
grant usage on schema auth, supabase_migrations to migration_operator;
grant select, references on auth.users to migration_operator;
grant select on auth.users to supabase_admin;
grant select, insert on supabase_migrations.schema_migrations
  to migration_operator;
alter publication supabase_realtime owner to migration_operator;
create schema extensions authorization supabase_admin;
set role supabase_admin;
create extension pgcrypto with schema extensions;
reset role;
""")
        preflight_user = "migration_operator"
        migration_user = preflight_user
        identity = psql(
            container,
            "select concat_ws('|',current_user,rolsuper,rolbypassrls) "
            "from pg_roles where rolname=current_user;",
            user=preflight_user,
        ).stdout.strip()
        if identity != "migration_operator|f|t":
            raise VerificationError(
                "preflight role is not the expected temporary non-superuser "
                f"BYPASSRLS owner: {identity}"
            )
        expect_failure(
            container,
            PGCRYPTO_PREFLIGHT.read_text(encoding="utf-8"),
            "cannot act as every pgcrypto member owner",
            user=preflight_user,
        )
        if extension_schema(container) != "extensions":
            raise VerificationError("member-owner rejection changed pgcrypto schema")
        psql(container, """
do $align_pgcrypto_member_owners$
declare
  member record;
begin
  for member in
    select procedure.oid::regprocedure::text as identity
    from pg_depend as dependency
    join pg_proc as procedure
      on dependency.classid='pg_proc'::regclass
     and dependency.objid=procedure.oid
    join pg_extension as extension
      on dependency.refclassid='pg_extension'::regclass
     and dependency.refobjid=extension.oid
    where extension.extname='pgcrypto'
      and dependency.deptype='e'
    order by procedure.oid
  loop
    execute format(
      'alter function %s owner to supabase_admin',
      member.identity
    );
  end loop;
end;
$align_pgcrypto_member_owners$;
""")
        require_controlled_preinstalled_pgcrypto_fixture(
            pgcrypto_convergence_snapshot(container)
        )
        print("PASS pgcrypto preflight rejects split extension/member ownership")
        verify_pgcrypto_member_contract_guards(container)
        verify_pgcrypto_comment_caller_guards(container)
        verify_pgcrypto_catalog_search_path_guards(container)
        verify_preconvergence_extensions_guards(container)
    verify_pgcrypto_preflight_guards(
        container,
        preinstalled=preinstall_pgcrypto,
        user=preflight_user,
    )
    before_preflight = (
        pgcrypto_convergence_snapshot(container) if preinstall_pgcrypto else None
    )
    if preinstall_pgcrypto:
        expect_pgcrypto_preflight_failure_unchanged(
            container,
            "set role migration_operator;",
            "verifier-injected failure after pgcrypto relocation",
            preflight_sql=pgcrypto_post_relocation_failure_sql(),
        )
        if extension_schema(container) != "extensions":
            raise VerificationError(
                "post-relocation failure did not restore pgcrypto to extensions"
        )
        print(
            "PASS post-relocation failure preserves the tracked pgcrypto and "
            "migration-ledger state"
        )
        install_pgcrypto_benign_unicode_callers(container)
        require_public_first_search_path(
            container,
            user=migration_user,
            label="retained extensions non-superuser replay preflight connection",
        )

    run_pgcrypto_preflight(container, user=migration_user)
    if preinstall_pgcrypto:
        remove_pgcrypto_benign_unicode_callers(container)
        print(
            "PASS preflight accepts inert caller text in literals, flat/nested "
            "comments, and dollar-quoted data"
        )
        after_preflight = pgcrypto_convergence_snapshot(container)
        if before_preflight is None or before_preflight.get("extension_schema") != "extensions":
            raise VerificationError(f"invalid preinstalled pgcrypto state: {before_preflight}")
        if after_preflight.get("extension_schema") != "public":
            raise VerificationError(f"pgcrypto was not staged in public: {after_preflight}")
        changed = changed_pgcrypto_stable_keys(before_preflight, after_preflight)
        if changed:
            raise VerificationError(
                f"pgcrypto replay preflight changed stable catalog fields {changed}: "
                f"before={before_preflight}, after={after_preflight}"
            )
        print(
            "PASS non-superuser replay preflight uses the migration runner "
            "identity and preserves OIDs/routine metadata"
        )
    elif extension_schema(container) != "<absent>":
        raise VerificationError("empty-database preflight unexpectedly installed pgcrypto")

    require_public_first_search_path(
        container,
        user=migration_user,
        label=(
            "retained extensions migration connection"
            if preinstall_pgcrypto
            else "empty PG17 migration connection"
        ),
    )
    verify_migration_runner_override_guard(
        container,
        user=migration_user,
    )
    convergence_before: dict[str, object] | None = None
    migrations = migration_files()
    for migration in migrations:
        if migration.name == PGCRYPTO_CONVERGENCE:
            convergence_before = pgcrypto_convergence_snapshot(container)
        apply_migration(container, migration, user=migration_user)
        if migration.name == PGCRYPTO_CONVERGENCE:
            verify_pgcrypto_transition(container, convergence_before)
    if convergence_before is None:
        raise VerificationError("pgcrypto convergence migration was not applied")
    verify_migration_ledger_fixture(container, migrations)
    psql(
        container,
        migration_runner_guard_sql(migration_user)
        + SEED.read_text(encoding="utf-8"),
        user=migration_user,
    )
    print("PASS seed non-live defaults")
    verify_converged_pgcrypto_acl_guards(
        container,
        preflight_user=migration_user,
    )
    verify_converged_pgcrypto_caller_guards(
        container,
        preflight_user=migration_user,
    )
    before_final_preflight = pgcrypto_convergence_snapshot(container)
    run_pgcrypto_preflight(container, user=migration_user)
    after_final_preflight = pgcrypto_convergence_snapshot(container)
    if after_final_preflight != before_final_preflight:
        raise VerificationError(
            "final-state preflight was not idempotent: "
            f"before={before_final_preflight}, after={after_final_preflight}"
        )
    print("PASS pgcrypto preflight is idempotent after convergence")
    if preinstall_pgcrypto:
        cleanup_before = pgcrypto_convergence_snapshot(container)
        cleanup_ledger_before = migration_ledger_snapshot(container)
        psql(container, r'''
alter publication supabase_realtime owner to postgres;
reassign owned by migration_operator to supabase_admin;
alter role migration_operator nobypassrls;
revoke select, references on auth.users from migration_operator;
revoke select, insert on supabase_migrations.schema_migrations
  from migration_operator;
revoke usage on schema auth, supabase_migrations from migration_operator;
revoke supabase_admin from migration_operator;
''')
        cleanup_after = pgcrypto_convergence_snapshot(container)
        cleanup_ledger_after = migration_ledger_snapshot(container)
        cleanup_pgcrypto_keys = tuple(
            key for key in PGCRYPTO_RELOCATION_STABLE_KEYS
            if key not in ("routine_count", "routine_metadata_md5")
        )
        cleanup_changed = [
            key for key in cleanup_pgcrypto_keys
            if cleanup_before.get(key) != cleanup_after.get(key)
        ]
        if cleanup_changed or cleanup_ledger_after != cleanup_ledger_before:
            raise VerificationError(
                "non-superuser replay capability cleanup changed pgcrypto or "
                "migration-ledger state: "
                f"changed_pgcrypto_keys={cleanup_changed}, "
                f"ledger_before={cleanup_ledger_before}, "
                f"ledger_after={cleanup_ledger_after}"
            )
        cleanup_receipt = psql(container, r'''
select concat_ws('|',
  (
    select count(*)
    from (
      select oid from pg_class where relowner='migration_operator'::regrole
      union all
      select oid from pg_proc where proowner='migration_operator'::regrole
      union all
      select oid from pg_namespace where nspowner='migration_operator'::regrole
      union all
      select oid from pg_type where typowner='migration_operator'::regrole
    ) as owned_object
  ),
  (select pg_get_userbyid(pubowner) from pg_publication
    where pubname='supabase_realtime'),
  pg_has_role('migration_operator','supabase_admin','MEMBER'),
  (select rolbypassrls from pg_roles where rolname='migration_operator'),
  has_table_privilege(
    'migration_operator',
    'supabase_migrations.schema_migrations',
    'INSERT'
  ),
  has_table_privilege('migration_operator','auth.users','SELECT'),
  has_table_privilege('migration_operator','auth.users','REFERENCES'),
  has_schema_privilege('migration_operator','public','CREATE')
);
''').stdout.strip()
        if cleanup_receipt != "0|postgres|f|f|f|f|f|f":
            raise VerificationError(
                "non-superuser replay capability cleanup receipt mismatch: "
                f"{cleanup_receipt}"
            )
        scheduler_owner_receipt = psql(container, r'''
with contract_object(owner_oid) as (
  select relation.relowner
  from pg_catalog.pg_class as relation
  join pg_catalog.pg_namespace as namespace
    on namespace.oid=relation.relnamespace
  where namespace.nspname='private'
    and relation.relname in (
      'scheduler_job_definitions',
      'scheduler_job_runs',
      'scheduler_job_leases',
      'scheduler_replay_requests'
    )
  union all
  select procedure.proowner
  from pg_catalog.pg_proc as procedure
  join pg_catalog.pg_namespace as namespace
    on namespace.oid=procedure.pronamespace
  where (
      namespace.nspname='private'
      and procedure.proname in (
        'scheduler_definition_sha256_v1',
        'require_scheduler_outer_lease_v1',
        'scheduler_command_barrier_satisfied_v1',
        'scheduler_definition_document_v1',
        'scheduler_run_document_v1',
        'guard_scheduler_job_definition_v1',
        'guard_scheduler_job_run_v1',
        'ensure_scheduler_job_definition_impl',
        'converge_scheduler_job_definition_impl',
        'claim_due_scheduler_job_impl',
        'complete_scheduler_job_run_impl',
        'fail_scheduler_job_run_impl',
        'inspect_scheduler_dead_letter_impl',
        'replay_scheduler_dead_letter_impl'
      )
    )
    or (
      namespace.nspname='worker_api'
      and procedure.proname in (
        'ensure_scheduler_job_definition',
        'converge_scheduler_job_definition',
        'claim_due_scheduler_job',
        'complete_scheduler_job_run',
        'fail_scheduler_job_run',
        'inspect_scheduler_dead_letter',
        'replay_scheduler_dead_letter'
      )
    )
)
select concat_ws(
  '|',
  count(*),
  count(distinct contract_object.owner_oid),
  min(owner.rolname),
  bool_and(not owner.rolcanlogin),
  bool_and(owner.rolsuper or owner.rolbypassrls)
)
from contract_object
join pg_catalog.pg_roles as owner on owner.oid=contract_object.owner_oid;
''').stdout.strip()
        if scheduler_owner_receipt != "25|1|supabase_admin|t|t":
            raise VerificationError(
                "scheduler trusted-owner cleanup receipt mismatch: "
                f"{scheduler_owner_receipt}"
            )
        print(
            "PASS non-superuser replay reassigns created objects and revokes "
            "temporary BYPASSRLS owner, auth, ledger, and publication capabilities"
        )


def pgcrypto_convergence_snapshot(container: str) -> dict[str, object]:
    result = psql(container, r"""
with extension_state as (
  select
    e.oid as extension_oid,
    n.nspname as schema_name,
    pg_get_userbyid(e.extowner) as extension_owner,
    e.extversion as extension_version,
    e.extrelocatable as extension_relocatable,
    to_regprocedure(format('%I.digest(bytea,text)', n.nspname))::oid
      as digest_bytea_oid,
    to_regprocedure(format('%I.digest(text,text)', n.nspname))::oid
      as digest_text_oid
  from pg_extension e
  join pg_namespace n on n.oid=e.extnamespace
  where e.extname='pgcrypto'
), extension_member_dependencies as (
  select
    d.classid::regclass::text as class_name,
    d.objid,
    d.objsubid
  from pg_depend d
  join extension_state e on e.extension_oid=d.refobjid
  where d.refclassid='pg_extension'::regclass
    and d.deptype='e'
), extension_member_routines as (
  select
    d.class_name,
    d.objid,
    d.objsubid,
    p.proname,
    pg_get_function_identity_arguments(p.oid) as identity_arguments,
    pg_get_function_result(p.oid) as result_type,
    l.lanname as language_name,
    pg_get_userbyid(p.proowner) as owner_name,
    to_jsonb(p) - 'pronamespace' as metadata
  from extension_member_dependencies d
  join pg_proc p on d.class_name='pg_proc' and p.oid=d.objid
  join pg_language l on l.oid=p.prolang
), application_routines as (
  select
    p.oid,
    p.prosecdef,
    to_jsonb(p) - 'prosrc' - 'prosqlbody' as metadata,
    pg_get_functiondef(p.oid) as definition
  from pg_proc p
  join pg_namespace n on n.oid=p.pronamespace
  where n.nspname in ('private','api','worker_api')
    and p.prokind in ('f','p')
)
select jsonb_build_object(
  'extension_oid', (select extension_oid from extension_state),
  'extension_schema', (select schema_name from extension_state),
  'extension_owner', (select extension_owner from extension_state),
  'extension_version', (select extension_version from extension_state),
  'extension_relocatable', (select extension_relocatable from extension_state),
  'extensions_schema_owner', (
    select pg_get_userbyid(nspowner) from pg_namespace where nspname='extensions'
  ),
  'public_schema_owner', (
    select pg_get_userbyid(nspowner) from pg_namespace where nspname='public'
  ),
  'public_schema_acl', (
    select coalesce(nspacl::text,'<default>')
    from pg_namespace where nspname='public'
  ),
  'extensions_schema_acl', (
    select coalesce(nspacl::text,'<default>')
    from pg_namespace where nspname='extensions'
  ),
  'member_dependency_count', (
    select count(*) from extension_member_dependencies
  ),
  'member_dependency_state', (
    select coalesce(jsonb_agg(
      jsonb_build_object(
        'class_name', class_name,
        'objid', objid,
        'objsubid', objsubid
      ) order by class_name, objid, objsubid
    ), '[]'::jsonb)
    from extension_member_dependencies
  ),
  'member_routine_count', (
    select count(*) from extension_member_routines
  ),
  'member_routine_state', (
    select coalesce(jsonb_agg(
      jsonb_build_object(
        'class_name', class_name,
        'objid', objid,
        'objsubid', objsubid,
        'name', proname,
        'identity_arguments', identity_arguments,
        'result', result_type,
        'language', language_name,
        'owner', owner_name,
        'metadata', metadata
      ) order by objid, objsubid
    ), '[]'::jsonb)
    from extension_member_routines
  ),
  'digest_bytea_oid', (select digest_bytea_oid from extension_state),
  'digest_text_oid', (select digest_text_oid from extension_state),
  'digest_metadata_md5', (
    select md5(coalesce(string_agg(
      (to_jsonb(p) - 'pronamespace')::text,
      E'\n' order by p.oid
    ), ''))
    from pg_proc p
    where p.oid in (
      (select digest_bytea_oid from extension_state),
      (select digest_text_oid from extension_state)
    )
  ),
  'public_bytea_absent', to_regprocedure('public.digest(bytea,text)') is null,
  'public_text_absent', to_regprocedure('public.digest(text,text)') is null,
  'public_reference_count', (
    select count(*) from application_routines
    where definition
      ~* '"?public"?[[:space:]]*\.[[:space:]]*"?digest"?[[:space:]]*\('
  ),
  'unqualified_reference_count', (
    select count(*) from application_routines
    where definition ~* '(^|[^a-zA-Z0-9_."])"?digest"?[[:space:]]*\('
  ),
  'routine_count', (select count(*) from application_routines),
  'routine_metadata_md5', (
    select md5(coalesce(string_agg(metadata::text, E'\n' order by oid), ''))
    from application_routines
  ),
  'routine_definition_md5', (
    select md5(coalesce(string_agg(definition, E'\n' order by oid), ''))
    from application_routines
  ),
  'unsafe_create_count', (
    select count(*)
    from pg_roles r
    where r.rolname in ('anon','authenticated','service_role','authenticator')
      and has_schema_privilege(
        r.oid,
        to_regnamespace('extensions'),
        'CREATE'
      )
  ) + (
    select count(*)
    from pg_namespace n
    cross join lateral aclexplode(
      coalesce(n.nspacl, acldefault('n', n.nspowner))
    ) acl
    where n.nspname='extensions'
      and acl.grantee<>n.nspowner
      and lower(acl.privilege_type)='create'
  ),
  'unsafe_digest_caller_count', (
    select count(*)
    from application_routines p
    cross join pg_roles r
    where not p.prosecdef
      and p.definition
        ~* '"?extensions"?[[:space:]]*\.[[:space:]]*"?digest"?[[:space:]]*\('
      and r.rolname in ('anon','authenticated','service_role','authenticator')
      and has_function_privilege(r.oid, p.oid, 'EXECUTE')
      and not has_schema_privilege(
        r.oid,
        to_regnamespace('extensions'),
        'USAGE'
      )
  )
);
""").stdout.strip()
    return json.loads(result)


PGCRYPTO17_MEMBER_CONTRACT_SHA256 = (
    "1aee894d806ae9e1f5b2cf17533521fd9b0a851bea3dd1a8413c3ee71c9fce29"
)


def pgcrypto17_member_contract(snapshot: dict[str, object]) -> list[dict[str, object]]:
    members = snapshot.get("member_routine_state")
    if not isinstance(members, list):
        raise VerificationError("pgcrypto member routine oracle is not a list")
    contract: list[dict[str, object]] = []
    metadata_keys = (
        "prokind",
        "provolatile",
        "proparallel",
        "prosecdef",
        "proleakproof",
        "proisstrict",
        "proretset",
        "pronargs",
        "pronargdefaults",
        "proargmodes",
        "proargnames",
        "proconfig",
        "probin",
        "prosrc",
        "procost",
        "prorows",
        "prosupport",
        "provariadic",
        "prosqlbody",
        "proargdefaults",
        "protrftypes",
    )
    for member in members:
        if not isinstance(member, dict):
            raise VerificationError("pgcrypto member routine oracle has a non-object entry")
        metadata = member.get("metadata")
        if not isinstance(metadata, dict):
            raise VerificationError("pgcrypto member routine metadata is not an object")
        contract.append({
            "name": member.get("name"),
            "identity_arguments": member.get("identity_arguments"),
            "result": member.get("result"),
            "language": member.get("language"),
            **{key: metadata.get(key) for key in metadata_keys},
        })
    return sorted(
        contract,
        key=lambda member: (
            str(member.get("name")),
            str(member.get("identity_arguments")),
            str(member.get("result")),
        ),
    )


def pgcrypto17_member_contract_sha256(snapshot: dict[str, object]) -> str:
    canonical = json.dumps(
        pgcrypto17_member_contract(snapshot),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def require_pgcrypto17_member_contract(snapshot: dict[str, object]) -> None:
    actual = pgcrypto17_member_contract_sha256(snapshot)
    if actual != PGCRYPTO17_MEMBER_CONTRACT_SHA256:
        raise VerificationError(
            "pgcrypto PostgreSQL 17 independent member oracle mismatch: "
            f"expected={PGCRYPTO17_MEMBER_CONTRACT_SHA256}, actual={actual}"
        )


def require_pgcrypto_converged(snapshot: dict[str, object]) -> None:
    member_dependencies = snapshot.get("member_dependency_state")
    member_routines = snapshot.get("member_routine_state")
    member_inventory_invalid = (
        not isinstance(member_dependencies, list)
        or not isinstance(member_routines, list)
        or len(member_dependencies) != 36
        or len(member_routines) != 36
        or any(
            not isinstance(member, dict)
            or member.get("class_name") != "pg_proc"
            for member in member_dependencies
        )
        or any(
            not isinstance(member, dict)
            or member.get("class_name") != "pg_proc"
            or member.get("owner") not in ("postgres", "supabase_admin")
            or not isinstance(member.get("metadata"), dict)
            for member in member_routines
        )
    )
    if (
        snapshot.get("extension_schema") != "extensions"
        or snapshot.get("extension_owner") not in ("postgres", "supabase_admin")
        or snapshot.get("extension_version") != "1.3"
        or snapshot.get("extension_relocatable") is not True
        or snapshot.get("extensions_schema_owner") not in ("postgres", "supabase_admin")
        or snapshot.get("member_dependency_count") != 36
        or snapshot.get("member_routine_count") != 36
        or member_inventory_invalid
        or snapshot.get("digest_bytea_oid") is None
        or snapshot.get("digest_text_oid") is None
        or snapshot.get("public_bytea_absent") is not True
        or snapshot.get("public_text_absent") is not True
        or snapshot.get("public_reference_count") != 0
        or snapshot.get("unqualified_reference_count") != 0
        or snapshot.get("unsafe_create_count") != 0
        or snapshot.get("unsafe_digest_caller_count") != 0
    ):
        raise VerificationError(f"pgcrypto convergence mismatch: {snapshot}")
    require_pgcrypto17_member_contract(snapshot)


def require_controlled_preinstalled_pgcrypto_fixture(
    snapshot: dict[str, object],
) -> None:
    members = snapshot.get("member_routine_state")
    if (
        snapshot.get("extension_schema") != "extensions"
        or snapshot.get("extension_owner") != "supabase_admin"
        or snapshot.get("extension_version") != "1.3"
        or snapshot.get("extension_relocatable") is not True
        or snapshot.get("extensions_schema_owner") != "supabase_admin"
        or snapshot.get("member_dependency_count") != 36
        or snapshot.get("member_routine_count") != 36
        or not isinstance(members, list)
        or len(members) != 36
        or any(
            not isinstance(member, dict)
            or member.get("owner") != "supabase_admin"
            or not isinstance(member.get("metadata"), dict)
            for member in members
        )
    ):
        raise VerificationError(
            f"controlled preinstalled pgcrypto fixture mismatch: {snapshot}"
        )
    require_pgcrypto17_member_contract(snapshot)


PGCRYPTO_RELOCATION_STABLE_KEYS = (
    "extension_oid",
    "extension_owner",
    "extension_version",
    "extension_relocatable",
    "public_schema_owner",
    "extensions_schema_owner",
    "public_schema_acl",
    "extensions_schema_acl",
    "member_dependency_count",
    "member_dependency_state",
    "member_routine_count",
    "member_routine_state",
    "digest_bytea_oid",
    "digest_text_oid",
    "digest_metadata_md5",
    "routine_count",
    "routine_metadata_md5",
)


def changed_pgcrypto_stable_keys(
    before: dict[str, object],
    after: dict[str, object],
    *,
    include_schema_state: bool = True,
) -> list[str]:
    keys = PGCRYPTO_RELOCATION_STABLE_KEYS
    if not include_schema_state:
        keys = tuple(
            key for key in keys
            if key not in (
                "public_schema_owner",
                "extensions_schema_owner",
                "public_schema_acl",
                "extensions_schema_acl",
            )
        )
    return [
        key for key in keys
        if before.get(key) != after.get(key)
    ]


def verify_pgcrypto_transition(
    container: str,
    before: dict[str, object] | None,
) -> None:
    if before is None or before.get("extension_schema") != "public":
        raise VerificationError(f"invalid pre-convergence pgcrypto state: {before}")
    require_pgcrypto17_member_contract(before)
    after = pgcrypto_convergence_snapshot(container)
    require_pgcrypto_converged(after)
    changed = changed_pgcrypto_stable_keys(
        before,
        after,
        include_schema_state=False,
    )
    if changed:
        raise VerificationError(
            f"pgcrypto transition changed stable catalog fields {changed}: "
            f"before={before}, after={after}"
        )
    print("PASS pgcrypto forward convergence preserves OIDs and routine metadata")


def verify_controlled_non_superuser_preinstalled_replay(container: str) -> None:
    apply_repository(container, preinstall_pgcrypto=True)
    require_pgcrypto_converged(pgcrypto_convergence_snapshot(container))
    print("PASS controlled non-superuser preinstalled pgcrypto replay")


def verify_pgcrypto_convergence_idempotency(container: str) -> None:
    before = pgcrypto_convergence_snapshot(container)
    migration = MIGRATIONS / "20260718165749_pgcrypto_schema_convergence.sql"
    psql(container, migration.read_text(encoding="utf-8"))
    after = pgcrypto_convergence_snapshot(container)
    require_pgcrypto_converged(after)
    if before != after:
        raise VerificationError(
            f"pgcrypto convergence is not idempotent: before={before}, after={after}"
        )
    probe = psql(container, """
select encode(extensions.digest('pgcrypto-convergence-verifier','sha256'),'hex');
""").stdout.strip()
    expected = hashlib.sha256(b"pgcrypto-convergence-verifier").hexdigest()
    if probe != expected:
        raise VerificationError(f"pgcrypto digest behavior mismatch: {probe}")
    print("PASS pgcrypto convergence is idempotent and digest output is stable")


def verify_retained_public_future_target_collision(container: str) -> None:
    cases = (
        ("public", "default", False),
        ("public", "variadic", False),
        ("extensions", "exact", True),
        ("extensions", "default", True),
        ("extensions", "variadic", True),
    )
    for schema, shape, create_schema in cases:
        fixture = pgcrypto_same_name_nonmember_fixture(
            schema,
            shape,
            create_schema=create_schema,
        )
        expect_pgcrypto_preflight_failure_unchanged(
            container,
            fixture,
            "pgcrypto target member name conflicts with",
            f"{schema}.digest",
        )
        print(
            "PASS retained public preflight rejects "
            f"{schema} {shape} digest nonmember"
        )


def verify_retained_pgcrypto_preflight(
    container: str,
    *,
    expected_before: str,
) -> None:
    before = pgcrypto_convergence_snapshot(container)
    if before.get("extension_schema") != expected_before:
        raise VerificationError(
            f"retained pgcrypto schema mismatch: expected={expected_before}, before={before}"
        )
    if expected_before == "public":
        verify_retained_public_future_target_collision(container)
        caller_cases = (
            (
                "already-broken extensions search-path caller",
                """
create schema preflight_public_probe;
create function preflight_public_probe.broken_search_path_digest_caller()
returns text
language plpgsql
set search_path = extensions, pg_temp
as $function$
begin
  return encode(digest('public-state-probe','sha256'),'hex');
end;
$function$;
""",
            ),
            (
                "working public-qualified caller outside patched schemas",
                """
create schema preflight_public_probe;
create function preflight_public_probe.public_qualified_digest_caller()
returns text
language sql
as $function$
  select pg_catalog.encode(
    public.digest('retained-public-qualified','sha256'),
    'hex'
  );
$function$;
select preflight_public_probe.public_qualified_digest_caller();
""",
            ),
            (
                "working default-public unqualified caller outside patched schemas",
                """
create schema preflight_public_probe;
create function preflight_public_probe.default_public_digest_caller()
returns text
language plpgsql
as $function$
begin
  return pg_catalog.encode(
    digest('retained-default-public','sha256'),
    'hex'
  );
end;
$function$;
select preflight_public_probe.default_public_digest_caller();
""",
            ),
        )
        for label, fixture in caller_cases:
            expect_pgcrypto_preflight_failure_unchanged(
                container,
                fixture,
                "unsafe pgcrypto caller",
            )
            print(f"PASS retained public pgcrypto rejects {label}")
    else:
        for schema in ("extensions", "public"):
            shapes = (
                ("default", "variadic")
                if schema == "extensions"
                else ("exact", "default", "variadic")
            )
            for shape in shapes:
                fixture = pgcrypto_same_name_nonmember_fixture(
                    schema,
                    shape,
                    create_schema=False,
                )
                expect_pgcrypto_preflight_failure_unchanged(
                    container,
                    fixture,
                    "pgcrypto target member name conflicts with",
                    f"{schema}.digest",
                )
                print(
                    "PASS retained extensions upgrade rejects "
                    f"{schema} {shape} digest nonmember"
                )
    run_pgcrypto_preflight(container)
    after = pgcrypto_convergence_snapshot(container)
    if after.get("extension_schema") != "public":
        raise VerificationError(f"retained pgcrypto was not staged in public: {after}")
    changed = changed_pgcrypto_stable_keys(before, after)
    if changed:
        raise VerificationError(
            f"retained pgcrypto preflight changed stable catalog fields {changed}: "
            f"before={before}, after={after}"
        )
    print(
        f"PASS retained {expected_before} pgcrypto state stages safely in public"
    )


def verify_populated_0015_upgrade(
    container: str,
    *,
    legacy_pgcrypto_schema: str,
) -> None:
    if legacy_pgcrypto_schema not in ("public", "extensions"):
        raise VerificationError(
            f"unsupported retained pgcrypto schema: {legacy_pgcrypto_schema}"
        )
    psql(container, bootstrap_sql())
    if legacy_pgcrypto_schema == "extensions":
        psql(container, """
create schema extensions;
create extension pgcrypto with schema extensions;
""")
    migrations = migration_files()
    cutoff = migration_position(migrations, "0015_paper_order_execution_details.sql")
    apply_migration_range(container, migrations, stop=cutoff + 1)
    psql(container, SEED.read_text(encoding="utf-8"))
    psql(container, f"""
insert into auth.users (id,email) values ('{LEGACY_USER}','legacy@example.invalid');
insert into public.user_roles (user_id,role) values ('{LEGACY_USER}','admin');
insert into public.positions (
  symbol,quantity,avg_price_krw,current_price_krw,market_value_krw,
  unrealized_pnl_krw,unrealized_pnl_pct,sector,synced_at
) values ('005930',3,70000,71000,213000,3000,0.014285,'legacy',clock_timestamp());
with strategy as (
  select id from public.strategy_versions where version_name='weighted_factor_v1_seed'
), decision as (
  insert into public.decision_snapshots (
    cycle_id,symbol,action,final_score,confidence,strategy_version_id
  ) select gen_random_uuid(),'005930','buy',0.8,0.8,id from strategy returning id
)
insert into public.orders (
  decision_id,symbol,side,mode,status,amount_krw,idempotency_key,quantity,price_krw
) select id,'005930','buy','paper','paper',100000,'legacy-order-001',1,100000
from decision;
""")
    verify_retained_pgcrypto_preflight(
        container,
        expected_before=legacy_pgcrypto_schema,
    )
    apply_migration_range(container, migrations, start=cutoff + 1)
    result = psql(container, f"""
select concat_ws('|',
  (select count(*) from public.orders where idempotency_key='legacy-order-001'),
  (select count(*) from public.positions where symbol='005930'),
  (select count(*) from private.order_intents),
  (select count(*) from private.position_projection),
  (select count(*) from private.trading_accounts where state='pending_open'),
  (select sum(settled_cash_krw)::bigint from private.cash_balance_projection),
  (select count(*) from private.role_assignments
    where user_id='{LEGACY_USER}' and role='platform_admin'),
  (select count(*) from information_schema.role_table_grants
    where table_schema in ('private','public')
      and grantee in ('anon','authenticated','service_role')),
  (select count(*) from pg_publication_rel pr
    join pg_publication pub on pub.oid=pr.prpubid
    join pg_class c on c.oid=pr.prrelid
    join pg_namespace n on n.oid=c.relnamespace
    where pub.pubname='supabase_realtime' and n.nspname='public')
);
""").stdout.strip()
    if result != "1|1|0|0|2|0|1|0|0":
        raise VerificationError(f"populated 0015 upgrade isolation mismatch: {result}")
    require_pgcrypto_converged(pgcrypto_convergence_snapshot(container))
    before_final_preflight = pgcrypto_convergence_snapshot(container)
    run_pgcrypto_preflight(container)
    if pgcrypto_convergence_snapshot(container) != before_final_preflight:
        raise VerificationError("retained 0015 final preflight was not idempotent")
    print(
        "PASS populated 0015 upgrade "
        f"({legacy_pgcrypto_schema}): legacy frozen, no accounting aggregation"
    )


def require_operational_upgrade_converged(
    container: str,
    *,
    valid_account: str,
    valid_qualification: str,
    upgrade_intent: str,
    phase: str,
) -> None:
    result = psql(container, f"""
select concat_ws('|',
  (select execution_enabled from private.execution_controls
    where account_id='paper-primary'),
  (select updated_reason_code from private.execution_controls
    where account_id='paper-primary'),
  (select execution_enabled from private.execution_controls
    where account_id='{valid_account}'),
  (select updated_reason_code from private.execution_controls
    where account_id='{valid_account}'),
  (select control.expires_at=qualification.valid_until
    from private.execution_controls as control
    join private.qualifications as qualification
      on qualification.id='{valid_qualification}'
    where control.account_id='{valid_account}'),
  (select execution_enabled from private.execution_controls
    where account_id='contract-test-primary'),
  (select updated_reason_code from private.execution_controls
    where account_id='contract-test-primary'),
  (select state from private.execution_reconciliation_state
    where intent_id='{upgrade_intent}'),
  (select claim_release_sha is null and claim_fencing_token is null
    from private.execution_reconciliation_state
    where intent_id='{upgrade_intent}'),
  (select status from private.delivery_outbox
    where dedupe_key='upgrade-tokenless-retry'),
  (select lease_token is null from private.delivery_outbox
    where dedupe_key='upgrade-tokenless-retry'),
  (select status from private.delivery_outbox
    where dedupe_key='upgrade-final-crash'),
  (select count(*) from private.incidents
    where incident_type='delivery_dead_letter'
      and correlation_id='33333333-3333-4333-8333-333333333334'),
  (select available_at <= clock_timestamp() from private.delivery_outbox
    where dedupe_key='upgrade-future-clock')
);
""").stdout.strip()
    if phase == "after 0024":
        expected = (
            "f|operational_upgrade_qualification_invalid|"
            "t|operational_upgrade_qualification_revalidated|t|"
            "f|unresolved_reconciliation_break|pending|t|pending|t|"
            "dead_letter|1|t"
        )
    elif phase == "after complete tail":
        expected = (
            "f|operational_upgrade_qualification_invalid|"
            "f|qualification_v1_required|t|"
            "f|unresolved_reconciliation_break|pending|t|pending|t|"
            "dead_letter|1|t"
        )
    else:
        raise VerificationError(f"unknown operational upgrade phase: {phase}")
    if result != expected:
        raise VerificationError(
            f"populated 0023 operational upgrade mismatch ({phase}): {result}"
        )
    print(f"PASS populated 0023 operational invariants ({phase})")


def verify_populated_0023_operational_upgrade(container: str) -> None:
    psql(container, bootstrap_sql())
    migrations = migration_files()
    cutoff = migration_position(migrations, "0023_operational_safety_closure.sql")
    convergence = migration_position(
        migrations,
        "0024_operational_upgrade_convergence.sql",
    )
    if convergence != cutoff + 1:
        raise VerificationError("0023/0024 operational migration boundary is not adjacent")
    apply_migration_range(container, migrations, stop=cutoff + 1)
    psql(container, SEED.read_text(encoding="utf-8"))
    psql(container, fixture_sql())
    valid_account = "paper-upgrade-valid"
    valid_qualification = "24242424-2424-4424-8424-242424242424"
    valid_command = "25252525-2525-4525-8525-252525252525"
    valid_risk = "26262626-2626-4626-8626-262626262626"
    upgrade_intent = "27272727-2727-4727-8727-272727272727"
    psql(container, f"""
-- Invalid enabled control: no applied qualification command exists.
update private.execution_controls
set execution_enabled=true,
    control_epoch=control_epoch+1,
    effective_at=clock_timestamp()-interval '1 minute',
    expires_at=clock_timestamp()+interval '2 days',
    updated_reason_code='pre_upgrade_unverified_enable',
    updated_at=clock_timestamp()+interval '1 microsecond'
where account_id='paper-primary';

-- Valid enabled control whose expiry was extended after 0023's application
-- guard; 0024 must retain it but clamp it back to qualification validity.
insert into private.trading_accounts (
 account_id,environment,broker,state,opening_capital_krw,opened_at
) values (
 '{valid_account}','paper','internal_paper','open',10000000,clock_timestamp()
);
insert into private.execution_controls (
 account_id,environment,execution_policy_version,execution_policy_sha256,
 risk_policy_sha256,effective_at,expires_at
)
select '{valid_account}','paper',execution_policy_version,
 execution_policy_sha256,risk_policy_sha256,
 clock_timestamp()-interval '5 minutes',clock_timestamp()+interval '4 hours'
from private.execution_controls where account_id='paper-primary';
insert into private.qualifications (
 id,environment,status,release_sha,ledger_checkpoint,dataset_version,
 execution_policy_version,execution_policy_sha256,risk_policy_sha256,
 strategy_version_id,risk_policy_version_id,valid_from,valid_until,
 g1_status,g1_checked_at,g1_evidence_id,g2_status,g2_checked_at,g2_evidence_id
)
select '{valid_qualification}','paper','qualified','{RELEASE_SHA}',
 'upgrade-ledger','upgrade-dataset',control.execution_policy_version,
 control.execution_policy_sha256,control.risk_policy_sha256,strategy.id,
 '{valid_risk}',clock_timestamp()-interval '5 minutes',
 clock_timestamp()+interval '1 hour','pass',clock_timestamp(),'{EVIDENCE}',
 'pass',clock_timestamp(),'{EVIDENCE}'
from private.execution_controls as control
cross join lateral (
 select id from public.strategy_versions order by created_at limit 1
) as strategy
where control.account_id='{valid_account}';
insert into private.operation_commands (
 id,command_type,state,requested_change,revision,evidence_id,target_release_sha,
 requester_user_id,reviewer_user_id,claimed_by_service,requested_at,reviewed_at,
 claimed_at,claim_expires_at,applied_at,expires_at,result_summary,
 idempotency_key
)
select '{valid_command}','paper_resume','applied',jsonb_build_object(
 'account_id','{valid_account}','environment','paper',
 'expected_state_version',control.control_epoch,
 'qualification_id','{valid_qualification}',
 'strategy_version_id',qualification.strategy_version_id::text,
 'risk_policy_version_id','{valid_risk}','release_sha','{RELEASE_SHA}',
 'ledger_checkpoint','upgrade-ledger',
 'execution_policy_version',control.execution_policy_version,
 'execution_policy_sha256',control.execution_policy_sha256,
 'risk_policy_sha256',control.risk_policy_sha256,
 'reason_code','upgrade_valid_resume'
),1,'{EVIDENCE}','{RELEASE_SHA}','{OPERATOR}','{RISK}','upgrade-worker',
 clock_timestamp()-interval '5 minutes',clock_timestamp()-interval '4 minutes',
 clock_timestamp()-interval '3 minutes',clock_timestamp()+interval '5 minutes',
 clock_timestamp()-interval '2 minutes',clock_timestamp()+interval '1 hour',
 '{{}}'::jsonb,'upgrade-valid-command'
from private.execution_controls as control
join private.qualifications as qualification
  on qualification.id='{valid_qualification}'
where control.account_id='{valid_account}';
update private.execution_controls as control
set execution_enabled=true,
    control_epoch=control.control_epoch+1,
    active_strategy_version_id=qualification.strategy_version_id::text,
    active_risk_policy_version_id=qualification.risk_policy_version_id,
    last_command_id='{valid_command}',
    effective_at=clock_timestamp()-interval '1 minute',
    expires_at=clock_timestamp()+interval '4 hours',
    updated_reason_code='qualified_resume',
    updated_at=clock_timestamp()+interval '1 microsecond'
from private.qualifications as qualification
where control.account_id='{valid_account}'
  and qualification.id='{valid_qualification}';
update private.execution_controls
set control_epoch=control_epoch+1,
    expires_at=clock_timestamp()+interval '4 hours',
    updated_reason_code='pre_upgrade_expiry_extension',
    updated_at=clock_timestamp()+interval '1 microsecond'
where account_id='{valid_account}';

-- Simulate a break inserted before the 0023 stop trigger was deployed.
alter table private.reconciliation_breaks
  disable trigger stop_execution_for_reconciliation_break_v1;
with run as (
  insert into private.reconciliation_runs (
    id,account_id,environment,started_at,completed_at,result,release_sha
  ) values (
    '28282828-2828-4828-8828-282828282828','contract-test-primary',
    'contract_test',clock_timestamp()-interval '1 hour',
    clock_timestamp()-interval '1 hour','breaks_found','{RELEASE_SHA}'
  ) returning id
)
insert into private.reconciliation_breaks (
 id,run_id,account_id,break_type,state,detected_at,summary_code
)
select '29292929-2929-4929-8929-292929292929',id,
 'contract-test-primary','execution','open',
 clock_timestamp()-interval '1 hour','pre_upgrade_open_break'
from run;
alter table private.reconciliation_breaks
  enable trigger stop_execution_for_reconciliation_break_v1;
update private.execution_controls
set execution_enabled=true,
    control_epoch=control_epoch+1,
    effective_at=clock_timestamp()-interval '1 minute',
    expires_at=clock_timestamp()+interval '2 days',
    updated_reason_code='pre_upgrade_break_not_enforced',
    updated_at=clock_timestamp()+interval '1 microsecond'
where account_id='contract-test-primary';

-- Tokenless reconciliation lease from a 0023 worker.
insert into private.execution_decisions (
 id,account_id,environment,strategy_version_id,symbol,action,decision_at,
 signal_valid_from,signal_valid_until,feature_snapshot_sha256,
 decision_sha256,release_sha
) values (
 '30303030-3030-4030-8030-303030303030','paper-primary','paper',
 'upgrade-strategy','005930','buy',clock_timestamp()-interval '2 minutes',
 clock_timestamp()-interval '3 minutes',clock_timestamp()+interval '1 hour',
 '{'3' * 64}','{'4' * 64}','{RELEASE_SHA}'
);
insert into private.risk_results (
 id,decision_id,account_id,environment,strategy_version_id,
 risk_policy_sha256,control_epoch,allowed,reason_codes,result_sha256,
 evaluated_at,expires_at,release_sha
)
select '31313131-3131-4131-8131-313131313131',
 '30303030-3030-4030-8030-303030303030','paper-primary','paper',
 'upgrade-strategy',risk_policy_sha256,control_epoch,true,array[]::text[],
 '{'5' * 64}',clock_timestamp()-interval '1 minute',
 clock_timestamp()+interval '1 hour','{RELEASE_SHA}'
from private.execution_controls where account_id='paper-primary';
insert into private.order_intents (
 id,semantic_key_sha256,account_id,environment,strategy_version_id,
 decision_id,risk_result_id,correlation_id,symbol,side,quantity,limit_price_krw,
 decision_at,signal_valid_from,signal_valid_until,eligible_at,expires_at,
 execution_policy_version,execution_policy_sha256,cost_schedule_version,
 cost_schedule_evidence_sha256,cash_commitment_krw,risk_policy_sha256,
 control_epoch,release_sha
)
select '{upgrade_intent}','{'6' * 64}','paper-primary','paper',
 'upgrade-strategy','30303030-3030-4030-8030-303030303030',
 '31313131-3131-4131-8131-313131313131','{upgrade_intent}',
 '005930','buy',1,10000,clock_timestamp()-interval '2 minutes',
 clock_timestamp()-interval '3 minutes',clock_timestamp()+interval '1 hour',
 clock_timestamp()-interval '1 minute',clock_timestamp()+interval '1 hour',
 execution_policy_version,execution_policy_sha256,'upgrade-cost','{'7' * 64}',
 10000,risk_policy_sha256,control_epoch,'{RELEASE_SHA}'
from private.execution_controls where account_id='paper-primary';
insert into private.execution_reconciliation_state (
 intent_id,priority,state,next_reconcile_at,lease_owner,lease_expires_at,
 attempt_count,last_reason_code
) values (
 '{upgrade_intent}',30,'leased',clock_timestamp()-interval '1 minute',
 'old-worker',clock_timestamp()+interval '1 hour',1,'pre_upgrade_claim'
);

insert into private.delivery_outbox (
 id,event_type,aggregate_type,aggregate_id,dedupe_key,payload,
 destination_type,status,available_at,lease_owner,lease_expires_at,
 attempt_count,max_attempts
) values
 ('32323232-3232-4232-8232-323232323232','verification','upgrade','retry',
  'upgrade-tokenless-retry','{{}}','operations_metric','leased',
  clock_timestamp()+interval '30 days','old-worker',
  clock_timestamp()+interval '30 days',1,3),
 ('33333333-3333-4333-8333-333333333334','verification','upgrade','final',
  'upgrade-final-crash','{{}}','operations_metric','leased',
  clock_timestamp()+interval '30 days','old-worker',
  clock_timestamp()+interval '30 days',3,3),
 ('34343434-3434-4434-8434-343434343434','verification','upgrade','clock',
  'upgrade-future-clock','{{}}','operations_metric','pending',
  clock_timestamp()+interval '30 days',null,null,0,3);
""")

    verify_retained_pgcrypto_preflight(container, expected_before="public")
    apply_migration(container, migrations[convergence])
    require_operational_upgrade_converged(
        container,
        valid_account=valid_account,
        valid_qualification=valid_qualification,
        upgrade_intent=upgrade_intent,
        phase="after 0024",
    )
    print(
        "PASS populated 0023->0024 upgrade: controls, claims and caller-clock "
        "delivery rows converge fail closed"
    )
    apply_migration_range(container, migrations, start=convergence + 1)
    require_operational_upgrade_converged(
        container,
        valid_account=valid_account,
        valid_qualification=valid_qualification,
        upgrade_intent=upgrade_intent,
        phase="after complete tail",
    )
    require_pgcrypto_converged(pgcrypto_convergence_snapshot(container))
    before_final_preflight = pgcrypto_convergence_snapshot(container)
    run_pgcrypto_preflight(container)
    if pgcrypto_convergence_snapshot(container) != before_final_preflight:
        raise VerificationError("retained 0023 final preflight was not idempotent")
    print("PASS populated 0023 upgrade remains compatible with the complete migration tail")


def fixture_sql() -> str:
    return f"""
insert into auth.users (id, email) values
  ('{ADMIN_1}', 'admin1@example.invalid'),
  ('{ADMIN_2}', 'admin2@example.invalid'),
  ('{OPERATOR}', 'operator@example.invalid'),
  ('{RISK}', 'risk@example.invalid'),
  ('{SUBJECT}', 'subject@example.invalid'),
  ('{VIEWER}', 'viewer@example.invalid'),
  ('{STRATEGY}', 'strategy@example.invalid'),
  ('{AUDITOR}', 'auditor@example.invalid'),
  ('{RELEASE_MANAGER}', 'release-manager@example.invalid');
insert into private.role_assignments (user_id, role, reason) values
  ('{ADMIN_1}', 'platform_admin', 'verifier_fixture'),
  ('{ADMIN_2}', 'platform_admin', 'verifier_fixture'),
  ('{OPERATOR}', 'operator', 'verifier_fixture'),
  ('{RISK}', 'risk_approver', 'verifier_fixture'),
  ('{VIEWER}', 'viewer', 'verifier_fixture'),
  ('{STRATEGY}', 'strategy_reviewer', 'verifier_fixture'),
  ('{AUDITOR}', 'auditor', 'verifier_fixture'),
  ('{RELEASE_MANAGER}', 'release_manager', 'verifier_fixture');
insert into private.control_evidence (
  id, evidence_type, environment, artifact_uri, artifact_sha256,
  captured_at, verified_at, verified_by, metadata_summary
) values
  ('{EVIDENCE}', 'account_opening', 'paper',
   'https://evidence.example.invalid/account-opening.json', '{'1' * 64}',
   clock_timestamp() - interval '1 minute', clock_timestamp(), '{ADMIN_1}',
   '{{"fixture":true}}'::jsonb),
  ('{CONTRACT_EVIDENCE}', 'contract_test_contract', 'contract_test',
   'https://openapi.tossinvest.com/openapi-docs/latest/openapi.json',
   '{OPENAPI_SHA256}', '2026-07-14T12:11:03.1057242Z', clock_timestamp(),
   '{ADMIN_2}', '{{"bytes":340381,"disposable_verifier":true}}'::jsonb);
insert into private.provider_contract_registry (
  provider, qualification_environment, execution_transport, contract_version,
  openapi_sha256, official_artifact_uri, retrieved_at, evidence_id,
  release_sha, status, requested_by, reviewed_by, effective_from, effective_until
) values (
  'toss', 'contract_test', 'local_contract_simulator', 'latest-2026-07-14',
  '{OPENAPI_SHA256}',
  'https://openapi.tossinvest.com/openapi-docs/latest/openapi.json',
  '2026-07-14T12:11:03.1057242Z', '{CONTRACT_EVIDENCE}', '{RELEASE_SHA}',
  'approved', '{ADMIN_1}', '{ADMIN_2}',
  clock_timestamp() - interval '1 minute', clock_timestamp() + interval '1 day'
);
"""


def install_paper_fill_bar_evidence(
    container: str,
    *,
    intent_id: str,
    filled_at: str,
    label: str,
    volume: int = 1_000_000,
) -> None:
    if volume <= 0:
        raise VerificationError("paper fill evidence volume must be positive")
    series_id = str(uuid4())
    fixture_id = str(uuid4())
    strategy_version_id = str(uuid4())

    def evidence_hash(kind: str) -> str:
        return hashlib.sha256(f"{label}:{intent_id}:{kind}".encode()).hexdigest()

    fixture_hash = evidence_hash("fixture")
    psql(container, f"""
insert into private.paper_bar_series (
  id,environment,source_kind,dataset_version,symbol,model_version,
  execution_policy_version,tick_rule_version,tick_size_krw,
  tick_rule_evidence_sha256,volume_source,volume_evidence_sha256,
  corporate_action_status,corporate_action_evidence_sha256,
  market_calendar_version,market_calendar_evidence_sha256,effective_from,
  effective_until,created_release_sha
)
select
  '{series_id}','paper','local_fixture','{label}-{series_id}',intent.symbol,
  'dedupe-model',intent.execution_policy_version,'dedupe-tick',1,
  '{'c' * 64}','shares','{'d' * 64}','not_required','{'e' * 64}',
  'dedupe-calendar','{'b' * 64}',intent.eligible_at-interval '1 day',
  intent.expires_at+interval '1 day',intent.release_sha
from private.order_intents as intent
where intent.id='{intent_id}';
insert into private.paper_bar_fixture_sets (
  id,series_id,batch_sequence,first_minute,last_minute,bar_count,
  observed_through,fixture_sha256,evidence_urn,release_sha,ingested_at
)
select
  '{fixture_id}','{series_id}',1,
  date_trunc('minute','{filled_at}'::timestamptz)-interval '1 minute',
  date_trunc('minute','{filled_at}'::timestamptz)-interval '1 minute',1,
  date_trunc('minute','{filled_at}'::timestamptz),'{fixture_hash}',
  'urn:sha256:{fixture_hash}',intent.release_sha,clock_timestamp()
from private.order_intents as intent
where intent.id='{intent_id}';
insert into private.paper_minute_bars (
  fixture_set_id,series_id,sequence,minute,completed_at,as_of,source_sha256,
  is_complete,open_krw,high_krw,low_krw,close_krw,volume,bar_sha256
) values (
  '{fixture_id}','{series_id}',1,
  date_trunc('minute','{filled_at}'::timestamptz)-interval '1 minute',
  date_trunc('minute','{filled_at}'::timestamptz),
  date_trunc('minute','{filled_at}'::timestamptz),'{evidence_hash('source')}',
  true,10000,10000,10000,10000,{volume},'{evidence_hash('bar')}'
);
insert into private.paper_execution_candidates (
  intent_id,account_id,environment,semantic_key_sha256,decision_id,risk_result_id,
  decision_feature_sha256,risk_evaluated_at,risk_expires_at,strategy_version_id,
  symbol,side,quantity,limit_price_krw,cash_commitment_krw,decision_at,
  signal_valid_from,signal_valid_until,eligible_at,expires_at,
  execution_policy_version,cost_schedule_version,cost_schedule_evidence_sha256,
  fixture_series_id,risk_input,candidate_sha256,source_release_sha,created_at
)
select
  intent.id,intent.account_id,intent.environment,intent.semantic_key_sha256,
  intent.decision_id,intent.risk_result_id,'{'7' * 64}',intent.decision_at,
  risk.expires_at,'{strategy_version_id}',intent.symbol,intent.side,
  intent.quantity,intent.limit_price_krw,intent.cash_commitment_krw,
  intent.decision_at,intent.signal_valid_from,intent.signal_valid_until,
  intent.eligible_at,date_trunc('minute',intent.expires_at),
  intent.execution_policy_version,
  intent.cost_schedule_version,intent.cost_schedule_evidence_sha256,
  '{series_id}',jsonb_build_object('fixture_label','{label}'),
  '{evidence_hash('candidate')}',intent.release_sha,intent.decision_at
from private.order_intents as intent
join private.risk_results as risk on risk.id=intent.risk_result_id
where intent.id='{intent_id}';
""")


def jwt_claim_sql(user_id: str, *, role: str = "authenticated", totp: bool = True) -> str:
    method = "totp" if totp else "password"
    return f"""
select set_config('request.jwt.claim.sub', '{user_id}', false);
select set_config('request.jwt.claim.role', '{role}', false);
select set_config('request.jwt.claim.aal', 'aal2', false);
select set_config(
  'request.jwt.claims',
  jsonb_build_object(
    'sub', '{user_id}', 'role', '{role}', 'aal', 'aal2',
    'session_id', 'session-{user_id}',
    'amr', jsonb_build_array(jsonb_build_object(
      'method', '{method}', 'timestamp', extract(epoch from clock_timestamp())
    ))
  )::text,
  false
);
set role {role};
"""


def verify_catalog(container: str) -> None:
    extension_boundary = psql(container, """
select concat_ws('|',
  (select n.nspname
   from pg_extension e
   join pg_namespace n on n.oid = e.extnamespace
   where e.extname = 'pgcrypto'),
  to_regprocedure('extensions.digest(bytea,text)') is not null,
  to_regprocedure('extensions.digest(text,text)') is not null,
  to_regprocedure('public.digest(bytea,text)') is null,
  to_regprocedure('public.digest(text,text)') is null
);
""").stdout.strip()
    if extension_boundary != "extensions|t|t|t|t":
        raise VerificationError(
            f"pgcrypto extension boundary mismatch: {extension_boundary}"
        )

    result = psql(container, """
select concat_ws('|',
  (select count(*) from pg_proc p join pg_namespace n on n.oid=p.pronamespace
    where n.nspname='worker_api'),
  (select count(*) from pg_proc p join pg_namespace n on n.oid=p.pronamespace
    where n.nspname in ('api','worker_api') and p.prosecdef),
  (select count(*) from information_schema.role_table_grants
    where table_schema in ('private','public')
      and grantee in ('anon','authenticated','service_role')),
  (select string_agg(n.nspname||'.'||c.relname, ',' order by n.nspname,c.relname)
   from pg_publication_rel pr join pg_publication pub on pub.oid=pr.prpubid
   join pg_class c on c.oid=pr.prrelid join pg_namespace n on n.oid=c.relnamespace
   where pub.pubname='supabase_realtime'),
  (select count(*)
   from pg_proc p join pg_namespace n on n.oid=p.pronamespace
   cross join lateral aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
   where n.nspname='private' and a.grantee=0 and a.privilege_type='EXECUTE'),
  (select count(*) from pg_proc p join pg_namespace n on n.oid=p.pronamespace
   where n.nspname='private' and has_function_privilege('anon',p.oid,'EXECUTE')),
  (select count(*) from pg_proc p join pg_namespace n on n.oid=p.pronamespace
   where n.nspname='private' and has_function_privilege('authenticated',p.oid,'EXECUTE')),
  (select count(*) from pg_proc p join pg_namespace n on n.oid=p.pronamespace
   where n.nspname='private' and has_function_privilege('service_role',p.oid,'EXECUTE')),
  has_function_privilege(
    'authenticated',
    'private.quarantine_execution_observation(uuid,uuid,integer,text,text,text,text,text,timestamptz,text)',
    'EXECUTE'
  ),
  has_function_privilege(
    'service_role',
    'private.quarantine_execution_observation(uuid,uuid,integer,text,text,text,text,text,timestamptz,text)',
    'EXECUTE'
  ),
  to_regprocedure(
    'worker_api.claim_paper_execution_v1(text,uuid,text,timestamptz,integer)'
  ) is not null,
  to_regprocedure(
    'worker_api.claim_cash_settlement_batch(text,text,text,bigint,timestamptz,integer)'
  ) is not null,
  to_regprocedure(
    'worker_api.complete_cash_settlement(uuid,bigint,uuid,text,text,bigint,timestamptz)'
  ) is not null,
  to_regprocedure(
    'worker_api.fail_cash_settlement_attempt(uuid,bigint,uuid,text,text,bigint,timestamptz,text)'
  ) is not null,
  to_regprocedure(
    'worker_api.claim_unknown_resolution_v2(uuid,text,text,bigint,bigint,bigint,timestamptz)'
  ) is not null,
  to_regprocedure(
    'worker_api.apply_unknown_resolution_v2(uuid,uuid,text,text,bigint,bigint,bigint,bigint,timestamptz)'
  ) is not null,
  to_regprocedure('api.request_unknown_resolution_v2(jsonb)') is not null,
  to_regprocedure('api.review_unknown_resolution_v2(jsonb)') is not null,
  to_regclass('private.paper_execution_work_items') is not null,
  to_regclass('private.cash_settlement_obligations') is not null,
  to_regclass('private.unknown_execution_resolution_applications_v2') is not null,
  to_regprocedure(
    'worker_api.claim_operation_command_batch(text,text,text,bigint,timestamptz,integer)'
  ) is not null,
  to_regprocedure(
    'worker_api.acknowledge_operation_command(uuid,text,text,text,text,bigint,bigint,timestamptz,jsonb,text)'
  ) is not null,
  to_regprocedure(
    'worker_api.claim_execution_reconciliation_batch(text,text,text,bigint,timestamptz,integer,integer,uuid,integer)'
  ) is not null,
  not has_function_privilege(
    'service_role',
    'worker_api.claim_operation_command_batch(text,text,timestamptz,integer)',
    'EXECUTE'
  ),
  not has_function_privilege(
    'service_role',
    'worker_api.acknowledge_operation_command(uuid,text,text,text,timestamptz,jsonb,text)',
    'EXECUTE'
  ),
  not has_function_privilege(
    'service_role',
    'worker_api.claim_execution_reconciliation_batch(text,text,timestamptz,integer,integer,uuid,integer)',
    'EXECUTE'
  )
);
""").stdout.strip()
    parts = result.split("|")
    if len(parts) != 27:
        raise VerificationError(f"catalog boundary shape mismatch: {result}")
    worker_rpc_count = int(parts[0])
    stable_boundary = parts[1:6]
    quarantine_acl = parts[8:10]
    explicit_contracts = parts[10:]
    if (
        worker_rpc_count < 35
        or stable_boundary != ["0", "0", "api.control_plane_signal", "0", "0"]
        or quarantine_acl != ["f", "f"]
        or explicit_contracts != ["t"] * len(explicit_contracts)
    ):
        raise VerificationError(f"catalog boundary mismatch: {result}")
    print(
        "PASS extensions-owned pgcrypto, catalog/API allowlists, explicit "
        "source/settlement/unknown contracts, private definer ACLs and "
        "signal-only Realtime"
    )


def verify_strict_auth(container: str) -> None:
    draft = f"jsonb_build_object('schema_version',1,'request_id','{ACCESS_REQUEST}'," \
        f"'subject_user_id','{SUBJECT}','requested_role','viewer','change_type','grant'," \
        f"'evidence_id','{EVIDENCE}','reason_code','role_required'," \
        "'requested_at',clock_timestamp(),'expires_at',clock_timestamp()+interval '1 hour')"
    expect_failure(
        container,
        jwt_claim_sql(ADMIN_1, totp=False)
        + f"select api.issue_access_step_up_grant_v1(jsonb_build_object(" \
          f"'schema_version',1,'bound_action','request','access_change_payload',{draft}));",
        "recent_totp_verification_required",
    )
    expect_failure(
        container,
        jwt_claim_sql(ADMIN_1)
        + f"select api.issue_access_step_up_grant_v1(jsonb_build_object(" \
          f"'schema_version','1','bound_action','request','access_change_payload',{draft}));",
        "access_step_up_request_schema_invalid",
    )
    boolean_sql = jwt_claim_sql(ADMIN_1) + f"""
with draft(value) as (select {draft}),
grant_value(value) as (
  select api.issue_access_step_up_grant_v1(jsonb_build_object(
    'schema_version',1,'bound_action','request','access_change_payload',draft.value
  )) from draft
)
select api.request_access_change_v1(
  draft.value || grant_value.value || jsonb_build_object('step_up_grant_one_time','true')
) from draft, grant_value;
"""
    expect_failure(container, boolean_sql, "access_change_request_json_type_invalid")
    revision_draft = "jsonb_build_object('schema_version',1,'review_id',gen_random_uuid()," \
        "'command_id',gen_random_uuid(),'command_type','pause_paper'," \
        "'reviewer_role','risk_approver','decision','reject'," \
        "'reason_code','evidence_incomplete','expected_receipt_revision','0'," \
        "'reviewed_at',clock_timestamp())"
    expect_failure(
        container,
        jwt_claim_sql(RISK)
        + f"select api.issue_step_up_grant_v1(jsonb_build_object(" \
          f"'schema_version',1,'bound_action','review','bound_command_type','pause_paper'," \
          f"'command_payload',{revision_draft}));",
        "step_up_draft_json_type_invalid",
    )
    print("PASS recent TOTP and strict numeric/boolean JSON types")


def verify_access_maker_checker(container: str) -> None:
    request_draft = f"jsonb_build_object('schema_version',1,'request_id','{ACCESS_REQUEST}'," \
        f"'subject_user_id','{SUBJECT}','requested_role','viewer','change_type','grant'," \
        f"'evidence_id','{EVIDENCE}','reason_code','role_required'," \
        "'requested_at',clock_timestamp(),'expires_at',clock_timestamp()+interval '1 hour')"
    result = psql(container, jwt_claim_sql(ADMIN_1) + f"""
with draft(value) as (select {request_draft}),
grant_value(value) as (
  select api.issue_access_step_up_grant_v1(jsonb_build_object(
    'schema_version',1,'bound_action','request','access_change_payload',draft.value
  )) from draft
)
select api.request_access_change_v1(draft.value || grant_value.value)->>'state'
from draft, grant_value;
""").stdout.strip().splitlines()[-1]
    if result != "requested":
        raise VerificationError(f"access request state mismatch: {result}")
    review_draft = f"jsonb_build_object('schema_version',1,'review_id','{ACCESS_REVIEW}'," \
        f"'request_id','{ACCESS_REQUEST}','decision','approve'," \
        "'reason_code','policy_satisfied','expected_state','requested'," \
        "'reviewed_at',clock_timestamp())"
    result = psql(container, jwt_claim_sql(ADMIN_2) + f"""
with draft(value) as (select {review_draft}),
grant_value(value) as (
  select api.issue_access_step_up_grant_v1(jsonb_build_object(
    'schema_version',1,'bound_action','review','access_change_payload',draft.value
  )) from draft
)
select api.review_access_change_v1(draft.value || grant_value.value)->>'state'
from draft, grant_value;
""").stdout.strip().splitlines()[-1]
    if result != "applied":
        raise VerificationError(f"access review state mismatch: {result}")
    state = psql(container, f"""
select concat_ws('|',
  (select count(*) from private.role_assignments where user_id='{SUBJECT}' and role='viewer' and revoked_at is null),
  (select count(*) from private.step_up_grants where consumed_at is not null),
  (select count(*) from private.audit_events where resource_id='{ACCESS_REQUEST}'),
  (select count(*) from private.delivery_outbox where aggregate_id='{ACCESS_REQUEST}')
);
""").stdout.strip()
    if state != "1|2|2|2":
        raise VerificationError(f"access maker-checker evidence mismatch: {state}")
    print("PASS access maker-checker, one-time grants, audit and outbox")


def verify_account_opening(container: str) -> None:
    draft = f"jsonb_build_object('schema_version',1,'request_id','{ACCOUNT_COMMAND}'," \
        "'environment','paper','account_id','paper-primary','opening_capital_krw',10000000," \
        f"'idempotency_key','77777777-7777-4777-8777-777777777777'," \
        "'requested_at',clock_timestamp(),'expires_at',clock_timestamp()+interval '1 hour'," \
        f"'command_type','account_opening','evidence_id','{EVIDENCE}'," \
        "'reason_code','approved_account_opening')"
    psql(container, jwt_claim_sql(OPERATOR) + f"""
with draft(value) as (select {draft}), grant_value(value) as (
  select api.issue_account_opening_step_up_grant_v1(jsonb_build_object(
    'schema_version',1,'bound_action','request','bound_command_type','account_opening',
    'command_payload',draft.value)) from draft)
select api.request_account_opening_v1(draft.value || grant_value.value)
from draft, grant_value;
""")
    bootstrap_probe = "87878787-8787-4787-8787-878787878787"
    expect_failure(
        container,
        jwt_claim_sql(bootstrap_probe, role="service_role") + f"""
select * from worker_api.acquire_worker_lease(
  'paper-primary','{bootstrap_probe}',clock_timestamp(),120,'{RELEASE_SHA}'
);
""",
        "open_trading_account_required",
    )
    review = f"jsonb_build_object('schema_version',1,'review_id',gen_random_uuid()," \
        f"'command_id','{ACCOUNT_COMMAND}','command_type','account_opening'," \
        "'reviewer_role','risk_approver','decision','approve'," \
        "'reason_code','policy_satisfied','expected_receipt_revision',0," \
        "'reviewed_at',clock_timestamp())"
    psql(container, jwt_claim_sql(RISK) + f"""
with draft(value) as (select {review}), grant_value(value) as (
  select api.issue_account_opening_step_up_grant_v1(jsonb_build_object(
    'schema_version',1,'bound_action','review','bound_command_type','account_opening',
    'command_payload',draft.value)) from draft)
select api.review_account_opening_v1(draft.value || grant_value.value)
from draft, grant_value;
""")
    holder = "88888888-8888-4888-8888-888888888888"
    result = psql(container, jwt_claim_sql(holder, role="service_role") + f"""
create temp table account_opening_lease as
select fencing_token from worker_api.acquire_worker_lease(
  'paper-primary','{holder}',clock_timestamp(),120,'{RELEASE_SHA}'
);
create temp table account_opening_claim as
select * from worker_api.claim_operation_command_batch(
  'paper-primary','{holder}','{RELEASE_SHA}',
  (select fencing_token from account_opening_lease),clock_timestamp(),25
);
select command_type from account_opening_claim;
select state from worker_api.acknowledge_operation_command(
  '{ACCOUNT_COMMAND}','applied','paper-primary','{holder}','{RELEASE_SHA}',
  (select fencing_token from account_opening_lease),
  (select revision from account_opening_claim where command_id='{ACCOUNT_COMMAND}'),
  clock_timestamp(),
  '{{}}'::jsonb,null);
select idempotent from worker_api.release_worker_lease(
  'paper-primary','{holder}',(select fencing_token from account_opening_lease),
  clock_timestamp(),'{RELEASE_SHA}'
);
reset role;
select concat_ws('|',
  (select state from private.trading_accounts where account_id='paper-primary'),
  (select count(*) from private.accounting_transactions where control_command_id='{ACCOUNT_COMMAND}'),
  (select count(*) from private.accounting_postings p join private.accounting_transactions t
    on t.id=p.journal_entry_id where t.control_command_id='{ACCOUNT_COMMAND}'),
  (select settled_cash_krw::bigint from private.cash_balance_projection where account_id='paper-primary')
);
""").stdout.strip().splitlines()
    if result[-4:] != ["account_opening", "applied", "f", "open|1|2|10000000"]:
        raise VerificationError(f"account opening mismatch: {result}")
    print("PASS account opening request/review/claim/exact-once journal")


def verify_lease_and_outbox(container: str) -> None:
    holder = "99999999-9999-4999-8999-999999999999"
    other = "aaaaaaaa-0000-4000-8000-000000000000"
    result = psql(container, jwt_claim_sql(holder, role="service_role") + f"""
select heartbeat_id is not null from worker_api.record_worker_heartbeat(
 '{holder}','ok',jsonb_build_object(
   'release_sha','{RELEASE_SHA}','mock_providers',true,
   'component','operations_v2',
   'checkpoint','operations_completed','completed_at',clock_timestamp()
 ),
 clock_timestamp(),'{RELEASE_SHA}');
select fencing_token from worker_api.acquire_worker_lease(
 'paper-primary','{holder}',clock_timestamp(),30,'{RELEASE_SHA}');
""").stdout.strip().splitlines()[-2:]
    token = result[-1]
    releases = psql(container, jwt_claim_sql(holder, role="service_role") + f"""
select idempotent from worker_api.release_worker_lease(
 'paper-primary','{holder}',{token},clock_timestamp(),'{RELEASE_SHA}');
select idempotent from worker_api.release_worker_lease(
 'paper-primary','{holder}',{token},clock_timestamp(),'{RELEASE_SHA}');
""").stdout.strip().splitlines()[-2:]
    if result[0] != "t" or releases != ["f", "t"]:
        raise VerificationError(f"lease release mismatch: {result}")
    expect_failure(
        container,
        jwt_claim_sql(other, role="service_role")
        + f"select * from worker_api.release_worker_lease(" \
          f"'paper-primary','{other}',{token},clock_timestamp(),'{RELEASE_SHA}');",
        "worker_lease_release_identity_conflict",
    )
    outbox = psql(container, f"""
insert into private.delivery_outbox (
 event_type,aggregate_type,aggregate_id,dedupe_key,payload,destination_type,available_at
) values (
 'verification','verification','one','stable-dedupe-key','{{}}','operations_metric',
 clock_timestamp()-interval '1 day'
);
{jwt_claim_sql(holder, role='service_role')}
create temp table first_claim as select * from worker_api.claim_delivery_outbox(
 '{holder}',clock_timestamp(),1,5);
create temp table second_claim as select * from worker_api.claim_delivery_outbox(
 '{other}',clock_timestamp()+interval '6 seconds',1,5);
reset role;
update private.delivery_outbox
set lease_expires_at=clock_timestamp()-interval '1 second'
where id=(select outbox_id from first_claim);
set role service_role;
create temp table reclaimed_claim as select * from worker_api.claim_delivery_outbox(
 '{other}',clock_timestamp(),1,5);
select concat_ws('|',
  (select dedupe_key from first_claim),
  (select count(*) from second_claim where dedupe_key='stable-dedupe-key'),
  (select dedupe_key from reclaimed_claim)
);
select status from worker_api.complete_outbox_delivery(
  (select outbox_id from reclaimed_claim),'{other}',
  (select lease_token from reclaimed_claim),clock_timestamp(),
  'receiver-validation','{'a' * 64}'
);
reset role;
insert into private.delivery_outbox (
 event_type,aggregate_type,aggregate_id,dedupe_key,payload,destination_type,available_at
) values (
 'verification','verification','two','failure-dedupe-key','{{}}','operations_metric',
 clock_timestamp()-interval '1 day'
);
set role service_role;
create temp table failure_claim as select * from worker_api.claim_delivery_outbox(
 '{holder}',clock_timestamp(),1,30);
select status from worker_api.fail_outbox_delivery(
  (select outbox_id from failure_claim),'{holder}',
  (select lease_token from failure_claim),clock_timestamp(),
  'receiver_unavailable',5
);
reset role;
select concat_ws('|',
  (select status from private.delivery_outbox where dedupe_key='stable-dedupe-key'),
  (select status from private.delivery_outbox where dedupe_key='failure-dedupe-key'),
  (select count(*) from private.delivery_outbox
    where dedupe_key='failure-dedupe-key' and available_at > clock_timestamp())
);
""").stdout.strip().splitlines()[-4:]
    if outbox != [
        "stable-dedupe-key|0|stable-dedupe-key",
        "delivered",
        "pending",
        "delivered|pending|1",
    ]:
        raise VerificationError(f"outbox DB-clock/ACK mismatch: {outbox}")

    aba = psql(container, f"""
insert into private.delivery_outbox (
 event_type,aggregate_type,aggregate_id,dedupe_key,payload,destination_type,
 available_at
) values (
 'verification','verification','aba','aba-dedupe-key','{{}}','operations_metric',
 clock_timestamp()-interval '3 days'
);
{jwt_claim_sql(holder, role='service_role')}
select concat_ws('|',outbox_id,lease_token)
from worker_api.claim_delivery_outbox('{holder}',clock_timestamp(),1,5)
where dedupe_key='aba-dedupe-key';
""").stdout.strip().splitlines()[-1].split("|")
    aba_id, first_lease_token = aba
    psql(container, f"""
reset role;
update private.delivery_outbox
set lease_expires_at=clock_timestamp()-interval '1 second'
where id='{aba_id}';
{jwt_claim_sql(holder, role='service_role')}
select concat_ws('|',outbox_id,lease_token)
from worker_api.claim_delivery_outbox('{holder}',clock_timestamp(),1,30)
where outbox_id='{aba_id}';
""")
    second_lease_token = psql(container, f"""
select lease_token from private.delivery_outbox where id='{aba_id}';
""").stdout.strip()
    if first_lease_token == second_lease_token:
        raise VerificationError("outbox reclaim reused the prior lease token")
    expect_failure(
        container,
        jwt_claim_sql(holder, role="service_role") + f"""
select * from worker_api.complete_outbox_delivery(
  '{aba_id}','{holder}','{first_lease_token}',clock_timestamp(),
  'stale-receipt','{'b' * 64}'
);
""",
        "outbox_lease_not_owned_current_or_expired",
    )
    expect_failure(
        container,
        jwt_claim_sql(holder, role="service_role") + f"""
select * from worker_api.fail_outbox_delivery(
  '{aba_id}','{holder}','{first_lease_token}',clock_timestamp(),
  'stale_attempt',5
);
""",
        "outbox_lease_not_owned_current_or_expired",
    )
    status = psql(container, jwt_claim_sql(holder, role="service_role") + f"""
select status from worker_api.complete_outbox_delivery(
  '{aba_id}','{holder}','{second_lease_token}',clock_timestamp(),
  'current-receipt','{'c' * 64}'
);
""").stdout.strip().splitlines()[-1]
    if status != "delivered":
        raise VerificationError(f"current outbox attempt did not complete: {status}")

    expect_failure(
        container,
        jwt_claim_sql(holder, role="service_role") + f"""
select * from worker_api.claim_delivery_outbox(
  '{holder}',clock_timestamp(),null,30
);
""",
        "outbox_claim_parameters_invalid",
    )
    expect_failure(
        container,
        jwt_claim_sql(holder, role="service_role") + f"""
select * from worker_api.complete_outbox_delivery(
  '{aba_id}','{holder}',null::uuid,clock_timestamp(),
  'missing-token','{'d' * 64}'
);
""",
        "outbox_receipt_invalid",
    )
    expect_failure(
        container,
        jwt_claim_sql(holder, role="service_role") + f"""
select * from worker_api.fail_outbox_delivery(
  '{aba_id}','{holder}',null::uuid,clock_timestamp(),'missing_token',5
);
""",
        "outbox_failure_parameters_invalid",
    )
    expect_failure(
        container,
        jwt_claim_sql(holder, role="service_role") + f"""
select * from worker_api.complete_outbox_delivery(
  '{aba_id}','{holder}',clock_timestamp(),'legacy-receipt','{'e' * 64}'
);
""",
        "worker_upgrade_required",
    )
    expect_failure(
        container,
        jwt_claim_sql(holder, role="service_role") + f"""
select * from worker_api.fail_outbox_delivery(
  '{aba_id}','{holder}',clock_timestamp(),'legacy_failure',5
);
""",
        "worker_upgrade_required",
    )

    final_attempt = psql(container, f"""
reset role;
insert into private.delivery_outbox (
 event_type,aggregate_type,aggregate_id,dedupe_key,payload,destination_type,
 available_at,max_attempts
) values (
 'verification','verification','final-crash','final-crash-dedupe-key',
 '{{}}','operations_metric',clock_timestamp()-interval '4 days',1
);
{jwt_claim_sql(other, role='service_role')}
select outbox_id from worker_api.claim_delivery_outbox(
 '{other}',clock_timestamp(),1,5
) where dedupe_key='final-crash-dedupe-key';
""").stdout.strip().splitlines()[-1]
    final_state = psql(container, f"""
reset role;
update private.delivery_outbox
set lease_expires_at=clock_timestamp()-interval '1 second'
where id='{final_attempt}';
{jwt_claim_sql(other, role='service_role')}
select count(*) from worker_api.claim_delivery_outbox(
 '{other}',clock_timestamp(),1,30
);
reset role;
select concat_ws('|',
  (select status from private.delivery_outbox where id='{final_attempt}'),
  (select last_error_code from private.delivery_outbox where id='{final_attempt}'),
  (select count(*) from private.incidents
    where incident_type='delivery_dead_letter'
      and correlation_id='{final_attempt}')
);
""").stdout.strip().splitlines()[-1]
    if final_state != (
        "dead_letter|delivery_attempt_lease_expired_at_max_attempts|1"
    ):
        raise VerificationError(f"final-attempt crash mismatch: {final_state}")
    print(
        "PASS lease CAS, outbox attempt fencing, NULL rejection and "
        "final-crash dead letter"
    )


def verify_command_claim_allowlist(container: str) -> None:
    holder = "abababab-abab-4bab-8bab-abababababab"
    psql(container, f"""
insert into private.operation_commands (
  command_type,state,requested_change,revision,requester_user_id,
  reviewer_user_id,requested_at,reviewed_at,expires_at,idempotency_key
)
select 'unknown_resolution','approved',
  jsonb_build_object('account_id','paper-primary','environment','paper'),1,
  '{OPERATOR}','{RISK}',clock_timestamp()-interval '1 minute',clock_timestamp(),
  clock_timestamp()+interval '1 hour','unsupported-'||value::text
from generate_series(1,30) as value;
insert into private.operation_commands (
  command_type,state,requested_change,revision,requester_user_id,
  reviewer_user_id,requested_at,reviewed_at,expires_at,idempotency_key
) values (
  'pause_paper','approved',
  jsonb_build_object('account_id','paper-primary','environment','paper',
    'expected_state_version',1,'reason_code','operator_pause'),1,
  '{OPERATOR}','{RISK}',clock_timestamp(),clock_timestamp(),
  clock_timestamp()+interval '1 hour','supported-after-unsupported'
);
""")
    result = psql(container, jwt_claim_sql(holder, role="service_role") + f"""
create temp table allowlist_lease as
select fencing_token from worker_api.acquire_worker_lease(
  'paper-primary','{holder}',clock_timestamp(),120,'{RELEASE_SHA}'
);
select command_type from worker_api.claim_operation_command_batch(
  'paper-primary','{holder}','{RELEASE_SHA}',
  (select fencing_token from allowlist_lease),clock_timestamp(),25
);
select idempotent from worker_api.release_worker_lease(
  'paper-primary','{holder}',(select fencing_token from allowlist_lease),
  clock_timestamp(),'{RELEASE_SHA}'
);
reset role;
select count(*) from private.operation_commands
where command_type='unknown_resolution' and state='approved';
""").stdout.strip().splitlines()[-3:]
    if result[-3:] != ["pause_paper", "f", "30"]:
        raise VerificationError(f"unsupported command queue starvation: {result}")
    print("PASS worker claim excludes unsupported commands before LIMIT")


def verify_command_ack_expiry(container: str) -> None:
    command_id = "acacacac-acac-4cac-8cac-acacacacacac"
    worker = "adadadad-adad-4dad-8dad-adadadadadad"
    expect_failure(
        container,
        f"""
insert into private.operation_commands (
  id,command_type,state,requested_change,revision,requester_user_id,
  reviewer_user_id,requested_at,reviewed_at,expires_at,idempotency_key
) values (
  '{command_id}','pause_paper','approved',
  jsonb_build_object('account_id','paper-primary','environment','paper',
    'expected_state_version',2,'reason_code','operator_pause'),1,
  '{OPERATOR}','{RISK}',clock_timestamp(),clock_timestamp(),
  clock_timestamp()+interval '500 milliseconds','ack-expiry-regression'
);
""" + jwt_claim_sql(worker, role="service_role") + f"""
create temp table ack_expiry_lease as
select fencing_token from worker_api.acquire_worker_lease(
  'paper-primary','{worker}',clock_timestamp(),120,'{RELEASE_SHA}'
);
create temp table ack_expiry_claim as
select * from worker_api.claim_operation_command_batch(
  'paper-primary','{worker}','{RELEASE_SHA}',
  (select fencing_token from ack_expiry_lease),clock_timestamp(),25
);
select pg_sleep(0.7);
select state from worker_api.acknowledge_operation_command(
  '{command_id}','applied','paper-primary','{worker}','{RELEASE_SHA}',
  (select fencing_token from ack_expiry_lease),
  (select revision from ack_expiry_claim where command_id='{command_id}'),
  clock_timestamp(),
  '{{}}'::jsonb,null
);
""",
        "operation_command_expired_before_application",
    )
    state = psql(container, f"""
select concat_ws('|',state,claimed_by_service,applied_at is null)
from private.operation_commands where id='{command_id}';
""").stdout.strip()
    if state != f"claimed|{worker}|t":
        raise VerificationError(f"expired ACK mutated command: {state}")
    ack_expiry_token = psql(container, f"""
select fencing_token from private.worker_leases
where account_id='paper-primary' and holder_id='{worker}';
""").stdout.strip()
    psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select idempotent from worker_api.release_worker_lease(
  'paper-primary','{worker}',{ack_expiry_token},
  clock_timestamp(),'{RELEASE_SHA}'
);
""")
    print("PASS applied ACK rechecks command expiry against database time")


def verify_command_claim_generation_fencing(container: str) -> None:
    command_id = "aeaeaeae-aeae-4eae-8eae-aeaeaeaeaeae"
    worker = "afafafaf-afaf-4faf-8faf-afafafafafaf"
    first = psql(container, f"""
insert into private.operation_commands (
  id,command_type,state,requested_change,revision,requester_user_id,
  reviewer_user_id,requested_at,reviewed_at,expires_at,idempotency_key
)
select
  '{command_id}','pause_paper','approved',
  jsonb_build_object(
    'account_id','paper-primary','environment','paper',
    'expected_state_version',control_epoch,'reason_code','claim_generation_test'
  ),
  1,'{OPERATOR}','{RISK}',clock_timestamp(),clock_timestamp(),
  clock_timestamp()+interval '1 hour','claim-generation-fencing'
from private.execution_controls where account_id='paper-primary';
{jwt_claim_sql(worker, role='service_role')}
create temp table command_generation_lease as
select fencing_token from worker_api.acquire_worker_lease(
  'paper-primary','{worker}',clock_timestamp(),120,'{RELEASE_SHA}'
);
create temp table first_command_generation as
select * from worker_api.claim_operation_command_batch(
  'paper-primary','{worker}','{RELEASE_SHA}',
  (select fencing_token from command_generation_lease),clock_timestamp(),25
);
select concat_ws('|',
  (select fencing_token from command_generation_lease),
  (select revision from first_command_generation where command_id='{command_id}')
);
""").stdout.strip().splitlines()[-1].split("|")
    fencing_token, first_revision = (int(value) for value in first)
    second_revision = int(psql(container, f"""
update private.operation_commands
set claim_expires_at=claimed_at+interval '1 microsecond'
where id='{command_id}';
{jwt_claim_sql(worker, role='service_role')}
select revision from worker_api.claim_operation_command_batch(
  'paper-primary','{worker}','{RELEASE_SHA}',{fencing_token},
  clock_timestamp(),25
)
where command_id='{command_id}';
""").stdout.strip().splitlines()[-1])
    if second_revision != first_revision + 1:
        raise VerificationError(
            "operation command reclaim did not advance claim revision"
        )
    expect_failure(
        container,
        jwt_claim_sql(worker, role="service_role") + f"""
select * from worker_api.acknowledge_operation_command(
  '{command_id}','failed','paper-primary','{worker}','{RELEASE_SHA}',
  {fencing_token},{first_revision},clock_timestamp(),
  '{{}}'::jsonb,'stale_claim_generation'
);
""",
        "operation_command_claim_generation_stale",
    )
    state = psql(container, f"""
select concat_ws('|',state,revision,claim_fencing_token,claim_release_sha)
from private.operation_commands where id='{command_id}';
""").stdout.strip()
    if state != f"claimed|{second_revision}|{fencing_token}|{RELEASE_SHA}":
        raise VerificationError(f"stale command ACK mutated claim: {state}")

    psql(container, f"""
update private.operation_commands
set claim_release_sha=null, claim_fencing_token=null
where id='{command_id}';
""")
    expect_failure(
        container,
        jwt_claim_sql(worker, role="service_role") + f"""
select * from worker_api.acknowledge_operation_command(
  '{command_id}','failed','paper-primary','{worker}','{RELEASE_SHA}',
  {fencing_token},{second_revision},clock_timestamp(),
  '{{}}'::jsonb,'legacy_tokenless_claim'
);
""",
        "operation_command_claim_generation_stale",
    )
    tokenless_state = psql(container, f"""
select concat_ws('|',state,revision,claim_release_sha is null,
  claim_fencing_token is null)
from private.operation_commands where id='{command_id}';
""").stdout.strip()
    if tokenless_state != f"claimed|{second_revision}|t|t":
        raise VerificationError(
            f"tokenless legacy command ACK mutated claim: {tokenless_state}"
        )
    psql(container, f"""
update private.operation_commands
set claim_release_sha='{RELEASE_SHA}', claim_fencing_token={fencing_token}
where id='{command_id}';
""")

    contract_lease_before = psql(container, """
select count(*) from private.worker_leases
where account_id='contract-test-primary';
""").stdout.strip()
    expect_failure(
        container,
        jwt_claim_sql(worker, role="service_role") + f"""
select * from worker_api.claim_operation_command_batch(
  'contract-test-primary','{worker}','{RELEASE_SHA}',{fencing_token},
  clock_timestamp(),25
);
""",
        "operation_command_worker_lease_stale_or_missing",
    )
    contract_lease_after = psql(container, """
select count(*) from private.worker_leases
where account_id='contract-test-primary';
""").stdout.strip()
    if contract_lease_before != contract_lease_after:
        raise VerificationError("cross-account command claim created a worker lease")

    terminal = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select state from worker_api.acknowledge_operation_command(
  '{command_id}','failed','paper-primary','{worker}','{RELEASE_SHA}',
  {fencing_token},{second_revision},clock_timestamp(),
  '{{}}'::jsonb,'claim_generation_test_complete'
);
select idempotent from worker_api.release_worker_lease(
  'paper-primary','{worker}',{fencing_token},clock_timestamp(),'{RELEASE_SHA}'
);
""").stdout.strip().splitlines()[-2:]
    if terminal != ["failed", "f"]:
        raise VerificationError(f"current command generation did not ACK: {terminal}")
    expect_failure(
        container,
        jwt_claim_sql(worker, role="service_role") + f"""
select * from worker_api.claim_operation_command_batch(
  'paper-primary','{worker}','{RELEASE_SHA}',{fencing_token},
  clock_timestamp(),25
);
""",
        "operation_command_worker_lease_stale_or_missing",
    )
    expect_failure(
        container,
        f"""
select * from worker_api.claim_operation_command_batch(
  '{worker}','{RELEASE_SHA}',clock_timestamp(),25
);
""",
        "worker_upgrade_required",
    )
    expect_failure(
        container,
        f"""
select * from worker_api.acknowledge_operation_command(
  '{command_id}','failed','{worker}','{RELEASE_SHA}',clock_timestamp(),
  '{{}}'::jsonb,'legacy_ack'
);
""",
        "worker_upgrade_required",
    )
    print(
        "PASS command claim account/lease generation, revision CAS and legacy "
        "overloads fail closed"
    )


def verify_qualification_expiry_at_application(container: str) -> None:
    qualification_id = "b0b0b0b0-b0b0-40b0-80b0-b0b0b0b0b0b0"
    command_id = "b1b1b1b1-b1b1-41b1-81b1-b1b1b1b1b1b1"
    risk_policy_version = "b2b2b2b2-b2b2-42b2-82b2-b2b2b2b2b2b2"
    worker = "b3b3b3b3-b3b3-43b3-83b3-b3b3b3b3b3b3"
    strategy = psql(
        container,
        "select id from public.strategy_versions order by created_at limit 1;",
    ).stdout.strip()
    expect_failure(
        container,
        f"""
insert into private.qualifications (
  id,environment,status,release_sha,ledger_checkpoint,dataset_version,
  execution_policy_version,execution_policy_sha256,risk_policy_sha256,
  strategy_version_id,risk_policy_version_id,valid_from,valid_until,
  g1_status,g1_checked_at,g1_evidence_id,g2_status,g2_checked_at,g2_evidence_id
)
select '{qualification_id}','paper','expired','{RELEASE_SHA}',
  'expired-checkpoint','expired-dataset',execution_policy_version,
  execution_policy_sha256,risk_policy_sha256,'{strategy}','{risk_policy_version}',
  clock_timestamp()-interval '3 minutes',clock_timestamp()-interval '1 minute',
  'pass',clock_timestamp()-interval '2 minutes','{EVIDENCE}',
  'pass',clock_timestamp()-interval '2 minutes','{EVIDENCE}'
from private.execution_controls where account_id='paper-primary';
alter table private.operation_commands
  disable trigger guard_operation_command_v1_qualification;
insert into private.operation_commands (
  id,command_type,state,requested_change,revision,evidence_id,target_release_sha,
  requester_user_id,reviewer_user_id,requested_at,reviewed_at,expires_at,
  idempotency_key
)
select '{command_id}','paper_resume','approved',jsonb_build_object(
  'account_id','paper-primary','environment','paper',
  'expected_state_version',control_epoch,
  'qualification_id','{qualification_id}',
  'strategy_version_id','{strategy}',
  'risk_policy_version_id','{risk_policy_version}',
  'release_sha','{RELEASE_SHA}','ledger_checkpoint','expired-checkpoint',
  'execution_policy_version',execution_policy_version,
  'execution_policy_sha256',execution_policy_sha256,
  'risk_policy_sha256',risk_policy_sha256,
  'reason_code','qualified_resume'
),1,'{EVIDENCE}','{RELEASE_SHA}','{OPERATOR}','{RISK}',
  clock_timestamp()-interval '2 minutes',clock_timestamp()-interval '90 seconds',
  clock_timestamp()+interval '1 hour','expired-qualification-at-apply'
from private.execution_controls where account_id='paper-primary';
alter table private.operation_commands
  enable trigger guard_operation_command_v1_qualification;
{jwt_claim_sql(worker, role='service_role')}
create temp table qualification_expiry_lease as
select fencing_token from worker_api.acquire_worker_lease(
  'paper-primary','{worker}',clock_timestamp(),120,'{RELEASE_SHA}'
);
create temp table qualification_expiry_claim as
select * from worker_api.claim_operation_command_batch(
  'paper-primary','{worker}','{RELEASE_SHA}',
  (select fencing_token from qualification_expiry_lease),clock_timestamp(),25
);
select command_id from qualification_expiry_claim where command_id='{command_id}';
select state from worker_api.acknowledge_operation_command(
  '{command_id}','applied','paper-primary','{worker}','{RELEASE_SHA}',
  (select fencing_token from qualification_expiry_lease),
  (select revision from qualification_expiry_claim where command_id='{command_id}'),
  clock_timestamp(),
  '{{}}'::jsonb,null
);
""",
        "qualified_evidence_bundle_expired_or_mismatched_at_application",
    )
    state = psql(container, f"""
select concat_ws('|',
  (select state from private.operation_commands where id='{command_id}'),
  (select execution_enabled from private.execution_controls
    where account_id='paper-primary'),
  (select control_epoch from private.execution_controls
    where account_id='paper-primary')
);
""").stdout.strip()
    if state != "claimed|f|1":
        raise VerificationError(f"expired qualification changed control: {state}")
    qualification_expiry_token = psql(container, f"""
select fencing_token from private.worker_leases
where account_id='paper-primary' and holder_id='{worker}';
""").stdout.strip()
    psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select idempotent from worker_api.release_worker_lease(
  'paper-primary','{worker}',{qualification_expiry_token},
  clock_timestamp(),'{RELEASE_SHA}'
);
""")

    print("PASS non-v1/expired qualification is rejected again at Worker apply")


def verify_qualification_suite_upgrade_retry(container: str) -> None:
    worker_id = "d1d1d1d1-d1d1-41d1-81d1-d1d1d1d1d1d1"
    reference_request_id = "d2d2d2d2-d2d2-42d2-82d2-d2d2d2d2d2d2"
    snapshot_id = "d3d3d3d3-d3d3-43d3-83d3-d3d3d3d3d3d3"
    snapshot_sequence = int(
        psql(
            container,
            """
select coalesce(max(sequence), 0) + 1
from private.account_snapshots
where account_id='contract-test-primary'
  and environment='contract_test';
""",
        ).stdout.strip()
    )

    def build_payload(run_id: str, suite_version: str, *, ledger_v2: bool) -> dict:
        check_ids = [
            "cancel_lifecycle",
            "create_lifecycle",
            "fault_injection",
            "production_order_network_zero",
            "status_partial_terminal",
        ]
        if ledger_v2:
            check_ids.append("ledger_invariants")
        checks = []
        for check_id in sorted(check_ids):
            metrics: dict[str, object] = {}
            if check_id == "production_order_network_zero":
                metrics = {"request_count": 0}
            elif check_id == "ledger_invariants":
                metrics = {
                    "balanced_transaction_count": 1,
                    "position_quantity": 1,
                    "provider_identity_change_blocked": True,
                    "projection_backed_by_journal": True,
                }
            checks.append(
                {
                    "check_id": check_id,
                    "status": "pass",
                    "evidence_sha256": hashlib.sha256(check_id.encode()).hexdigest(),
                    "metrics": metrics,
                }
            )
        base = {
            "schema_version": 1,
            "run_id": run_id,
            "run_kind": "contract_qualification",
            "account_id": "contract-test-primary",
            "environment": "contract_test",
            "reference_bundle_request_id": reference_request_id,
            "account_snapshot_id": snapshot_id,
            "account_snapshot_sequence": snapshot_sequence,
            "ledger_checkpoint_sha256": "4" * 64,
            "release_sha": RELEASE_SHA,
            "suite_version": suite_version,
            "started_at": "2026-07-14T09:00:00+00:00",
            "completed_at": "2026-07-14T09:01:00+00:00",
            "result": "pass",
            "evidence_manifest": {"schema_version": 1, "checks": checks},
            "worker_id": worker_id,
        }
        encoded = json.dumps(base, separators=(",", ":"), sort_keys=True)
        return json.loads(psql(container, f"""
with payload(value) as (select $payload${encoded}$payload$::jsonb)
select value || jsonb_build_object(
  'evidence_sha256',private.sha256_jsonb_v1(value)
)
from payload;
""").stdout.strip())

    legacy_payload = build_payload(
        "d4d4d4d4-d4d4-44d4-84d4-d4d4d4d4d4d4",
        "contract-test-qualification-v1",
        ledger_v2=False,
    )
    current_payload = build_payload(
        "d5d5d5d5-d5d5-45d5-85d5-d5d5d5d5d5d5",
        "contract-test-qualification-v2",
        ledger_v2=True,
    )

    def payload_literal(payload: dict) -> str:
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    # Reproduce an exact v1 row that was committed by the pre-upgrade RPC.  It
    # is deliberately injected with trigger replication disabled because only
    # an earlier deployed schema could have accepted that five-check suite.
    legacy_encoded = payload_literal(legacy_payload)
    psql(container, f"""
set session_replication_role=replica;
with payload(value) as (select $payload${legacy_encoded}$payload$::jsonb)
insert into private.qualification_runs (
  id,run_kind,account_id,environment,reference_bundle_request_id,
  account_snapshot_id,account_snapshot_sequence,ledger_checkpoint_sha256,
  release_sha,suite_version,result,evidence_manifest,evidence_sha256,
  started_at,completed_at,worker_id
)
select
  (value->>'run_id')::uuid,value->>'run_kind',value->>'account_id',
  value->>'environment',(value->>'reference_bundle_request_id')::uuid,
  (value->>'account_snapshot_id')::uuid,
  (value->>'account_snapshot_sequence')::bigint,
  value->>'ledger_checkpoint_sha256',value->>'release_sha',
  value->>'suite_version',value->>'result',value->'evidence_manifest',
  value->>'evidence_sha256',(value->>'started_at')::timestamptz,
  (value->>'completed_at')::timestamptz,(value->>'worker_id')::uuid
from payload;
set session_replication_role=origin;
""")

    # Build valid storage prerequisites, then exercise the v2 RPC as a true
    # first insertion.  This is distinct from the old-row exact retry above.
    psql(container, f"""
update private.trading_accounts
set state='open',opened_at=clock_timestamp(),closed_at=null
where account_id='contract-test-primary';
insert into private.market_calendars (
  id,environment,calendar_version,calendar_sha256,timezone_name,
  valid_from,valid_until,status,evidence_id,requested_by,reviewed_by
) values (
  'e1e1e1e1-e1e1-41e1-81e1-e1e1e1e1e1e1','contract_test',
  'qualification-v2-calendar','{'a' * 64}','Asia/Seoul',current_date-1,
  current_date+10,'approved','{CONTRACT_EVIDENCE}','{ADMIN_1}','{ADMIN_2}'
);
insert into private.paper_execution_model_registry (
  id,environment,model_version,tick_size_evidence_sha256,
  volume_model_evidence_sha256,corporate_action_evidence_sha256,
  market_calendar_id,status,evidence_id,requested_by,reviewed_by,
  effective_from,effective_until
) values (
  'e2e2e2e2-e2e2-42e2-82e2-e2e2e2e2e2e2','contract_test',
  'qualification-v2-model','{'b' * 64}','{'c' * 64}','{'d' * 64}',
  'e1e1e1e1-e1e1-41e1-81e1-e1e1e1e1e1e1','approved',
  '{CONTRACT_EVIDENCE}','{ADMIN_1}','{ADMIN_2}',
  clock_timestamp()-interval '1 day',clock_timestamp()+interval '1 day'
);
insert into private.paper_execution_policies (
  id,account_id,policy_version,policy_sha256,status,price_model,fill_model,
  parameters,evidence_id,requested_by,reviewed_by,effective_from,effective_until
) values (
  'e3e3e3e3-e3e3-43e3-83e3-e3e3e3e3e3e3','contract-test-primary',
  'qualification-v2-policy','{'e' * 64}','approved',
  'next_executable_minute_v1','whole_share_volume_bounded_v1','{{}}'::jsonb,
  '{CONTRACT_EVIDENCE}','{ADMIN_1}','{ADMIN_2}',
  clock_timestamp()-interval '1 day',clock_timestamp()+interval '1 day'
);
insert into private.execution_cost_schedules (
  id,account_id,schedule_version,schedule_sha256,buy_commission_rate,
  sell_commission_rate,sell_tax_rate,settlement_days,status,evidence_id,
  requested_by,reviewed_by,effective_from,effective_until
) values (
  'e4e4e4e4-e4e4-44e4-84e4-e4e4e4e4e4e4','contract-test-primary',
  'qualification-v2-cost','{'f' * 64}',0,0,0,0,'approved',
  '{CONTRACT_EVIDENCE}','{ADMIN_1}','{ADMIN_2}',
  clock_timestamp()-interval '1 day',clock_timestamp()+interval '1 day'
);
insert into private.control_approval_requests (
  id,request_kind,account_id,environment,payload,payload_sha256,
  requester_user_id,requested_at,expires_at,idempotency_key
) values (
  '{reference_request_id}','reference_bundle','contract-test-primary',
  'contract_test',jsonb_build_object('release_sha','{RELEASE_SHA}'),
  '{'1' * 64}','{ADMIN_1}',clock_timestamp()-interval '2 minutes',
  clock_timestamp()+interval '1 hour',
  'e5e5e5e5-e5e5-45e5-85e5-e5e5e5e5e5e5'
);
insert into private.control_approval_reviews (
  id,request_id,decision,payload,payload_sha256,
  expected_request_payload_sha256,reviewer_user_id,reviewed_at
) values (
  'e6e6e6e6-e6e6-46e6-86e6-e6e6e6e6e6e6','{reference_request_id}',
  'approved','{{}}'::jsonb,'{'2' * 64}','{'1' * 64}','{ADMIN_2}',
  clock_timestamp()-interval '1 minute'
);
insert into private.reference_bundle_materializations (
  request_id,review_id,evidence_id,policy_id,cost_schedule_id,calendar_id,
  execution_model_id,provider_contract_id,execution_model_sha256,
  provider_contract_sha256,bundle_sha256
) values (
  '{reference_request_id}','e6e6e6e6-e6e6-46e6-86e6-e6e6e6e6e6e6',
  '{CONTRACT_EVIDENCE}','e3e3e3e3-e3e3-43e3-83e3-e3e3e3e3e3e3',
  'e4e4e4e4-e4e4-44e4-84e4-e4e4e4e4e4e4',
  'e1e1e1e1-e1e1-41e1-81e1-e1e1e1e1e1e1',
  'e2e2e2e2-e2e2-42e2-82e2-e2e2e2e2e2e2',
  (select id from private.provider_contract_registry
    where provider='toss' and qualification_environment='contract_test'
    order by created_at limit 1),
  '{'3' * 64}','{OPENAPI_SHA256}','{'5' * 64}'
);
insert into private.account_snapshots (
  id,account_id,environment,sequence,cash_krw,reserved_cash_krw,
  positions_sha256,source_type,source_id,observed_at,
  checkpoint_schema_version,ledger_checkpoint_sha256
) values (
  '{snapshot_id}','contract-test-primary','contract_test',{snapshot_sequence},
  0,0,'{'6' * 64}',
  'ledger_projection','e7e7e7e7-e7e7-47e7-87e7-e7e7e7e7e7e7',
  clock_timestamp(),1,'{'4' * 64}'
);
{jwt_claim_sql(worker_id, role='service_role')}
select fencing_token from worker_api.acquire_worker_lease(
  'contract-test-primary','{worker_id}',clock_timestamp(),300,'{RELEASE_SHA}'
);
""")

    current_encoded = payload_literal(current_payload)
    current_first = json.loads(psql(
        container,
        jwt_claim_sql(worker_id, role="service_role") + f"""
select worker_api.register_qualification_run_v2(
  $payload${current_encoded}$payload$::jsonb
);
""",
    ).stdout.strip().splitlines()[-1])
    if (
        current_first.get("inserted") is not True
        or current_first.get("evidence_sha256")
        != current_payload["evidence_sha256"]
    ):
        raise VerificationError(
            f"qualification v2 first insertion mismatch: {current_first}"
        )
    legacy_retry = json.loads(psql(
        container,
        jwt_claim_sql(worker_id, role="service_role") + f"""
select worker_api.register_qualification_run_v1(
  $payload${legacy_encoded}$payload$::jsonb
);
""",
    ).stdout.strip().splitlines()[-1])
    current_retry = json.loads(psql(
        container,
        jwt_claim_sql(worker_id, role="service_role") + f"""
select worker_api.register_qualification_run_v2(
  $payload${current_encoded}$payload$::jsonb
);
""",
    ).stdout.strip().splitlines()[-1])
    if (
        legacy_retry.get("inserted") is not False
        or current_retry.get("inserted") is not False
        or legacy_retry.get("evidence_sha256")
        != legacy_payload["evidence_sha256"]
        or current_retry.get("evidence_sha256")
        != current_payload["evidence_sha256"]
    ):
        raise VerificationError(
            f"qualification suite exact retry mismatch: {legacy_retry}|{current_retry}"
        )
    expect_failure(
        container,
        jwt_claim_sql(worker_id, role="service_role") + f"""
select worker_api.register_qualification_run_v2(
  $payload${legacy_encoded}$payload$::jsonb
);
""",
        "qualification_run_values_invalid",
    )
    expect_failure(
        container,
        jwt_claim_sql(worker_id, role="service_role") + f"""
select worker_api.register_qualification_run_v1(
  $payload${current_encoded}$payload$::jsonb
);
""",
        "qualification_run_check_set_invalid",
    )
    print(
        "PASS qualification v1 exact retry survives v2 deployment, v2 first "
        "submission persists, and suites remain version-separated"
    )


def verify_reconciliation_keyset(container: str) -> None:
    worker = "cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd"
    decision = "10101010-1010-4010-8010-101010101010"
    risk = "20202020-2020-4020-8020-202020202020"
    psql(container, f"""
insert into private.execution_decisions (
  id,account_id,environment,strategy_version_id,symbol,action,
  decision_at,signal_valid_from,signal_valid_until,
  feature_snapshot_sha256,decision_sha256,release_sha
) values (
  '{decision}','paper-primary','paper','verifier-strategy','005930','buy',
  clock_timestamp()-interval '2 minutes',clock_timestamp()-interval '3 minutes',
  clock_timestamp()+interval '1 hour','{'3' * 64}','{'4' * 64}','{RELEASE_SHA}'
);
insert into private.risk_results (
  id,decision_id,account_id,environment,strategy_version_id,
  risk_policy_sha256,control_epoch,allowed,reason_codes,result_sha256,
  evaluated_at,expires_at,release_sha
) values (
  '{risk}','{decision}','paper-primary','paper','verifier-strategy',
  (select risk_policy_sha256 from private.execution_controls where account_id='paper-primary'),
  1,true,array[]::text[],'{'5' * 64}',clock_timestamp()-interval '1 minute',
  clock_timestamp()+interval '1 hour','{RELEASE_SHA}'
);
insert into private.order_intents (
  id,semantic_key_sha256,account_id,environment,strategy_version_id,
  decision_id,risk_result_id,correlation_id,symbol,side,quantity,limit_price_krw,
  decision_at,signal_valid_from,signal_valid_until,eligible_at,expires_at,
  execution_policy_version,execution_policy_sha256,cost_schedule_version,
  cost_schedule_evidence_sha256,cash_commitment_krw,risk_policy_sha256,
  control_epoch,release_sha
)
select
  md5('recon-intent-'||value::text)::uuid,
  encode(extensions.digest('recon-semantic-'||value::text,'sha256'),'hex'),
  'paper-primary','paper','verifier-strategy','{decision}','{risk}',
  md5('recon-intent-'||value::text)::uuid,'005930','buy',1,10000,
  clock_timestamp()-interval '2 minutes',clock_timestamp()-interval '3 minutes',
  clock_timestamp()+interval '1 hour',clock_timestamp()-interval '1 minute',
  clock_timestamp()+interval '1 hour','unapproved',
  (select execution_policy_sha256 from private.execution_controls where account_id='paper-primary'),
  'verifier-cost','{'6' * 64}',10000,
  (select risk_policy_sha256 from private.execution_controls where account_id='paper-primary'),
  1,'{RELEASE_SHA}'
from generate_series(1,60) as value;
insert into private.execution_reconciliation_state (
  intent_id,priority,state,next_reconcile_at
)
select id,30,'pending',clock_timestamp()-interval '1 minute'
from private.order_intents where decision_id='{decision}';
""")
    result = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
create temp table reconciliation_lease as
select fencing_token,expires_at from worker_api.acquire_worker_lease(
  'paper-primary','{worker}',clock_timestamp(),120,'{RELEASE_SHA}'
);
create temp table first_batch as
select * from worker_api.claim_execution_reconciliation_batch(
  'paper-primary','{worker}','{RELEASE_SHA}',
  (select fencing_token from reconciliation_lease),
  clock_timestamp(),50,null,null,30);
create temp table second_batch as
select * from worker_api.claim_execution_reconciliation_batch(
  'paper-primary','{worker}','{RELEASE_SHA}',
  (select fencing_token from reconciliation_lease),
  clock_timestamp(),50,30,
  (select intent_id from first_batch order by intent_id desc limit 1),30);
select state from worker_api.complete_execution_reconciliation(
  (select intent_id from first_batch order by intent_id limit 1),
  '{worker}','{RELEASE_SHA}',
  (select lease_fencing_token from first_batch order by intent_id limit 1),
  clock_timestamp(),'reschedule',clock_timestamp()+interval '1 minute',
  'reconciliation_positive_control'
);
reset role;
select concat_ws('|',
  (select count(*) from first_batch),
  (select count(*) from second_batch),
  (select count(distinct intent_id) from (
    select intent_id from first_batch union all select intent_id from second_batch
  ) as all_claims),
  (select lease.expires_at = initial.expires_at
    from private.worker_leases as lease
    cross join reconciliation_lease as initial
    where lease.account_id='paper-primary')
);
""").stdout.strip().splitlines()[-2:]
    if result != ["pending", "50|10|60|t"]:
        raise VerificationError(f"reconciliation keyset/starvation mismatch: {result}")
    old_token = psql(container, """
select fencing_token from private.worker_leases where account_id='paper-primary';
""").stdout.strip()
    psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select idempotent from worker_api.release_worker_lease(
  'paper-primary','{worker}',{old_token},clock_timestamp(),'{RELEASE_SHA}'
);
""")
    expect_failure(
        container,
        jwt_claim_sql(worker, role="service_role") + f"""
select * from worker_api.claim_execution_reconciliation_batch(
  'paper-primary','{worker}','{RELEASE_SHA}',{old_token},
  clock_timestamp(),1,null,null,30
);
""",
        "reconciliation_worker_lease_stale_or_missing",
    )
    contract_lease_before = psql(container, """
select count(*) from private.worker_leases
where account_id='contract-test-primary';
""").stdout.strip()
    expect_failure(
        container,
        jwt_claim_sql(worker, role="service_role") + f"""
select * from worker_api.claim_execution_reconciliation_batch(
  'contract-test-primary','{worker}','{RELEASE_SHA}',{old_token},
  clock_timestamp(),1,null,null,30
);
""",
        "reconciliation_worker_lease_stale_or_missing",
    )
    contract_lease_after = psql(container, """
select count(*) from private.worker_leases
where account_id='contract-test-primary';
""").stdout.strip()
    if contract_lease_before != contract_lease_after:
        raise VerificationError("cross-account reconciliation created a worker lease")
    new_token = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select fencing_token from worker_api.acquire_worker_lease(
  'paper-primary','{worker}',clock_timestamp(),30,'{RELEASE_SHA}'
);
""").stdout.strip().splitlines()[-1]
    remaining = psql(container, """
select intent_id from private.execution_reconciliation_state
where state='leased' order by intent_id limit 1;
""").stdout.strip()
    legacy_signature_present = psql(container, """
select to_regprocedure(
  'worker_api.complete_execution_reconciliation(uuid,text,timestamptz,text,timestamptz,text)'
) is not null;
""").stdout.strip()
    if legacy_signature_present != "t":
        raise VerificationError("legacy reconciliation upgrade overload is missing")
    expect_failure(
        container,
        jwt_claim_sql(worker, role="service_role") + f"""
select * from worker_api.complete_execution_reconciliation(
  '{remaining}','{worker}',clock_timestamp(),'reschedule',
  clock_timestamp()+interval '1 minute','legacy_tokenless_completion'
);
""",
        "worker_upgrade_required",
    )
    state_after_legacy = psql(container, f"""
select state from private.execution_reconciliation_state
where intent_id='{remaining}';
""").stdout.strip()
    if state_after_legacy != "leased":
        raise VerificationError("legacy reconciliation overload mutated state")
    expect_failure(
        container,
        jwt_claim_sql(worker, role="service_role") + f"""
select * from worker_api.complete_execution_reconciliation(
  '{remaining}','{worker}','{RELEASE_SHA}',{old_token},clock_timestamp(),
  'reschedule',clock_timestamp()+interval '1 minute','stale_fencing_token'
);
""",
        "reconciliation_lease_not_owned_current_or_fenced",
    )
    expect_failure(
        container,
        jwt_claim_sql(worker, role="service_role") + f"""
select * from worker_api.complete_execution_reconciliation(
  '{remaining}','{worker}','{RELEASE_SHA}',{new_token},clock_timestamp(),
  'reschedule',clock_timestamp()+interval '1 minute','new_token_old_claim'
);
""",
        "reconciliation_lease_not_owned_current_or_fenced",
    )
    expect_failure(
        container,
        jwt_claim_sql(worker, role="service_role") + f"""
select * from worker_api.complete_execution_reconciliation(
  '{remaining}','{worker}','{NEXT_RELEASE_SHA}',{new_token},clock_timestamp(),
  'reschedule',clock_timestamp()+interval '1 minute','wrong_release_sha'
);
""",
        "reconciliation_lease_not_owned_current_or_fenced",
    )
    psql(container, f"""
update private.execution_reconciliation_state
set lease_expires_at=clock_timestamp()-interval '1 second'
where intent_id='{remaining}';
""")
    expect_failure(
        container,
        jwt_claim_sql(worker, role="service_role") + f"""
select * from worker_api.complete_execution_reconciliation(
  '{remaining}','{worker}','{RELEASE_SHA}',{new_token},
  clock_timestamp()-interval '1 minute','reschedule',
  clock_timestamp()+interval '1 minute','expired_state_lease'
);
""",
        "reconciliation_lease_not_owned_current_or_fenced",
    )
    psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select idempotent from worker_api.release_worker_lease(
  'paper-primary','{worker}',{new_token},clock_timestamp(),'{RELEASE_SHA}'
);
""")
    print(
        "PASS reconciliation keyset, exact claim-token fencing and "
        "legacy fail-closed completion"
    )


def verify_manual_reconciliation_atomicity(container: str) -> None:
    worker = "dededede-dede-4ede-8ede-dededededede"
    reason = "operator_reconciliation_required"
    baseline_epoch = int(psql(container, """
select control_epoch from private.execution_controls
where account_id='paper-primary';
""").stdout.strip())
    candidate = psql(container, """
select intent_id from private.execution_reconciliation_state
where state='leased'
order by intent_id
limit 1;
""").stdout.strip()
    if not candidate:
        raise VerificationError("manual reconciliation candidate is missing")
    claim = psql(container, f"""
update private.execution_reconciliation_state
set lease_expires_at=clock_timestamp()-interval '1 second',
    next_reconcile_at=clock_timestamp()-interval '1 second'
where intent_id='{candidate}';
{jwt_claim_sql(worker, role='service_role')}
create temp table manual_reconciliation_lease as
select fencing_token from worker_api.acquire_worker_lease(
  'paper-primary','{worker}',clock_timestamp(),120,'{RELEASE_SHA}'
);
select concat_ws('|',intent_id,lease_fencing_token)
from worker_api.claim_execution_reconciliation_batch(
  'paper-primary','{worker}','{RELEASE_SHA}',
  (select fencing_token from manual_reconciliation_lease),
  clock_timestamp(),1,null,null,30
)
where intent_id='{candidate}';
""").stdout.strip().splitlines()[-1].split("|")
    claimed_intent, token = claim
    if claimed_intent != candidate:
        raise VerificationError(f"manual reconciliation claim mismatch: {claim}")

    expect_failure(
        container,
        jwt_claim_sql(worker, role="service_role") + f"""
select * from worker_api.complete_execution_reconciliation(
  '{candidate}','{worker}','{RELEASE_SHA}',{int(token) + 1},
  clock_timestamp(),'manual',null,'{reason}'
);
""",
        "reconciliation_lease_not_owned_current_or_fenced",
    )
    negative = psql(container, f"""
select concat_ws('|',
  (select count(*) from private.reconciliation_breaks
    where summary_code='{reason}'),
  (select count(*) from private.incidents
    where incident_type='execution_reconciliation_manual_required'
      and summary_code='{reason}'),
  (select count(*) from private.delivery_outbox
    where event_type='execution_reconciliation_manual_required'
      and aggregate_id='{candidate}'),
  (select count(*) from private.audit_events
    where action='execution_reconciliation_manual_required'
      and resource_id='{candidate}')
);
""").stdout.strip()
    if negative != "0|0|0|0":
        raise VerificationError(f"stale manual completion emitted effects: {negative}")

    results = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select state from worker_api.complete_execution_reconciliation(
  '{candidate}','{worker}','{RELEASE_SHA}',{token},clock_timestamp(),
  'manual',null,'{reason}'
);
select state from worker_api.complete_execution_reconciliation(
  '{candidate}','{worker}','{RELEASE_SHA}',{token},clock_timestamp(),
  'manual',null,'{reason}'
);
reset role;
select concat_ws('|',
  (select state from private.execution_reconciliation_state
    where intent_id='{candidate}'),
  (select count(*) from private.reconciliation_breaks
    where summary_code='{reason}'),
  (select count(*) from private.incidents
    where incident_type='execution_reconciliation_manual_required'
      and summary_code='{reason}'),
  (select count(*) from private.delivery_outbox
    where event_type='execution_reconciliation_manual_required'
      and aggregate_id='{candidate}'),
  (select count(*) from private.audit_events
    where action='execution_reconciliation_manual_required'
      and resource_id='{candidate}'),
  (select count(*) from private.order_events
    where intent_id='{candidate}'
      and event_summary->>'reason_code'='{reason}'),
  (select execution_enabled from private.execution_controls
    where account_id='paper-primary'),
  (select control_epoch from private.execution_controls
    where account_id='paper-primary')
);
""").stdout.strip().splitlines()[-3:]
    expected = [
        "manual",
        "manual",
        f"manual|1|1|1|1|1|f|{baseline_epoch + 1}",
    ]
    if results != expected:
        raise VerificationError(f"manual reconciliation atomicity mismatch: {results}")
    psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select idempotent from worker_api.release_worker_lease(
  'paper-primary','{worker}',{token},clock_timestamp(),'{RELEASE_SHA}'
);
""")
    print(
        "PASS manual reconciliation atomically stops execution and emits "
        "idempotent break/incident/outbox/audit evidence"
    )


def verify_semantic_dedupe_concurrency(container: str) -> dict[str, str]:
    decision_id = "40404040-4040-4040-8040-404040404040"
    risk_id = "50505050-5050-4050-8050-505050505050"
    feature_hash = "7" * 64
    cost_evidence_hash = "1" * 64
    policy_hash = "8" * 64
    risk_policy_hash = "9" * 64
    cost_schedule_hash = "a" * 64
    calendar_hash = "b" * 64
    tick_hash = "c" * 64
    volume_hash = "d" * 64
    corporate_action_hash = "e" * 64
    calendar_id = "60606060-6060-4060-8060-606060606060"
    worker = "70707070-7070-4070-8070-707070707070"
    fixture = psql(container, f"""
insert into private.market_calendars (
  id,environment,calendar_version,calendar_sha256,timezone_name,
  valid_from,valid_until,status,evidence_id,requested_by,reviewed_by
) values (
  '{calendar_id}','paper','dedupe-calendar','{calendar_hash}','Asia/Seoul',
  (clock_timestamp() at time zone 'Asia/Seoul')::date-1,
  (clock_timestamp() at time zone 'Asia/Seoul')::date+10,
  'approved','{EVIDENCE}','{ADMIN_1}','{ADMIN_2}'
);
insert into private.market_calendar_sessions (
  calendar_id,session_date,is_open,session_sha256
)
select
  '{calendar_id}',
  (clock_timestamp() at time zone 'Asia/Seoul')::date + offset_value,
  true,
  encode(extensions.digest(convert_to(
    '{calendar_id}:' || (
      (clock_timestamp() at time zone 'Asia/Seoul')::date + offset_value
    )::text,
    'UTF8'
  ),'sha256'),'hex')
from generate_series(-1,3) as offsets(offset_value);
insert into private.paper_execution_model_registry (
  environment,model_version,tick_size_evidence_sha256,
  volume_model_evidence_sha256,corporate_action_evidence_sha256,
  market_calendar_id,status,evidence_id,requested_by,reviewed_by,
  effective_from,effective_until
) values (
  'paper','dedupe-model','{tick_hash}','{volume_hash}',
  '{corporate_action_hash}','{calendar_id}','approved','{EVIDENCE}',
  '{ADMIN_1}','{ADMIN_2}',clock_timestamp()-interval '2 days',
  clock_timestamp()+interval '1 day'
);
insert into private.paper_execution_policies (
  account_id,policy_version,policy_sha256,status,price_model,fill_model,
  parameters,evidence_id,requested_by,reviewed_by,effective_from,effective_until
) values (
  'paper-primary','dedupe-policy','{policy_hash}','approved',
  'next_executable_minute_v1','whole_share_volume_bounded_v1',
  jsonb_build_object(
    'corporate_action_evidence_sha256','{corporate_action_hash}',
    'execution_model_version','dedupe-model',
    'market_calendar_sha256','{calendar_hash}',
    'market_calendar_version','dedupe-calendar',
    'tick_size_evidence_sha256','{tick_hash}',
    'volume_model_evidence_sha256','{volume_hash}'
  ),'{EVIDENCE}','{ADMIN_1}','{ADMIN_2}',
  clock_timestamp()-interval '2 days',clock_timestamp()+interval '1 day'
);
insert into private.execution_cost_schedules (
  account_id,schedule_version,schedule_sha256,buy_commission_rate,
  sell_commission_rate,sell_tax_rate,settlement_days,status,evidence_id,
  requested_by,reviewed_by,effective_from,effective_until
) values (
  'paper-primary','dedupe-cost','{cost_schedule_hash}',0.001,0.001,0.002,0,
  'approved','{EVIDENCE}','{ADMIN_1}','{ADMIN_2}',
  clock_timestamp()-interval '2 days',clock_timestamp()+interval '1 day'
);
alter table private.execution_controls
  disable trigger guard_execution_control_qualification_freshness_v1;
update private.execution_controls
set execution_enabled=true,control_epoch=2,
    active_strategy_version_id='dedupe-strategy',
    execution_policy_version='dedupe-policy',
    execution_policy_sha256='{policy_hash}',risk_policy_sha256='{risk_policy_hash}',
    effective_at=clock_timestamp()-interval '1 minute',
    expires_at=clock_timestamp()+interval '1 day',
    updated_reason_code='verifier_semantic_race',updated_at=clock_timestamp()
where account_id='paper-primary';
alter table private.execution_controls
  enable trigger guard_execution_control_qualification_freshness_v1;
""" + jwt_claim_sql(worker, role="service_role") + f"""
select fencing_token from worker_api.acquire_worker_lease(
  'paper-primary','{worker}',clock_timestamp(),300,'{RELEASE_SHA}'
);
reset role;
with times as (
    select
      date_trunc('minute',clock_timestamp())-interval '1 minute' as decision_at,
      date_trunc('minute',clock_timestamp())-interval '2 minutes' as signal_from,
    date_trunc('minute',clock_timestamp())+interval '10 minutes' as signal_until,
    date_trunc('minute',clock_timestamp()) as eligible_at,
    date_trunc('minute',clock_timestamp())+interval '5 minutes' as expires_at,
    clock_timestamp()-interval '30 seconds' as risk_at,
    clock_timestamp()+interval '5 minutes' as risk_expires
)
select jsonb_build_object(
  'semantic_key',private.compute_order_semantic_key(
    'paper-primary','paper','dedupe-strategy','005930','buy',
    signal_from,signal_until,'dedupe-policy'
  ),
  'decision_at',decision_at,'signal_from',signal_from,
  'signal_until',signal_until,'eligible_at',eligible_at,
  'expires_at',expires_at,'risk_at',risk_at,'risk_expires',risk_expires,
  'fencing_token',(select fencing_token from private.worker_leases
    where account_id='paper-primary')
)
from times;
""").stdout.strip().splitlines()[-1]
    values = json.loads(fixture)

    def reserve_once(_: int) -> str:
        requested_id = str(uuid4())
        result = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select concat_ws('|',reserved,intent_id,reservation_id,reason_code)
from worker_api.reserve_order_intent(
  '{requested_id}','{values['semantic_key']}','paper-primary','paper',
  'dedupe-strategy','{decision_id}','{feature_hash}','{risk_id}',true,
  array[]::text[],'{values['risk_at']}','{values['risk_expires']}',
  '005930','buy',1,10000,'{values['decision_at']}','{values['signal_from']}',
  '{values['signal_until']}','dedupe-policy','dedupe-cost',
  '{cost_evidence_hash}',10010,'{values['eligible_at']}','{values['expires_at']}',
  2,'{worker}',{values['fencing_token']},'{RELEASE_SHA}'
);
""").stdout.strip().splitlines()[-1]
        return result

    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(reserve_once, range(100)))
    created = [row for row in results if row.startswith("t|")]
    duplicates = [row for row in results if row.startswith("f|")]
    if len(created) != 1 or len(duplicates) != 99:
        raise VerificationError(
            f"semantic first-create cardinality mismatch: created={len(created)} "
            f"duplicates={len(duplicates)} values={set(results)}"
        )
    _, intent_id, reservation_id, create_reason = created[0].split("|")
    if create_reason != "reserved":
        raise VerificationError(f"semantic create reason mismatch: {created[0]}")
    expected_duplicate = f"f|{intent_id}|{reservation_id}|duplicate_semantic_intent"
    if set(duplicates) != {expected_duplicate}:
        raise VerificationError(f"semantic canonical duplicate mismatch: {set(duplicates)}")
    counts = psql(container, f"""
select concat_ws('|',
  (select count(*) from private.order_intents where semantic_key_sha256='{values['semantic_key']}'),
  (select count(*) from private.order_reservations where intent_id='{intent_id}'),
  (select count(*) from private.reservation_events where intent_id='{intent_id}'),
  (select count(*) from private.execution_reconciliation_state where intent_id='{intent_id}'),
  (select count(*) from private.order_events where intent_id='{intent_id}'),
  (select count(*) from private.audit_events
    where action='order_intent_reserved' and correlation_id='{intent_id}'),
  (select count(*) from private.delivery_outbox
    where destination_type='audit_archive'
      and payload->'envelope'->>'correlation_id'='{intent_id}'),
  (select reserved_cash_krw from private.cash_balance_projection
    where account_id='paper-primary')
);
""").stdout.strip()
    if counts != "1|1|1|1|1|1|1|10010.0000":
        raise VerificationError(f"semantic first-create atomicity mismatch: {counts}")
    print("PASS 100-way first-create semantic dedupe and atomic reservation")
    return {
        "intent_id": intent_id,
        "reservation_id": reservation_id,
        "worker_id": worker,
        "fencing_token": str(values["fencing_token"]),
        "control_epoch": "2",
    }


def verify_single_active_sell_reservation(
    container: str,
    canonical: dict[str, str],
) -> None:
    symbol = "091990"
    worker = canonical["worker_id"]
    token = canonical["fencing_token"]
    epoch = canonical["control_epoch"]
    payloads = [
        {
            "intent_id": "c1c1c1c1-c1c1-41c1-81c1-c1c1c1c1c1c1",
            "decision_id": "c2c2c2c2-c2c2-42c2-82c2-c2c2c2c2c2c2",
            "risk_id": "c3c3c3c3-c3c3-43c3-83c3-c3c3c3c3c3c3",
            "feature_hash": "6" * 64,
            "window_offset": 1,
        },
        {
            "intent_id": "d1d1d1d1-d1d1-41d1-81d1-d1d1d1d1d1d1",
            "decision_id": "d2d2d2d2-d2d2-42d2-82d2-d2d2d2d2d2d2",
            "risk_id": "d3d3d3d3-d3d3-43d3-83d3-d3d3d3d3d3d3",
            "feature_hash": "7" * 64,
            "window_offset": 2,
        },
    ]
    psql(container, f"""
insert into private.position_projection (
  account_id,symbol,quantity,reserved_quantity,pending_sell_quantity,
  average_cost_krw,projection_version,projected_at
) values (
  'paper-primary','{symbol}',3,0,0,10000,0,clock_timestamp()
)
on conflict (account_id,symbol) do update
set quantity=3,reserved_quantity=0,pending_sell_quantity=0,
    average_cost_krw=10000,projection_version=0,
    projected_at=clock_timestamp();
{jwt_claim_sql(worker, role='service_role')}
select fencing_token from worker_api.renew_worker_lease(
  'paper-primary','{worker}',{token},clock_timestamp(),300,'{RELEASE_SHA}'
);
""")
    timing = json.loads(psql(container, f"""
with times as (
  select
    date_trunc('minute',clock_timestamp())-interval '1 minute' as decision_at,
    date_trunc('minute',clock_timestamp())-interval '2 minutes' as signal_from,
    date_trunc('minute',clock_timestamp())-interval '30 seconds'
      as risk_evaluated_at,
    date_trunc('minute',clock_timestamp())+interval '5 minutes'
      as risk_expires_at,
    date_trunc('minute',clock_timestamp())+interval '10 minutes'
      + interval '1 second' as signal_until_1,
    date_trunc('minute',clock_timestamp())+interval '10 minutes'
      + interval '2 seconds' as signal_until_2,
    date_trunc('minute',clock_timestamp()) as eligible_at,
    date_trunc('minute',clock_timestamp())+interval '5 minutes' as expires_at
)
select jsonb_build_object(
  'decision_at',decision_at,'signal_from',signal_from,
  'risk_evaluated_at',risk_evaluated_at,'risk_expires_at',risk_expires_at,
  'signal_until_1',signal_until_1,'signal_until_2',signal_until_2,
  'eligible_at',eligible_at,'expires_at',expires_at,
  'semantic_key_1',private.compute_order_semantic_key(
    'paper-primary','paper','dedupe-strategy','{symbol}','sell',
    signal_from,signal_until_1,'dedupe-policy'
  ),
  'semantic_key_2',private.compute_order_semantic_key(
    'paper-primary','paper','dedupe-strategy','{symbol}','sell',
    signal_from,signal_until_2,'dedupe-policy'
  )
)
from times;
""").stdout.strip())
    for index, payload in enumerate(payloads, start=1):
        payload["semantic_key"] = timing[f"semantic_key_{index}"]
        payload["signal_until"] = timing[f"signal_until_{index}"]

    def reserve_sql(payload: dict[str, object]) -> str:
        return jwt_claim_sql(worker, role="service_role") + f"""
select concat_ws('|',result.reserved,result.intent_id,result.reservation_id,
  result.reason_code)
from worker_api.reserve_order_intent(
  '{payload['intent_id']}','{payload['semantic_key']}',
  'paper-primary','paper','dedupe-strategy','{payload['decision_id']}',
  '{payload['feature_hash']}','{payload['risk_id']}',true,array[]::text[],
  '{timing['risk_evaluated_at']}','{timing['risk_expires_at']}',
  '{symbol}','sell',1,10000,'{timing['decision_at']}',
  '{timing['signal_from']}','{payload['signal_until']}','dedupe-policy',
  'dedupe-cost','{'1' * 64}',0,'{timing['eligible_at']}',
  '{timing['expires_at']}',{epoch},
  '{worker}',{token},'{RELEASE_SHA}'
) as result;
"""

    def reserve_once(payload: dict[str, object]) -> tuple[str, str]:
        result = psql(container, reserve_sql(payload), check=False)
        if result.returncode == 0:
            return "success", result.stdout.strip().splitlines()[-1]
        return "failure", (result.stdout + "\n" + result.stderr).lower()

    with ThreadPoolExecutor(max_workers=2) as executor:
        concurrent = list(executor.map(reserve_once, payloads))
    successes = [item for item in concurrent if item[0] == "success"]
    failures = [item for item in concurrent if item[0] == "failure"]
    if len(successes) != 1 or len(failures) != 1:
        raise VerificationError(
            f"active sell reservation concurrency mismatch: {concurrent}"
        )
    if "active_sell_reservation_exists" not in failures[0][1]:
        raise VerificationError(
            f"active sell reservation rejection mismatch: {failures[0][1]}"
        )
    created = successes[0][1].split("|")
    if len(created) != 4 or created[0] != "t" or created[3] != "reserved":
        raise VerificationError(f"active sell reservation create mismatch: {created}")
    winner_intent, winner_reservation = created[1], created[2]
    winner_index = next(
        index
        for index, payload in enumerate(payloads)
        if payload["intent_id"] == winner_intent
    )
    loser_payload = payloads[1 - winner_index]

    replay = reserve_once(payloads[winner_index])
    expected_replay = (
        "success",
        f"f|{winner_intent}|{winner_reservation}|duplicate_semantic_intent",
    )
    if replay != expected_replay:
        raise VerificationError(f"active sell exact replay mismatch: {replay}")

    def release_reservation(intent_id: str, reason_code: str) -> None:
        claimed = psql(
            container,
            f"""
update private.execution_reconciliation_state
set priority=0,state='pending',next_reconcile_at=clock_timestamp()-interval '1 second',
    lease_owner=null,lease_expires_at=null
where intent_id='{intent_id}';
{jwt_claim_sql(worker, role='service_role')}
select intent_id from worker_api.claim_execution_reconciliation_batch(
  'paper-primary','{worker}','{RELEASE_SHA}',{token},clock_timestamp(),
  1,null,null,30
);
""",
        ).stdout.strip().splitlines()[-1]
        if claimed != intent_id:
            raise VerificationError(
                f"active sell reconciliation claim mismatch: {claimed}"
            )
        released = psql(
            container,
            jwt_claim_sql(worker, role="service_role")
            + f"""
select concat_ws('|',state,reason_code,idempotent)
from worker_api.fail_reserved_intent_pre_dispatch(
  '{intent_id}','{worker}',{token},{epoch},'{RELEASE_SHA}',clock_timestamp(),
  '{reason_code}'
);
""",
        ).stdout.strip().splitlines()[-1]
        if released != f"complete|{reason_code}|f":
            raise VerificationError(
                f"active sell terminal release mismatch: {released}"
            )

    release_reservation(winner_intent, "sell_reservation_verifier_release")
    released_state = psql(container, f"""
select concat_ws('|',
  (select event_type from private.reservation_events
    where intent_id='{winner_intent}' order by event_sequence desc limit 1),
  (select remaining_quantity from private.reservation_events
    where intent_id='{winner_intent}' order by event_sequence desc limit 1),
  (select state from private.execution_reconciliation_state
    where intent_id='{winner_intent}'),
  (select reserved_quantity from private.position_projection
    where account_id='paper-primary' and symbol='{symbol}')
);
""").stdout.strip()
    if released_state != "released|0|complete|0":
        raise VerificationError(
            f"active sell terminal release state mismatch: {released_state}"
        )

    after_release = reserve_once(loser_payload)
    if after_release[0] != "success":
        raise VerificationError(
            f"sell reservation after release was rejected: {after_release[1]}"
        )
    recreated = after_release[1].split("|")
    if len(recreated) != 4 or recreated[0] != "t" or recreated[3] != "reserved":
        raise VerificationError(
            f"sell reservation after release mismatch: {after_release[1]}"
        )
    release_reservation(recreated[1], "sell_reservation_verifier_cleanup")
    final_reserved = psql(container, f"""
select reserved_quantity from private.position_projection
where account_id='paper-primary' and symbol='{symbol}';
""").stdout.strip()
    if final_reserved != "0":
        raise VerificationError(
            f"sell reservation cleanup projection mismatch: {final_reserved}"
        )
    print(
        "PASS concurrent sell reservation serializes exactly one active intent, "
        "preserves exact replay, and permits the next intent after terminal release"
    )


def verify_aggregate_paper_bar_participation(
    container: str,
    canonical: dict[str, str],
) -> None:
    first_intent = canonical["intent_id"]
    worker = canonical["worker_id"]
    token = canonical["fencing_token"]
    second_intent = "a1a1a1a1-a1a1-41a1-81a1-a1a1a1a1a1a1"
    second_decision = "a2a2a2a2-a2a2-42a2-82a2-a2a2a2a2a2a2"
    second_risk = "a3a3a3a3-a3a3-43a3-83a3-a3a3a3a3a3a3"
    series_id = "a4a4a4a4-a4a4-44a4-84a4-a4a4a4a4a4a4"
    fixture_id = "a5a5a5a5-a5a5-45a5-85a5-a5a5a5a5a5a5"
    conflicting_series_id = "b4b4b4b4-b4b4-44b4-84b4-b4b4b4b4b4b4"
    conflicting_fixture_id = "b5b5b5b5-b5b5-45b5-85b5-b5b5b5b5b5b5"
    attempt_a = "a6a6a6a6-a6a6-46a6-86a6-a6a6a6a6a6a6"
    attempt_b = "a7a7a7a7-a7a7-47a7-87a7-a7a7a7a7a7a7"
    observation_a = "a8a8a8a8-a8a8-48a8-88a8-a8a8a8a8a8a8"
    observation_b = "a9a9a9a9-a9a9-49a9-89a9-a9a9a9a9a9a9"
    marker = psql(
        container,
        jwt_claim_sql(worker, role="service_role")
        + f"""
begin;
reset role;
create temp table participation_second_reservation on commit drop as
with source as (
  select
    intent.*,
    risk.evaluated_at as risk_evaluated_at,
    risk.expires_at as risk_expires_at,
    intent.signal_valid_until + interval '1 second' as second_signal_until,
    private.compute_order_semantic_key(
      intent.account_id,intent.environment,intent.strategy_version_id,
      intent.symbol,intent.side,intent.signal_valid_from,
      intent.signal_valid_until + interval '1 second',
      intent.execution_policy_version
    ) as second_semantic_key
  from private.order_intents as intent
  join private.risk_results as risk on risk.id=intent.risk_result_id
  where intent.id='{first_intent}'
)
select result.*
from source
cross join lateral worker_api.reserve_order_intent(
  '{second_intent}',source.second_semantic_key,'paper-primary','paper',
  source.strategy_version_id,'{second_decision}','{'7' * 64}',
  '{second_risk}',true,array[]::text[],source.risk_evaluated_at,
  source.risk_expires_at,source.symbol,source.side,1,
  source.limit_price_krw,source.decision_at,source.signal_valid_from,
  source.second_signal_until,source.execution_policy_version,
  source.cost_schedule_version,source.cost_schedule_evidence_sha256,
  source.cash_commitment_krw,source.eligible_at,source.expires_at,
  source.control_epoch,'{worker}',{token},'{RELEASE_SHA}'
) as result;
reset role;
do $verify$
begin
  if not exists (
    select 1 from participation_second_reservation
    where reserved and intent_id='{second_intent}'
  ) then
    raise exception 'participation_second_intent_reservation_failed';
  end if;
end;
$verify$;

create temp table participation_bar on commit drop as
select
  date_trunc('minute',clock_timestamp())-interval '2 minutes' as minute,
  date_trunc('minute',clock_timestamp())-interval '1 minute' as completed_at;

insert into private.paper_bar_series (
  id,environment,source_kind,dataset_version,symbol,model_version,
  execution_policy_version,tick_rule_version,tick_size_krw,
  tick_rule_evidence_sha256,volume_source,volume_evidence_sha256,
  corporate_action_status,corporate_action_evidence_sha256,
  market_calendar_version,market_calendar_evidence_sha256,effective_from,
  effective_until,created_release_sha
) values (
  '{series_id}','paper','local_fixture','aggregate-cap-verifier-v1','005930',
  'dedupe-model','dedupe-policy','dedupe-tick',1,'{'c' * 64}','shares',
  '{'d' * 64}','not_required','{'e' * 64}','dedupe-calendar','{'b' * 64}',
  clock_timestamp()-interval '1 day',clock_timestamp()+interval '1 day',
  '{RELEASE_SHA}'
);
insert into private.paper_bar_fixture_sets (
  id,series_id,batch_sequence,first_minute,last_minute,bar_count,
  observed_through,fixture_sha256,evidence_urn,release_sha,ingested_at
)
select
  '{fixture_id}','{series_id}',1,minute,minute,1,completed_at,'{'2' * 64}',
  'urn:sha256:{'2' * 64}','{RELEASE_SHA}',clock_timestamp()
from participation_bar;
insert into private.paper_minute_bars (
  fixture_set_id,series_id,sequence,minute,completed_at,as_of,source_sha256,
  is_complete,open_krw,high_krw,low_krw,close_krw,volume,bar_sha256
)
select
  '{fixture_id}','{series_id}',1,minute,completed_at,completed_at,'{'3' * 64}',
  true,10000,10000,10000,10000,100,'{'4' * 64}'
from participation_bar;
insert into private.paper_bar_series (
  id,environment,source_kind,dataset_version,symbol,model_version,
  execution_policy_version,tick_rule_version,tick_size_krw,
  tick_rule_evidence_sha256,volume_source,volume_evidence_sha256,
  corporate_action_status,corporate_action_evidence_sha256,
  market_calendar_version,market_calendar_evidence_sha256,effective_from,
  effective_until,created_release_sha
) values (
  '{conflicting_series_id}','paper','local_fixture',
  'aggregate-conflict-verifier-v1','005930','dedupe-model','dedupe-policy',
  'dedupe-tick',1,'{'c' * 64}','conflicting-shares','{'6' * 64}',
  'not_required','{'e' * 64}','dedupe-calendar','{'b' * 64}',
  clock_timestamp()-interval '1 day',clock_timestamp()+interval '1 day',
  '{RELEASE_SHA}'
);
insert into private.paper_bar_fixture_sets (
  id,series_id,batch_sequence,first_minute,last_minute,bar_count,
  observed_through,fixture_sha256,evidence_urn,release_sha,ingested_at
)
select
  '{conflicting_fixture_id}','{conflicting_series_id}',1,minute,minute,1,
  completed_at,'{'7' * 64}','urn:sha256:{'7' * 64}','{RELEASE_SHA}',
  clock_timestamp()
from participation_bar;
insert into private.paper_minute_bars (
  fixture_set_id,series_id,sequence,minute,completed_at,as_of,source_sha256,
  is_complete,open_krw,high_krw,low_krw,close_krw,volume,bar_sha256
)
select
  '{conflicting_fixture_id}','{conflicting_series_id}',1,minute,completed_at,
  completed_at,'{'8' * 64}',true,10000,10000,10000,10000,1000,'{'9' * 64}'
from participation_bar;
insert into private.paper_execution_candidates (
  intent_id,account_id,environment,semantic_key_sha256,decision_id,risk_result_id,
  decision_feature_sha256,risk_evaluated_at,risk_expires_at,strategy_version_id,
  symbol,side,quantity,limit_price_krw,cash_commitment_krw,decision_at,
  signal_valid_from,signal_valid_until,eligible_at,expires_at,
  execution_policy_version,cost_schedule_version,cost_schedule_evidence_sha256,
  fixture_series_id,risk_input,candidate_sha256,source_release_sha,created_at
)
select
  intent.id,intent.account_id,intent.environment,intent.semantic_key_sha256,
  intent.decision_id,intent.risk_result_id,'{'7' * 64}',intent.decision_at,
  risk.expires_at,'b1b1b1b1-b1b1-41b1-81b1-b1b1b1b1b1b1',intent.symbol,
  intent.side,intent.quantity,intent.limit_price_krw,intent.cash_commitment_krw,
  intent.decision_at,intent.signal_valid_from,intent.signal_valid_until,
  intent.eligible_at,intent.expires_at,intent.execution_policy_version,
  intent.cost_schedule_version,intent.cost_schedule_evidence_sha256,
  '{series_id}','{{}}'::jsonb,
  case when intent.id='{first_intent}' then '{'5' * 64}' else '{'6' * 64}' end,
  intent.release_sha,intent.created_at
from private.order_intents as intent
join private.risk_results as risk on risk.id=intent.risk_result_id
where intent.id in ('{first_intent}','{second_intent}');

insert into private.order_attempts (
  id,reservation_id,intent_id,account_id,environment,broker,lease_holder_id,
  fencing_token,control_epoch,client_order_key,request_sha256,prepared_at
)
select
  case when reservation.intent_id='{first_intent}'
    then '{attempt_a}'::uuid else '{attempt_b}'::uuid end,
  reservation.id,reservation.intent_id,reservation.account_id,
  reservation.environment,'internal_paper','{worker}',reservation.fencing_token,
  reservation.control_epoch,
  case when reservation.intent_id='{first_intent}'
    then 'aggregate-cap-verifier-a' else 'aggregate-cap-verifier-b' end,
  case when reservation.intent_id='{first_intent}'
    then '{'8' * 64}' else '{'9' * 64}' end,
  bar.completed_at-interval '1 second'
from private.order_reservations as reservation
cross join participation_bar as bar
where reservation.intent_id in ('{first_intent}','{second_intent}');
insert into private.provider_order_bindings (
  attempt_id,intent_id,provider_order_id,binding_sha256,bound_at
)
select '{attempt_a}'::uuid,'{first_intent}'::uuid,
       'paper:{first_intent}','{'a' * 64}',
       completed_at-interval '1 second'
from participation_bar
union all
select '{attempt_b}'::uuid,'{second_intent}'::uuid,
       'paper:{second_intent}','{'f' * 64}',
       completed_at-interval '1 second'
from participation_bar;
insert into private.execution_observations (
  id,intent_id,attempt_id,sequence,event_type,observed_at,cumulative_quantity,
  cumulative_gross_krw,cumulative_commission_krw,cumulative_tax_krw,
  observation_sha256,provider_order_id,provider_execution_id,
  provider_observation_sha256
)
select '{observation_a}'::uuid,'{first_intent}'::uuid,'{attempt_a}'::uuid,
       1,'filled',completed_at,
       1,10000,0,0,'{'0' * 64}','paper:{first_intent}',
       'paper:{first_intent}:fill:1','{'1' * 64}'
from participation_bar
union all
select '{observation_b}'::uuid,'{second_intent}'::uuid,'{attempt_b}'::uuid,
       1,'filled',completed_at,
       1,10000,0,0,'{'2' * 64}','paper:{second_intent}',
       'paper:{second_intent}:fill:1','{'3' * 64}'
from participation_bar;

insert into private.fills (
  event_id,intent_id,attempt_id,account_id,broker,provider_execution_id,
  quantity,price_krw,commission_krw,tax_krw,filled_at,settlement_date
)
select '{observation_a}','{first_intent}','{attempt_a}','paper-primary',
       'internal_paper','paper:{first_intent}:fill:1',1,10000,0,0,completed_at,
       (completed_at at time zone 'Asia/Seoul')::date
from participation_bar;
do $verify$
declare
  rejected boolean := false;
begin
  begin
    insert into private.fills (
      event_id,intent_id,attempt_id,account_id,broker,provider_execution_id,
      quantity,price_krw,commission_krw,tax_krw,filled_at,settlement_date
    )
    select '{observation_b}','{second_intent}','{attempt_b}','paper-primary',
           'internal_paper','paper:{second_intent}:fill:1',1,10000,0,0,
           completed_at,(completed_at at time zone 'Asia/Seoul')::date
    from participation_bar;
  exception
    when check_violation then
      if sqlerrm <> 'paper_bar_participation_capacity_exceeded' then
        raise;
      end if;
      rejected := true;
  end;
  if not rejected then
    raise exception 'aggregate_paper_bar_participation_was_not_rejected';
  end if;
  if (
    select concat_ws('|',count(*),coalesce(sum(fill.quantity),0))
    from private.fills as fill
    join private.order_intents as intent on intent.id=fill.intent_id
    cross join participation_bar as bar
    where fill.account_id='paper-primary'
      and intent.symbol='005930'
      and fill.filled_at=bar.completed_at
  ) <> '1|1' then
    raise exception 'aggregate_paper_bar_participation_count_mismatch';
  end if;
end;
$verify$;

-- Candidate rows are immutable in production.  The disposable verifier
-- rewires only the unfilled second fixture to prove both conflicting evidence
-- arrival orders against the same already-committed fill.
alter table private.paper_execution_candidates
  disable trigger reject_paper_execution_candidate_mutation;
update private.paper_execution_candidates
set fixture_series_id='{conflicting_series_id}'
where intent_id='{second_intent}';
alter table private.paper_execution_candidates
  enable trigger reject_paper_execution_candidate_mutation;
do $verify$
declare
  rejected boolean := false;
begin
  begin
    insert into private.fills (
      event_id,intent_id,attempt_id,account_id,broker,provider_execution_id,
      quantity,price_krw,commission_krw,tax_krw,filled_at,settlement_date
    )
    select '{observation_b}','{second_intent}','{attempt_b}','paper-primary',
           'internal_paper','paper:{second_intent}:fill:1',1,10000,0,0,
           completed_at,(completed_at at time zone 'Asia/Seoul')::date
    from participation_bar;
  exception
    when check_violation then
      if sqlerrm <> 'paper_bar_participation_evidence_conflict' then
        raise;
      end if;
      rejected := true;
  end;
  if not rejected then
    raise exception 'paper_bar_cross_series_conflict_was_not_rejected';
  end if;
end;
$verify$;

alter table private.paper_execution_candidates
  disable trigger reject_paper_execution_candidate_mutation;
update private.paper_execution_candidates
set fixture_series_id='{conflicting_series_id}'
where intent_id='{first_intent}';
update private.paper_execution_candidates
set fixture_series_id='{series_id}'
where intent_id='{second_intent}';
alter table private.paper_execution_candidates
  enable trigger reject_paper_execution_candidate_mutation;
do $verify$
declare
  rejected boolean := false;
begin
  begin
    insert into private.fills (
      event_id,intent_id,attempt_id,account_id,broker,provider_execution_id,
      quantity,price_krw,commission_krw,tax_krw,filled_at,settlement_date
    )
    select '{observation_b}','{second_intent}','{attempt_b}','paper-primary',
           'internal_paper','paper:{second_intent}:fill:1',1,10000,0,0,
           completed_at,(completed_at at time zone 'Asia/Seoul')::date
    from participation_bar;
  exception
    when check_violation then
      if sqlerrm <> 'paper_bar_participation_evidence_conflict' then
        raise;
      end if;
      rejected := true;
  end;
  if not rejected then
    raise exception 'paper_bar_reverse_series_conflict_was_not_rejected';
  end if;
end;
$verify$;
select 'aggregate_participation_ok';
rollback;
""",
    ).stdout.strip().splitlines()
    if "aggregate_participation_ok" not in marker:
        raise VerificationError(f"aggregate participation marker missing: {marker}")
    print(
        "PASS aggregate Paper bar participation cap and cross-series evidence "
        "conflicts are order-independent"
    )


def verify_execution_transition_guards(container: str) -> None:
    base = "private.execution_observation_transition_violation"
    valid = psql(container, f"""
select coalesce({base}(
  null,null,null,0,0,0,0,
  1,'open',clock_timestamp(),2,0,0,0,0,
  null,null,null,'[]'::jsonb
),'ok');
select coalesce({base}(
  1,'open',clock_timestamp()-interval '1 second',0,0,0,0,
  2,'partial_filled',clock_timestamp(),2,1,100,0,0,
  1,100,current_date,'[{{}},{{}}]'::jsonb
),'ok');
select coalesce({base}(
  2,'partial_filled',clock_timestamp()-interval '1 second',1,100,0,0,
  3,'canceled',clock_timestamp(),2,1,100,0,0,
  null,null,null,'[]'::jsonb
),'ok');
""").stdout.strip().splitlines()[-3:]
    if valid != ["ok", "ok", "ok"]:
        raise VerificationError(f"valid execution transitions rejected: {valid}")
    attacks = {
        "sequence_gap": f"""select {base}(
          1,'open',clock_timestamp(),0,0,0,0,
          3,'open',clock_timestamp(),2,0,0,0,0,
          null,null,null,'[]'::jsonb);""",
        "observed_at_regressed": f"""select {base}(
          1,'open',clock_timestamp(),0,0,0,0,
          2,'open',clock_timestamp()-interval '1 second',2,0,0,0,0,
          null,null,null,'[]'::jsonb);""",
        "terminal_delta_without_fill": f"""select {base}(
          1,'partial_filled',clock_timestamp(),1,100,0,0,
          2,'filled',clock_timestamp(),2,2,200,0,0,
          null,null,null,'[]'::jsonb);""",
        "zero_delta_with_fill": f"""select {base}(
          1,'partial_filled',clock_timestamp(),1,100,0,0,
          2,'partial_filled',clock_timestamp(),2,1,100,0,0,
          1,100,current_date,'[{{}},{{}}]'::jsonb);""",
        "rejected_after_fill": f"""select {base}(
          1,'partial_filled',clock_timestamp(),1,100,0,0,
          2,'rejected',clock_timestamp(),2,1,100,0,0,
          null,null,null,'[]'::jsonb);""",
    }
    expected = {
        "sequence_gap": "execution_observation_sequence_gap",
        "observed_at_regressed": "execution_observed_at_regressed",
        "terminal_delta_without_fill": "fill_delta_requires_complete_evidence",
        "zero_delta_with_fill": "non_fill_observation_delta_or_evidence_forbidden",
        "rejected_after_fill": "non_executed_terminal_observation_quantity_invalid",
    }
    for name, sql in attacks.items():
        value = psql(container, sql).stdout.strip().splitlines()[-1]
        if value != expected[name]:
            raise VerificationError(f"execution guard {name} mismatch: {value}")
    print("PASS exact sequence, status matrix and fill/accounting evidence guards")


def verify_pre_dispatch_recovery(container: str, canonical: dict[str, str]) -> None:
    intent_id = canonical["intent_id"]
    reservation_id = canonical["reservation_id"]
    crashed_worker = canonical["worker_id"]
    crashed_token = canonical["fencing_token"]
    worker = "71717171-7171-4171-8171-717171717171"
    epoch = canonical["control_epoch"]
    claim = psql(container, f"""
update private.execution_reconciliation_state
set priority=0,state='pending',next_reconcile_at=clock_timestamp()-interval '1 second',
    lease_owner=null,lease_expires_at=null
where intent_id='{intent_id}';
update private.worker_leases
set acquired_at=clock_timestamp()-interval '2 minutes',
    renewed_at=clock_timestamp()-interval '1 minute',
    expires_at=clock_timestamp()-interval '1 second'
where account_id='paper-primary' and holder_id='{crashed_worker}'
  and fencing_token={crashed_token};
""" + jwt_claim_sql(worker, role="service_role") + f"""
create temp table pre_dispatch_recovery_lease as
select fencing_token from worker_api.acquire_worker_lease(
  'paper-primary','{worker}',clock_timestamp(),120,'{NEXT_RELEASE_SHA}'
);
select concat_ws('|',intent_id,lease_fencing_token,reservation_fencing_token,
  control_epoch,reservation_control_epoch,intent_release_sha,lease_release_sha,
  recovery_disposition)
from worker_api.claim_execution_reconciliation_batch(
  'paper-primary','{worker}','{NEXT_RELEASE_SHA}',
  (select fencing_token from pre_dispatch_recovery_lease),
  clock_timestamp(),1,null,null,120
);
""").stdout.strip().splitlines()[-1]
    claim_parts = claim.split("|")
    if claim_parts != [
        intent_id, str(int(crashed_token) + 1), crashed_token,
        epoch, epoch, RELEASE_SHA, NEXT_RELEASE_SHA,
        "pre_dispatch_release_takeover",
    ]:
        raise VerificationError(f"restart takeover claim mismatch: {claim}")
    token = claim_parts[1]
    expect_failure(
        container,
        jwt_claim_sql(crashed_worker, role="service_role") + f"""
select * from worker_api.fail_reserved_intent_pre_dispatch(
  '{intent_id}','{crashed_worker}',{crashed_token},{epoch},'{RELEASE_SHA}',
  clock_timestamp(),'pre_dispatch_crash_recovered'
);
""",
        "worker_fencing_token_stale",
    )
    expect_failure(container, f"""
select set_config('request.jwt.claim.role','service_role',false);
select set_config('request.jwt.claim.sub','{worker}',false);
select set_config('request.jwt.claims',jsonb_build_object(
  'role','service_role','sub','{worker}'
)::text,false);
begin;
insert into private.order_attempts (
  reservation_id,intent_id,account_id,environment,broker,lease_holder_id,
  fencing_token,control_epoch,client_order_key,request_sha256,prepared_at
) values (
  '{reservation_id}','{intent_id}','paper-primary','paper','internal_paper',
  '{worker}',{token},{epoch},'terminal-without-fill-attack','{'b' * 64}',
  clock_timestamp()
);
with attack as (
  select clock_timestamp() as observed_at
), payload as (
  select observed_at,encode(extensions.digest(convert_to(concat_ws('|',
    '{intent_id}','1','filled','provider-order-attack','',
    private.utc_iso8601(observed_at),'1','10000','0','0','','','',
    'terminal_without_fill_attack'
  ),'UTF8'),'sha256'),'hex') as observation_sha256
  from attack
)
select concat_ws('|',result.quarantined,result.reason_code)
from payload
cross join lateral worker_api.record_execution_observation(
  '{intent_id}',1,'filled','provider-order-attack',null,
  payload.observation_sha256,payload.observed_at,1,10000,0,0,
  null,null,null,'[]'::jsonb,'terminal_without_fill_attack','{worker}',{token}
) as result;
rollback;
""", "worker_fencing_token_stale")
    expect_failure(
        container,
        f"""
select set_config('request.jwt.claim.role','service_role',false);
select set_config('request.jwt.claim.sub','{worker}',false);
select set_config('request.jwt.claims',jsonb_build_object(
  'role','service_role','sub','{worker}'
)::text,false);
begin;
insert into private.order_attempts (
  reservation_id,intent_id,account_id,environment,broker,lease_holder_id,
  fencing_token,control_epoch,client_order_key,request_sha256,prepared_at
) values (
  '{reservation_id}','{intent_id}','paper-primary','paper','internal_paper',
  '{worker}',{token},{epoch},'attack-dispatch-started','{'a' * 64}',clock_timestamp()
);
select * from worker_api.fail_reserved_intent_pre_dispatch(
  '{intent_id}','{worker}',{token},{epoch},'{NEXT_RELEASE_SHA}',clock_timestamp(),
  'pre_dispatch_crash_recovered'
);
commit;
""",
        "pre_dispatch_failure_dispatch_already_started",
    )
    expect_failure(
        container,
        jwt_claim_sql(worker, role="service_role") + f"""
select * from worker_api.fail_reserved_intent_pre_dispatch(
  '{intent_id}','{worker}',{int(token) + 1},{epoch},'{NEXT_RELEASE_SHA}',
  clock_timestamp(),'pre_dispatch_crash_recovered'
);
""",
        "worker_fencing_token_stale",
    )
    result = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select concat_ws('|',state,reason_code,idempotent,observation_id)
from worker_api.fail_reserved_intent_pre_dispatch(
  '{intent_id}','{worker}',{token},{epoch},'{NEXT_RELEASE_SHA}',clock_timestamp(),
  'pre_dispatch_crash_recovered'
);
select concat_ws('|',state,reason_code,idempotent,observation_id)
from worker_api.fail_reserved_intent_pre_dispatch(
  '{intent_id}','{worker}',{token},{epoch},'{NEXT_RELEASE_SHA}',clock_timestamp(),
  'pre_dispatch_crash_recovered'
);
""").stdout.strip().splitlines()[-2:]
    first = result[0].split("|")
    second = result[1].split("|")
    if first[:3] != ["complete", "pre_dispatch_crash_recovered", "f"]:
        raise VerificationError(f"pre-dispatch recovery result mismatch: {result}")
    if second[:3] != ["complete", "pre_dispatch_crash_recovered", "t"] \
            or second[3] != first[3]:
        raise VerificationError(f"pre-dispatch recovery replay mismatch: {result}")
    state = psql(container, f"""
select concat_ws('|',
  (select count(*) from private.order_attempts where intent_id='{intent_id}'),
  (select count(*) from private.execution_observations
    where intent_id='{intent_id}' and event_type='failed_pre_dispatch'
      and provider_order_id is null),
  (select state from private.execution_reconciliation_state where intent_id='{intent_id}'),
  (select remaining_cash_krw from private.reservation_events
    where intent_id='{intent_id}' order by event_sequence desc limit 1),
  (select reserved_cash_krw from private.cash_balance_projection
    where account_id='paper-primary'),
  (select count(*) from private.audit_events
    where action='reserved_intent_failed_pre_dispatch' and correlation_id='{intent_id}'),
  (select count(*) from private.delivery_outbox
    where dedupe_key='pre-dispatch-failure:{intent_id}')
);
""").stdout.strip()
    if state != "0|1|complete|0|0.0000|1|1":
        raise VerificationError(f"pre-dispatch recovery atomicity mismatch: {state}")
    psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select idempotent from worker_api.release_worker_lease(
  'paper-primary','{worker}',{token},clock_timestamp(),'{NEXT_RELEASE_SHA}'
);
""")
    print("PASS reserve-only crash recovery is fenced, atomic and idempotent")


def verify_partial_resume_accounting_and_expiry(container: str) -> None:
    intent_id = "74747474-7474-4474-8474-747474747474"
    decision_id = "75757575-7575-4575-8575-757575757575"
    risk_id = "76767676-7676-4676-8676-767676767676"
    worker_a = "72727272-7272-4272-8272-727272727272"
    worker_b = "73737373-7373-4373-8373-737373737373"
    setup = psql(container, jwt_claim_sql(worker_a, role="service_role") + f"""
select fencing_token from worker_api.acquire_worker_lease(
  'paper-primary','{worker_a}',clock_timestamp(),120,'{RELEASE_SHA}'
);
reset role;
with times as (
  select
    date_trunc('minute',clock_timestamp())-interval '1 minute' as decision_at,
    date_trunc('minute',clock_timestamp())-interval '2 minutes' as signal_from,
    date_trunc('minute',clock_timestamp())+interval '10 minutes' as signal_until,
    date_trunc('minute',clock_timestamp()) as eligible_at,
    clock_timestamp()+interval '8 seconds' as expires_at,
    clock_timestamp()-interval '30 seconds' as risk_at,
    clock_timestamp()+interval '5 minutes' as risk_expires
)
select jsonb_build_object(
  'decision_at',decision_at,'signal_from',signal_from,
  'signal_until',signal_until,'eligible_at',eligible_at,
  'expires_at',expires_at,'risk_at',risk_at,'risk_expires',risk_expires,
  'semantic_key',private.compute_order_semantic_key(
    'paper-primary','paper','dedupe-strategy','000660','buy',
    signal_from,signal_until,'dedupe-policy'
  ),
  'fencing_token',(select fencing_token from private.worker_leases
    where account_id='paper-primary'),
  'control_epoch',(select control_epoch from private.execution_controls
    where account_id='paper-primary')
)
from times;
""").stdout.strip().splitlines()[-1]
    values = json.loads(setup)
    token_a = int(values["fencing_token"])
    reserved = psql(container, jwt_claim_sql(worker_a, role="service_role") + f"""
select concat_ws('|',reserved,intent_id,reservation_id,reason_code)
from worker_api.reserve_order_intent(
  '{intent_id}','{values['semantic_key']}','paper-primary','paper',
  'dedupe-strategy','{decision_id}','{'7' * 64}','{risk_id}',true,
  array[]::text[],'{values['risk_at']}','{values['risk_expires']}',
  '000660','buy',2,10000,'{values['decision_at']}','{values['signal_from']}',
  '{values['signal_until']}','dedupe-policy','dedupe-cost','{'1' * 64}',
  20020,'{values['eligible_at']}','{values['expires_at']}',2,
  '{worker_a}',{token_a},'{RELEASE_SHA}'
);
select concat_ws('|',attempt_id,reason_code)
from worker_api.mark_dispatch_started(
  '{intent_id}','paper-primary','paper','{worker_a}',{token_a},2,
  clock_timestamp(),'{'2' * 64}','paper:{intent_id}'
);
""").stdout.strip().splitlines()[-2:]
    if not reserved[0].startswith(f"t|{intent_id}|") or not reserved[1].endswith("|prepared"):
        raise VerificationError(f"partial fixture reserve/dispatch mismatch: {reserved}")

    observed = json.loads(psql(container, f"""
select jsonb_build_object(
  'observed_at',intent.eligible_at,
  'settlement_date',(intent.eligible_at at time zone 'Asia/Seoul')::date
)
from private.order_intents as intent
where intent.id='{intent_id}';
""").stdout.strip())
    install_paper_fill_bar_evidence(
        container,
        intent_id=intent_id,
        filled_at=observed["observed_at"],
        label="partial-resume-accounting",
    )
    observation_hash = psql(container, f"""
select encode(extensions.digest(convert_to(concat_ws('|',
  '{intent_id}','1','partial_filled','paper:{intent_id}',
  'paper:{intent_id}:fill:1',private.utc_iso8601('{observed['observed_at']}'::timestamptz),
  '1','9000','9','0','1','9000','{observed['settlement_date']}',
  'partial_fill_verifier'
),'UTF8'),'sha256'),'hex');
""").stdout.strip()
    postings = "jsonb_build_array(" \
        "jsonb_build_object('account','POSITION_COST','debit_krw',9000,'credit_krw',0)," \
        "jsonb_build_object('account','FEES','debit_krw',9,'credit_krw',0)," \
        "jsonb_build_object('account','CASH','debit_krw',0,'credit_krw',9009))"
    record = psql(container, jwt_claim_sql(worker_a, role="service_role") + f"""
select concat_ws('|',observation_id,inserted,quarantined,reason_code)
from worker_api.record_execution_observation(
  '{intent_id}',1,'partial_filled','paper:{intent_id}',
  'paper:{intent_id}:fill:1','{observation_hash}',
  '{observed['observed_at']}',1,9000,9,0,1,9000,
  '{observed['settlement_date']}',{postings},'partial_fill_verifier',
  '{worker_a}',{token_a}
);
select concat_ws('|',observation_id,inserted,quarantined,reason_code)
from worker_api.record_execution_observation(
  '{intent_id}',1,'partial_filled','paper:{intent_id}',
  'paper:{intent_id}:fill:1','{observation_hash}',
  '{observed['observed_at']}',1,9000,9,0,1,9000,
  '{observed['settlement_date']}',{postings},'partial_fill_verifier',
  '{worker_a}',{token_a}
);
""").stdout.strip().splitlines()[-2:]
    first = record[0].split("|")
    duplicate = record[1].split("|")
    if first[1:] != ["t", "f", "recorded"] \
            or duplicate != [first[0], "f", "f", "duplicate_observation"]:
        raise VerificationError(f"partial fill/duplicate mismatch: {record}")
    partial_state = psql(container, f"""
select concat_ws('|',
  (select count(*) from private.fills where intent_id='{intent_id}'),
  (select count(*) from private.accounting_transactions
    where correlation_id='{intent_id}' and source_type='fill'),
  (select count(*) from private.accounting_postings as posting
    join private.accounting_transactions as transaction
      on transaction.id=posting.journal_entry_id
    where transaction.correlation_id='{intent_id}' and transaction.source_type='fill'),
  (select settled_cash_krw::bigint from private.cash_balance_projection
    where account_id='paper-primary'),
  (select reserved_cash_krw::bigint from private.cash_balance_projection
    where account_id='paper-primary'),
  (select pending_debit_cash_krw::bigint from private.cash_balance_projection
    where account_id='paper-primary'),
  (select quantity from private.position_projection
    where account_id='paper-primary' and symbol='000660'),
  (select average_cost_krw from private.position_projection
    where account_id='paper-primary' and symbol='000660'),
  (select remaining_cash_krw from private.reservation_events
    where intent_id='{intent_id}' order by event_sequence desc limit 1),
  (select state from private.execution_reconciliation_state where intent_id='{intent_id}')
);
""").stdout.strip()
    if partial_state != "1|1|3|10000000|10010|9009|1|9000.0000|10010|pending":
        raise VerificationError(f"partial fill ledger/projection mismatch: {partial_state}")

    expect_failure(
        container,
        """
begin;
update private.execution_controls
set execution_enabled=false,control_epoch=3,effective_at=clock_timestamp(),
    updated_at=clock_timestamp(),updated_reason_code='verifier_emergency_stop'
where account_id='paper-primary';
""" + jwt_claim_sql(worker_a, role="service_role") + f"""
select * from worker_api.mark_dispatch_started(
  '{intent_id}','paper-primary','paper','{worker_a}',{token_a},3,
  clock_timestamp(),'{'2' * 64}','paper:{intent_id}'
);
rollback;
""",
        "reservation_fencing_or_epoch_mismatch",
    )
    expect_failure(
        container,
        """
begin;
update private.execution_controls
set execution_enabled=false,control_epoch=3,effective_at=clock_timestamp(),
    updated_at=clock_timestamp(),updated_reason_code='verifier_emergency_stop'
where account_id='paper-primary';
""" + jwt_claim_sql(worker_a, role="service_role") + f"""
select * from worker_api.load_paper_execution_checkpoint(
  '{intent_id}','paper-primary','{worker_a}',{token_a},3,
  '{RELEASE_SHA}',clock_timestamp()
);
rollback;
""",
        "paper_checkpoint_control_revalidation_failed",
    )
    stopped_duplicate = psql(container, """
begin;
update private.execution_controls
set execution_enabled=false,control_epoch=3,effective_at=clock_timestamp(),
    updated_at=clock_timestamp(),updated_reason_code='verifier_emergency_stop'
where account_id='paper-primary';
""" + jwt_claim_sql(worker_a, role="service_role") + f"""
select concat_ws('|',inserted,quarantined,reason_code)
from worker_api.record_execution_observation(
  '{intent_id}',1,'partial_filled','paper:{intent_id}',
  'paper:{intent_id}:fill:1','{observation_hash}',
  '{observed['observed_at']}',1,9000,9,0,1,9000,
  '{observed['settlement_date']}',{postings},'partial_fill_verifier',
  '{worker_a}',{token_a}
);
rollback;
""").stdout.strip().splitlines()
    if "f|f|duplicate_observation" not in stopped_duplicate:
        raise VerificationError(
            f"pre-stop durable response was not accepted after stop: {stopped_duplicate}"
        )
    expect_failure(
        container,
        f"""
begin;
update private.worker_leases
set acquired_at=clock_timestamp()-interval '2 minutes',
    renewed_at=clock_timestamp()-interval '1 minute',
    expires_at=clock_timestamp()-interval '1 second'
where account_id='paper-primary' and holder_id='{worker_a}'
  and fencing_token={token_a};
""" + jwt_claim_sql(worker_b, role="service_role") + f"""
select fencing_token from worker_api.acquire_worker_lease(
  'paper-primary','{worker_b}',clock_timestamp(),30,'{NEXT_RELEASE_SHA}'
);
select * from worker_api.record_execution_observation(
  '{intent_id}',1,'partial_filled','paper:{intent_id}',
  'paper:{intent_id}:fill:1','{observation_hash}',
  '{observed['observed_at']}',1,9000,9,0,1,9000,
  '{observed['settlement_date']}',{postings},'partial_fill_verifier',
  '{worker_b}',{token_a + 1}
);
rollback;
""",
        "worker_fencing_token_stale",
    )
    unchanged_after_guards = psql(container, f"""
select concat_ws('|',
  (select count(*) from private.execution_observations where intent_id='{intent_id}'),
  (select count(*) from private.fills where intent_id='{intent_id}'),
  (select count(*) from private.accounting_transactions
    where correlation_id='{intent_id}' and source_type='fill'),
  (select control_epoch from private.execution_controls where account_id='paper-primary'),
  (select execution_enabled from private.execution_controls where account_id='paper-primary')
);
""").stdout.strip()
    if unchanged_after_guards != "1|1|1|2|t":
        raise VerificationError(
            f"stop/release guard mutated durable state: {unchanged_after_guards}"
        )

    claim = psql(container, f"""
update private.worker_leases
set acquired_at=clock_timestamp()-interval '2 minutes',
    renewed_at=clock_timestamp()-interval '1 minute',
    expires_at=clock_timestamp()-interval '1 second'
where account_id='paper-primary' and holder_id='{worker_a}'
  and fencing_token={token_a};
update private.execution_reconciliation_state
set state='pending',next_reconcile_at=clock_timestamp()-interval '1 second',
    lease_owner=null,lease_expires_at=null
where intent_id='{intent_id}';
""" + jwt_claim_sql(worker_b, role="service_role") + f"""
create temp table partial_resume_lease as
select fencing_token from worker_api.acquire_worker_lease(
  'paper-primary','{worker_b}',clock_timestamp(),120,'{RELEASE_SHA}'
);
select concat_ws('|',intent_id,lease_fencing_token,reservation_fencing_token,
  latest_sequence,latest_status,latest_cumulative_quantity,
  observation_history_sha256,recovery_disposition,control_epoch)
from worker_api.claim_execution_reconciliation_batch(
  'paper-primary','{worker_b}','{RELEASE_SHA}',
  (select fencing_token from partial_resume_lease),
  clock_timestamp(),1,null,null,120
);
""").stdout.strip().splitlines()[-1].split("|")
    if claim[:6] != [
        intent_id, str(token_a + 1), str(token_a), "1", "partial_filled", "1",
    ] or claim[7:] != ["same_release", "2"]:
        raise VerificationError(f"partial restart claim mismatch: {claim}")
    token_b = int(claim[1])
    expected_history_hash = hashlib.sha256(f"1:{observation_hash}".encode()).hexdigest()
    if claim[6] != expected_history_hash:
        raise VerificationError(f"partial history hash mismatch: {claim[6]}")
    expect_failure(
        container,
        jwt_claim_sql(worker_a, role="service_role") + f"""
select * from worker_api.load_paper_execution_checkpoint(
  '{intent_id}','paper-primary','{worker_a}',{token_a},2,
  '{RELEASE_SHA}',clock_timestamp()
);
""",
        "worker_fencing_token_stale",
    )
    checkpoint = psql(container, jwt_claim_sql(worker_b, role="service_role") + f"""
select concat_ws('|',intent_id,latest_sequence,latest_status,
  latest_cumulative_quantity,latest_cumulative_gross_krw,
  latest_cumulative_commission_krw,latest_cumulative_tax_krw,
  observation_history_sha256,intent_release_sha,lease_release_sha)
from worker_api.load_paper_execution_checkpoint(
  '{intent_id}','paper-primary','{worker_b}',{token_b},2,
  '{RELEASE_SHA}',clock_timestamp()
);
""").stdout.strip().splitlines()[-1]
    expected_checkpoint = (
        f"{intent_id}|1|partial_filled|1|9000|9|0|{expected_history_hash}|"
        f"{RELEASE_SHA}|{RELEASE_SHA}"
    )
    if checkpoint != expected_checkpoint:
        raise VerificationError(f"durable checkpoint mismatch: {checkpoint}")

    psql(container, f"""
select pg_sleep(greatest(
  extract(epoch from ('{values['expires_at']}'::timestamptz-clock_timestamp())),0
)+0.1);
""")
    expiry = psql(container, jwt_claim_sql(worker_b, role="service_role") + f"""
select concat_ws('|',observation_id,sequence,state,reason_code,idempotent)
from worker_api.expire_paper_intent_remainder(
  '{intent_id}','{worker_b}',{token_b},2,'{RELEASE_SHA}',clock_timestamp(),
  'paper_day_expired'
);
select concat_ws('|',observation_id,sequence,state,reason_code,idempotent)
from worker_api.expire_paper_intent_remainder(
  '{intent_id}','{worker_b}',{token_b},2,'{RELEASE_SHA}',clock_timestamp(),
  'paper_day_expired'
);
""").stdout.strip().splitlines()[-2:]
    expiry_first = expiry[0].split("|")
    expiry_replay = expiry[1].split("|")
    if expiry_first[1:] != ["2", "complete", "paper_day_expired", "f"] \
            or expiry_replay != [
                expiry_first[0], "2", "complete", "paper_day_expired", "t",
            ]:
        raise VerificationError(f"paper expiry/replay mismatch: {expiry}")
    final_state = psql(container, f"""
select concat_ws('|',
  (select count(*) from private.execution_observations where intent_id='{intent_id}'),
  (select count(*) from private.fills where intent_id='{intent_id}'),
  (select count(*) from private.accounting_transactions
    where correlation_id='{intent_id}' and source_type='fill'),
  (select count(*) from private.accounting_postings as posting
    join private.accounting_transactions as transaction
      on transaction.id=posting.journal_entry_id
    where transaction.correlation_id='{intent_id}' and transaction.source_type='fill'),
  (select settled_cash_krw::bigint from private.cash_balance_projection
    where account_id='paper-primary'),
  (select reserved_cash_krw::bigint from private.cash_balance_projection
    where account_id='paper-primary'),
  (select pending_debit_cash_krw::bigint from private.cash_balance_projection
    where account_id='paper-primary'),
  (select quantity from private.position_projection
    where account_id='paper-primary' and symbol='000660'),
  (select remaining_cash_krw from private.reservation_events
    where intent_id='{intent_id}' order by event_sequence desc limit 1),
  (select state from private.execution_reconciliation_state where intent_id='{intent_id}'),
  (select count(*) from private.delivery_outbox
    where dedupe_key='paper-expiry:{intent_id}'),
  (select count(*) from private.audit_events
    where action='paper_intent_remainder_expired' and correlation_id='{intent_id}')
);
""").stdout.strip()
    if final_state != "2|1|1|3|10000000|0|9009|1|0|complete|1|1":
        raise VerificationError(f"paper expiry atomicity mismatch: {final_state}")
    psql(container, jwt_claim_sql(worker_b, role="service_role") + f"""
select idempotent from worker_api.release_worker_lease(
  'paper-primary','{worker_b}',{token_b},clock_timestamp(),'{RELEASE_SHA}'
);
""")
    print("PASS partial fill ledger, durable restart checkpoint and DAY expiry")


def verify_cash_settlement_maturity(container: str) -> None:
    due_intent = "74747474-7474-4474-8474-747474747474"
    future_intent = "91919191-9191-4191-8191-919191919191"
    future_decision = "92929292-9292-4292-8292-929292929292"
    future_risk = "93939393-9393-4393-8393-939393939393"
    dead_intent = "94949494-9494-4494-8494-949494949494"
    dead_decision = "95959595-9595-4595-8595-959595959595"
    dead_risk = "96969696-9696-4696-8696-969696969696"
    worker = "97979797-9797-4797-8797-979797979797"
    future_schedule_hash = "6" * 64

    lease = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select fencing_token from worker_api.acquire_worker_lease(
  'paper-primary','{worker}',clock_timestamp(),300,'{RELEASE_SHA}'
);
""").stdout.strip().splitlines()[-1]
    fencing_token = int(lease)
    control_epoch = int(psql(container, """
select control_epoch from private.execution_controls
where account_id='paper-primary';
""").stdout.strip())

    psql(container, f"""
insert into private.execution_cost_schedules (
  account_id,schedule_version,schedule_sha256,buy_commission_rate,
  sell_commission_rate,sell_tax_rate,settlement_days,status,evidence_id,
  requested_by,reviewed_by,effective_from,effective_until
) values (
  'paper-primary','settlement-next-session','{future_schedule_hash}',
  0.001,0.001,0.002,1,'approved','{EVIDENCE}','{ADMIN_1}','{ADMIN_2}',
  clock_timestamp()-interval '1 day',clock_timestamp()+interval '1 day'
);
""")

    def create_buy_fill(
        intent_id: str,
        decision_id: str,
        risk_id: str,
        symbol: str,
        schedule_version: str,
        settlement_offset: int,
    ) -> str:
        values = json.loads(psql(container, f"""
with times as (
  select
    date_trunc('minute',clock_timestamp())-interval '1 minute' as decision_at,
    date_trunc('minute',clock_timestamp())-interval '2 minutes' as signal_from,
    date_trunc('minute',clock_timestamp())+interval '10 minutes' as signal_until,
    date_trunc('minute',clock_timestamp()) as eligible_at,
    clock_timestamp()+interval '5 minutes' as expires_at,
    clock_timestamp()-interval '30 seconds' as risk_at,
    clock_timestamp()+interval '5 minutes' as risk_expires
)
select jsonb_build_object(
  'decision_at',decision_at,'signal_from',signal_from,
  'signal_until',signal_until,'eligible_at',eligible_at,
  'expires_at',expires_at,'risk_at',risk_at,'risk_expires',risk_expires,
  'semantic_key',private.compute_order_semantic_key(
    'paper-primary','paper','dedupe-strategy','{symbol}','buy',
    signal_from,signal_until,'dedupe-policy'
  )
)
from times;
""").stdout.strip())
        prepared = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select concat_ws('|',reserved,intent_id,reason_code)
from worker_api.reserve_order_intent(
  '{intent_id}','{values['semantic_key']}','paper-primary','paper',
  'dedupe-strategy','{decision_id}','{'7' * 64}','{risk_id}',true,
  array[]::text[],'{values['risk_at']}','{values['risk_expires']}',
  '{symbol}','buy',1,10000,'{values['decision_at']}','{values['signal_from']}',
  '{values['signal_until']}','dedupe-policy','{schedule_version}','{'1' * 64}',
  10010,'{values['eligible_at']}','{values['expires_at']}',{control_epoch},
  '{worker}',{fencing_token},'{RELEASE_SHA}'
);
select concat_ws('|',attempt_id,reason_code)
from worker_api.mark_dispatch_started(
  '{intent_id}','paper-primary','paper','{worker}',{fencing_token},
  {control_epoch},clock_timestamp(),'{'2' * 64}','paper:{intent_id}'
);
""").stdout.strip().splitlines()[-2:]
        if not prepared[0].startswith(f"t|{intent_id}|") \
                or not prepared[1].endswith("|prepared"):
            raise VerificationError(
                f"cash settlement fixture reserve/dispatch mismatch: {prepared}"
            )

        observed = json.loads(psql(container, f"""
select jsonb_build_object(
  'observed_at',clock_timestamp(),
  'settlement_date',(
    select session_date
    from private.market_calendar_sessions
    where calendar_id='60606060-6060-4060-8060-606060606060'
      and session_date >= (clock_timestamp() at time zone 'Asia/Seoul')::date
      and is_open
    order by session_date
    offset {settlement_offset}
    limit 1
  )
);
""").stdout.strip())
        install_paper_fill_bar_evidence(
            container,
            intent_id=intent_id,
            filled_at=observed["observed_at"],
            label=f"cash-settlement-{intent_id}",
        )
        observation_hash = psql(container, f"""
select encode(extensions.digest(convert_to(concat_ws('|',
  '{intent_id}','1','filled','paper:{intent_id}',
  'paper:{intent_id}:fill:1',
  private.utc_iso8601('{observed['observed_at']}'::timestamptz),
  '1','9000','9','0','1','9000','{observed['settlement_date']}',
  'cash_settlement_verifier'
),'UTF8'),'sha256'),'hex');
""").stdout.strip()
        postings = (
            "jsonb_build_array("
            "jsonb_build_object('account','POSITION_COST','debit_krw',9000,"
            "'credit_krw',0),"
            "jsonb_build_object('account','FEES','debit_krw',9,'credit_krw',0),"
            "jsonb_build_object('account','CASH','debit_krw',0,'credit_krw',9009))"
        )
        recorded = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select concat_ws('|',inserted,quarantined,reason_code)
from worker_api.record_execution_observation(
  '{intent_id}',1,'filled','paper:{intent_id}',
  'paper:{intent_id}:fill:1','{observation_hash}',
  '{observed['observed_at']}',1,9000,9,0,1,9000,
  '{observed['settlement_date']}',{postings},'cash_settlement_verifier',
  '{worker}',{fencing_token}
);
""").stdout.strip().splitlines()[-1]
        if recorded != "t|f|recorded":
            raise VerificationError(
                f"cash settlement fixture fill mismatch: {recorded}"
            )
        obligation_id = psql(container, f"""
select obligation.id
from private.cash_settlement_obligations as obligation
where obligation.intent_id='{intent_id}';
""").stdout.strip()
        if not obligation_id:
            raise VerificationError("cash settlement obligation was not created")
        return obligation_id

    future_obligation = create_buy_fill(
        future_intent,
        future_decision,
        future_risk,
        "068270",
        "settlement-next-session",
        1,
    )
    due_obligation = psql(container, f"""
select id from private.cash_settlement_obligations
where intent_id='{due_intent}';
""").stdout.strip()
    if not due_obligation:
        raise VerificationError("partial-fill due settlement obligation is missing")

    due_count = psql(container, jwt_claim_sql(worker, role="service_role") + """
select due_count from worker_api.list_due_cash_settlement_accounts(
  clock_timestamp(),20
) where account_id='paper-primary';
""").stdout.strip().splitlines()[-1]
    boundary_state = psql(container, f"""
select concat_ws('|',
  (select settlement_date=(trade_at at time zone 'Asia/Seoul')::date
   from private.cash_settlement_obligations where id='{due_obligation}'),
  (select settlement_date>(clock_timestamp() at time zone 'Asia/Seoul')::date
   from private.cash_settlement_obligations where id='{future_obligation}'),
  (select pending_debit_cash_krw from private.account_snapshots
   where account_id='paper-primary' order by sequence desc limit 1),
  (select pending_debit_cash_krw from private.cash_balance_projection
   where account_id='paper-primary'),
  position(
    'session_date >= (p_observed_at at time zone ''Asia/Seoul'')::date'
    in pg_get_functiondef(
      'private.record_execution_observation_impl(uuid,integer,text,text,text,text,timestamptz,bigint,bigint,bigint,bigint,bigint,bigint,date,jsonb,text,text,bigint)'::regprocedure
    )
  ) > 0
);
""").stdout.strip().splitlines()[-1]
    boundary_parts = boundary_state.split("|")
    if (
        boundary_parts[:2] != ["t", "t"]
        or due_count != "1"
        or boundary_parts[2] != boundary_parts[3]
        or boundary_parts[4] != "t"
    ):
        raise VerificationError(
            f"cash settlement due/future/KST/snapshot mismatch: {boundary_state}"
        )

    expect_failure(
        container,
        f"""
begin;
alter table private.cash_settlement_obligations
  disable trigger guard_cash_settlement_obligation_scope_v1;
insert into private.cash_settlement_obligations (
  id,fill_id,trade_accounting_transaction_id,
  settlement_reclassification_transaction_id,intent_id,account_id,
  environment,obligation_type,amount_krw,trade_at,settlement_date,
  obligation_sha256,source_release_sha
)
select gen_random_uuid(),fill_id,trade_accounting_transaction_id,
  settlement_reclassification_transaction_id,intent_id,account_id,
  environment,obligation_type,amount_krw,
  '2026-01-01T15:30:00Z','2026-01-01','{'4' * 64}',source_release_sha
from private.cash_settlement_obligations where id='{due_obligation}';
""",
        "cash_settlement_obligation_date_check",
    )
    expect_failure(
        container,
        f"""
insert into private.cash_settlement_obligations (
  id,fill_id,trade_accounting_transaction_id,
  settlement_reclassification_transaction_id,intent_id,account_id,
  environment,obligation_type,amount_krw,trade_at,settlement_date,
  obligation_sha256,source_release_sha
)
select gen_random_uuid(),fill_id,trade_accounting_transaction_id,
  settlement_reclassification_transaction_id,intent_id,account_id,
  environment,obligation_type,amount_krw+1,trade_at,settlement_date,
  '{'5' * 64}',source_release_sha
from private.cash_settlement_obligations where id='{due_obligation}';
""",
        "cash_settlement_obligation_scope_mismatch",
    )

    claim_row = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select concat_ws('|',obligation_id,revision,claim_token)
from worker_api.claim_cash_settlement_batch(
  'paper-primary','{worker}','{RELEASE_SHA}',{fencing_token},
  clock_timestamp(),1
);
""").stdout.strip().splitlines()[-1].split("|")
    if claim_row[0] != due_obligation:
        raise VerificationError(f"cash settlement due claim mismatch: {claim_row}")
    claim_revision = int(claim_row[1])
    claim_token = claim_row[2]
    expect_failure(
        container,
        jwt_claim_sql(worker, role="service_role") + f"""
select worker_api.complete_cash_settlement(
  '{due_obligation}',{claim_revision},'{claim_token}','{worker}',
  '{RELEASE_SHA}',{fencing_token + 1},clock_timestamp()
);
""",
        "cash_settlement_claim_not_owned_current_or_expired",
    )
    receipts = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select concat_ws('|',value->>'replayed',value->>'claim_revision',
  value->>'settled_revision')
from (select worker_api.complete_cash_settlement(
  '{due_obligation}',{claim_revision},'{claim_token}','{worker}',
  '{RELEASE_SHA}',{fencing_token},clock_timestamp()) as value) as result;
select concat_ws('|',value->>'replayed',value->>'claim_revision',
  value->>'settled_revision')
from (select worker_api.complete_cash_settlement(
  '{due_obligation}',{claim_revision},'{claim_token}','{worker}',
  '{RELEASE_SHA}',{fencing_token},clock_timestamp()) as value) as result;
""").stdout.strip().splitlines()[-2:]
    expected_settled_revision = str(claim_revision + 1)
    if receipts != [
        f"false|{claim_revision}|{expected_settled_revision}",
        f"true|{claim_revision}|{expected_settled_revision}",
    ]:
        raise VerificationError(f"cash settlement exact replay mismatch: {receipts}")
    settled_state = psql(container, f"""
select concat_ws('|',
  (select state from private.cash_settlement_state
   where obligation_id='{due_obligation}'),
  (select count(*) from private.accounting_transactions
   where source_type='cash_settlement'
     and correlation_id='{due_intent}'),
  (select count(*) from private.cash_settlement_events
   where obligation_id='{due_obligation}' and event_type='settled'),
  (select pending_debit_cash_krw from private.account_snapshots
   where account_id='paper-primary' order by sequence desc limit 1),
  (select pending_debit_cash_krw from private.cash_balance_projection
   where account_id='paper-primary')
);
""").stdout.strip()
    settled_parts = settled_state.split("|")
    if (
        settled_parts[:3] != ["settled", "1", "1"]
        or settled_parts[3] != settled_parts[4]
    ):
        raise VerificationError(
            f"cash settlement completion projection mismatch: {settled_state}"
        )

    dead_obligation = create_buy_fill(
        dead_intent,
        dead_decision,
        dead_risk,
        "096770",
        "dedupe-cost",
        0,
    )
    psql(container, f"""
alter table private.cash_settlement_state
  disable trigger guard_cash_settlement_state_transition_v1;
update private.cash_settlement_state
set attempt_count=7,available_at=clock_timestamp()-interval '1 second',
    updated_at=greatest(clock_timestamp(),updated_at+interval '1 microsecond')
where obligation_id='{dead_obligation}' and state='pending';
alter table private.cash_settlement_state
  enable trigger guard_cash_settlement_state_transition_v1;
""")
    dead_claim = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select concat_ws('|',obligation_id,revision,claim_token)
from worker_api.claim_cash_settlement_batch(
  'paper-primary','{worker}','{RELEASE_SHA}',{fencing_token},
  clock_timestamp(),1
);
""").stdout.strip().splitlines()[-1].split("|")
    if dead_claim[0] != dead_obligation:
        raise VerificationError(f"cash settlement dead-letter claim mismatch: {dead_claim}")
    dead_revision = int(dead_claim[1])
    dead_token = dead_claim[2]
    failures = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select concat_ws('|',value->>'state',value->>'attempt_count',value->>'replayed')
from (select worker_api.fail_cash_settlement_attempt(
  '{dead_obligation}',{dead_revision},'{dead_token}','{worker}',
  '{RELEASE_SHA}',{fencing_token},clock_timestamp(),
  'settlement_worker_error') as value) as result;
select concat_ws('|',value->>'state',value->>'attempt_count',value->>'replayed')
from (select worker_api.fail_cash_settlement_attempt(
  '{dead_obligation}',{dead_revision},'{dead_token}','{worker}',
  '{RELEASE_SHA}',{fencing_token},clock_timestamp(),
  'settlement_worker_error') as value) as result;
""").stdout.strip().splitlines()[-2:]
    if failures != ["dead_letter|8|false", "dead_letter|8|true"]:
        raise VerificationError(
            f"cash settlement attempt-8 dead-letter mismatch: {failures}"
        )
    dead_state = psql(container, f"""
select concat_ws('|',state,attempt_count,
  (select count(*) from private.cash_settlement_events as event
   where event.obligation_id=state_row.obligation_id
     and event.event_type='dead_letter'),
  (select count(*) from private.incidents
   where incident_type='cash_settlement_dead_letter'
     and correlation_id='{dead_intent}'),
  (select count(*) from private.delivery_outbox
   where dedupe_key='cash-settlement-dead-letter:'||state_row.obligation_id::text)
)
from private.cash_settlement_state as state_row
where obligation_id='{dead_obligation}';
""").stdout.strip()
    if dead_state != "dead_letter|8|1|1|1":
        raise VerificationError(f"cash settlement dead-letter evidence mismatch: {dead_state}")

    psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select idempotent from worker_api.release_worker_lease(
  'paper-primary','{worker}',{fencing_token},clock_timestamp(),'{RELEASE_SHA}'
);
""")
    # Isolate the following legacy workflow fixture after proving the
    # dead-letter stop. The disposable setup resolves only its synthetic cash
    # break with already-reviewed fixture evidence, then re-opens execution
    # while the production qualification trigger is temporarily disabled.
    psql(container, f"""
update private.reconciliation_breaks
set state='resolved',evidence_id='{EVIDENCE}',
    resolution_command_id='{ACCOUNT_COMMAND}',resolved_at=clock_timestamp(),
    revision=revision+1
where account_id='paper-primary' and break_type='cash'
  and summary_code='settlement_worker_error' and state='open';
alter table private.execution_controls
  disable trigger guard_execution_control_qualification_freshness_v1;
update private.execution_controls
set execution_enabled=true,
    control_epoch=control_epoch+1,
    effective_at=greatest(clock_timestamp(),updated_at+interval '1 microsecond'),
    expires_at=clock_timestamp()+interval '1 day',
    updated_reason_code='verifier_next_isolated_case',
    updated_at=greatest(clock_timestamp(),updated_at+interval '1 microsecond')
where account_id='paper-primary';
alter table private.execution_controls
  enable trigger guard_execution_control_qualification_freshness_v1;
""")
    print(
        "PASS KST settlement boundary, due/future claims, exact replay, stale "
        "fence, snapshot pending cash, scope guard and attempt-8 dead-letter"
    )


def verify_unknown_resolution_v2(container: str) -> None:
    worker = "a1a1a1a1-a1a1-41a1-81a1-a1a1a1a1a1a1"
    cases = [
        {
            "name": "buy",
            "intent": "a2a2a2a2-a2a2-42a2-82a2-a2a2a2a2a2a2",
            "decision": "a3a3a3a3-a3a3-43a3-83a3-a3a3a3a3a3a3",
            "risk": "a4a4a4a4-a4a4-44a4-84a4-a4a4a4a4a4a4",
            "request": "a5a5a5a5-a5a5-45a5-85a5-a5a5a5a5a5a5",
            "review": "a6a6a6a6-a6a6-46a6-86a6-a6a6a6a6a6a6",
            "idempotency": "a7a7a7a7-a7a7-47a7-87a7-a7a7a7a7a7a7",
            "symbol": "035720",
            "side": "buy",
            "quantity": 2,
            "limit": 10000,
            "reserved_cash": 20020,
            "terminal": "filled",
            "fill_quantity": 2,
            "commission": 18,
            "tax": 0,
        },
        {
            "name": "sell",
            "intent": "b2a2a2a2-a2a2-42a2-82a2-a2a2a2a2a2a2",
            "decision": "b3a3a3a3-a3a3-43a3-83a3-a3a3a3a3a3a3",
            "risk": "b4a4a4a4-a4a4-44a4-84a4-a4a4a4a4a4a4",
            "request": "b5a5a5a5-a5a5-45a5-85a5-a5a5a5a5a5a5",
            "review": "b6a6a6a6-a6a6-46a6-86a6-a6a6a6a6a6a6",
            "idempotency": "b7a7a7a7-a7a7-47a7-87a7-a7a7a7a7a7a7",
            "symbol": "000660",
            "side": "sell",
            "quantity": 1,
            "limit": 8000,
            "reserved_cash": 0,
            "terminal": "filled",
            "fill_quantity": 1,
            "commission": 9,
            "tax": 18,
        },
        {
            "name": "no_fill",
            "intent": "c2a2a2a2-a2a2-42a2-82a2-a2a2a2a2a2a2",
            "decision": "c3a3a3a3-a3a3-43a3-83a3-a3a3a3a3a3a3",
            "risk": "c4a4a4a4-a4a4-44a4-84a4-a4a4a4a4a4a4",
            "request": "c5a5a5a5-a5a5-45a5-85a5-a5a5a5a5a5a5",
            "review": "c6a6a6a6-a6a6-46a6-86a6-a6a6a6a6a6a6",
            "idempotency": "c7a7a7a7-a7a7-47a7-87a7-a7a7a7a7a7a7",
            "symbol": "051910",
            "side": "buy",
            "quantity": 1,
            "limit": 10000,
            "reserved_cash": 10010,
            "terminal": "rejected",
            "fill_quantity": 0,
            "commission": 0,
            "tax": 0,
        },
    ]
    lease = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select fencing_token from worker_api.acquire_worker_lease(
  'paper-primary','{worker}',clock_timestamp(),300,'{RELEASE_SHA}'
);
""").stdout.strip().splitlines()[-1]
    fencing_token = int(lease)
    control = psql(container, """
select concat_ws('|',execution_enabled,control_epoch)
from private.execution_controls where account_id='paper-primary';
""").stdout.strip().split("|")
    if control[0] != "t":
        raise VerificationError(f"unknown V2 fixture control is not enabled: {control}")
    dispatch_epoch = int(control[1])
    kst_contracts = psql(container, """
select concat_ws('|',
  position(
    'session.session_date = (p_eligible_at at time zone ''Asia/Seoul'')::date'
    in pg_get_functiondef((select min(proc.oid)
      from pg_proc as proc join pg_namespace as namespace
        on namespace.oid=proc.pronamespace
      where namespace.nspname='private'
        and proc.proname='reserve_order_intent_impl'))
  ) > 0,
  position(
    'session.session_date >= (filled_time at time zone ''Asia/Seoul'')::date'
    in pg_get_functiondef(
      'private.assert_unknown_resolution_fill_manifest_v2(uuid,uuid,text,text,jsonb,timestamptz)'::regprocedure
    )
  ) > 0
);
""").stdout.strip()
    if kst_contracts != "t|t":
        raise VerificationError(f"KST reserve/unknown contract patch missing: {kst_contracts}")

    for case in cases:
        values = json.loads(psql(container, f"""
with boundary as (
  select
    case
      when clock_timestamp() - (
        date_trunc('day',clock_timestamp() at time zone 'Asia/Seoul')
          at time zone 'Asia/Seoul'
      ) >= interval '3 minutes'
      then date_trunc('day',clock_timestamp() at time zone 'Asia/Seoul')
        at time zone 'Asia/Seoul'
      else (
        date_trunc('day',clock_timestamp() at time zone 'Asia/Seoul')
          at time zone 'Asia/Seoul'
      ) - interval '1 day'
    end as boundary_midnight,
    clock_timestamp() as observed_now
), times as (
  select
    boundary_midnight as decision_at,
    boundary_midnight as signal_from,
    observed_now+interval '10 minutes' as signal_until,
    boundary_midnight+interval '1 minute' as eligible_at,
    boundary_midnight+interval '2 minutes' as boundary_filled_at,
    observed_now+interval '5 minutes' as expires_at,
    observed_now-interval '30 seconds' as risk_at,
    observed_now+interval '5 minutes' as risk_expires
  from boundary
)
select jsonb_build_object(
  'decision_at',decision_at,'signal_from',signal_from,
  'signal_until',signal_until,'eligible_at',eligible_at,
  'boundary_filled_at',boundary_filled_at,
  'expires_at',expires_at,'risk_at',risk_at,'risk_expires',risk_expires,
  'semantic_key',private.compute_order_semantic_key(
    'paper-primary','paper','dedupe-strategy','{case['symbol']}',
    '{case['side']}',signal_from,signal_until,'dedupe-policy'
  )
) from times;
""").stdout.strip())
        case["boundary_filled_at"] = values["boundary_filled_at"]
        prepared = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select concat_ws('|',reserved,intent_id,reason_code)
from worker_api.reserve_order_intent(
  '{case['intent']}','{values['semantic_key']}','paper-primary','paper',
  'dedupe-strategy','{case['decision']}','{'7' * 64}','{case['risk']}',true,
  array[]::text[],'{values['risk_at']}','{values['risk_expires']}',
  '{case['symbol']}','{case['side']}',{case['quantity']},{case['limit']},
  '{values['decision_at']}','{values['signal_from']}','{values['signal_until']}',
  'dedupe-policy','dedupe-cost','{'1' * 64}',{case['reserved_cash']},
  '{values['eligible_at']}','{values['expires_at']}',{dispatch_epoch},
  '{worker}',{fencing_token},'{RELEASE_SHA}'
);
select concat_ws('|',attempt_id,reason_code)
from worker_api.mark_dispatch_started(
  '{case['intent']}','paper-primary','paper','{worker}',{fencing_token},
  {dispatch_epoch},clock_timestamp(),'{'2' * 64}','paper:{case['intent']}'
);
""").stdout.strip().splitlines()[-2:]
        if not prepared[0].startswith(f"t|{case['intent']}|") \
                or not prepared[1].endswith("|prepared"):
            raise VerificationError(
                f"unknown V2 {case['name']} reserve/dispatch mismatch: {prepared}"
            )
        if case["fill_quantity"]:
            install_paper_fill_bar_evidence(
                container,
                intent_id=case["intent"],
                filled_at=values["boundary_filled_at"],
                label=f"unknown-v2-{case['name']}",
            )

    for case in cases:
        observed_at = psql(container, f"""
select '{case['boundary_filled_at']}'::timestamptz-interval '30 seconds';
""").stdout.strip()
        reason_code = "provider_state_ambiguous"
        observation_hash = psql(container, f"""
select encode(extensions.digest(convert_to(concat_ws('|',
  '{case['intent']}','1','unknown_requires_manual_check',
  'paper:{case['intent']}','',private.utc_iso8601('{observed_at}'::timestamptz),
  '0','0','0','0','','','', '{reason_code}'
),'UTF8'),'sha256'),'hex');
""").stdout.strip()
        unknown = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select concat_ws('|',observation_id,inserted,quarantined,reason_code)
from worker_api.record_execution_observation(
  '{case['intent']}',1,'unknown_requires_manual_check',
  'paper:{case['intent']}',null,'{observation_hash}','{observed_at}',
  0,0,0,0,null,null,null,'[]'::jsonb,'{reason_code}',
  '{worker}',{fencing_token}
);
reset role;
select event.event_summary->>'reconciliation_break_id'
from private.order_events as event
where event.intent_id='{case['intent']}'
  and event.event_type='manual_check_quarantined'
order by event.occurred_at desc limit 1;
""").stdout.strip().splitlines()[-2:]
        unknown_parts = unknown[0].split("|")
        if unknown_parts[1:] != ["t", "f", "recorded"] or not unknown[1]:
            raise VerificationError(
                f"unknown V2 {case['name']} quarantine mismatch: {unknown}"
            )
        case["unknown_observation"] = unknown_parts[0]
        case["break"] = unknown[1]

    resolution_epoch = int(psql(container, """
select control_epoch from private.execution_controls
where account_id='paper-primary';
""").stdout.strip())
    if resolution_epoch <= dispatch_epoch:
        raise VerificationError("unknown V2 quarantine did not advance control epoch")

    for index, case in enumerate(cases):
        evidence_hash = hashlib.sha256(
            f"unknown-v2-{case['name']}-evidence".encode()
        ).hexdigest()
        snapshot = json.loads(psql(container, f"""
select jsonb_build_object(
  'break_revision',(select revision from private.reconciliation_breaks
    where id='{case['break']}'),
  'cash_version',(select projection_version from private.cash_balance_projection
    where account_id='paper-primary'),
  'position_version',(select projection_version from private.position_projection
    where account_id='paper-primary' and symbol='{case['symbol']}'),
  'reservation_sequence',(select max(event_sequence)
    from private.reservation_events where intent_id='{case['intent']}'),
  'requested_at',clock_timestamp(),
  'evidence_captured_at',clock_timestamp()-interval '1 second',
  'filled_at','{case['boundary_filled_at']}'::timestamptz,
  'settlement_date',(select session_date
    from private.market_calendar_sessions
    where calendar_id='60606060-6060-4060-8060-606060606060'
      and session_date >= (
        '{case['boundary_filled_at']}'::timestamptz at time zone 'Asia/Seoul'
      )::date
      and is_open order by session_date limit 1)
);
""").stdout.strip())
        position_version = (
            "null"
            if snapshot["position_version"] is None
            else str(snapshot["position_version"])
        )
        if case["fill_quantity"]:
            missing_fills = (
                "jsonb_build_array(jsonb_build_object("
                "'fill_sequence',1,"
                f"'provider_order_id','paper:{case['intent']}',"
                f"'provider_execution_id','paper:{case['intent']}:resolved:1',"
                f"'quantity',{case['fill_quantity']},'price_krw',9000,"
                f"'commission_krw',{case['commission']},'tax_krw',{case['tax']},"
                f"'filled_at','{snapshot['filled_at']}'::timestamptz,"
                f"'settlement_date','{snapshot['settlement_date']}'::date,"
                f"'evidence_sha256','{evidence_hash}'"
                "))"
            )
        else:
            missing_fills = "'[]'::jsonb"
        request_draft = f"jsonb_build_object(" \
            "'schema_version',2," \
            f"'request_id','{case['request']}','environment','paper'," \
            f"'idempotency_key','{case['idempotency']}'," \
            "'command_type','close_unknown_execution'," \
            f"'break_id','{case['break']}','intent_id','{case['intent']}'," \
            f"'unknown_observation_id','{case['unknown_observation']}'," \
            f"'provider_order_id','paper:{case['intent']}'," \
            f"'evidence_artifact_uri','urn:sha256:{evidence_hash}'," \
            f"'evidence_sha256','{evidence_hash}'," \
            f"'evidence_captured_at','{snapshot['evidence_captured_at']}'::timestamptz," \
            "'reason_code','accounting_closure_requested'," \
            "'expected_break_state','open'," \
            f"'expected_break_revision',{snapshot['break_revision']}," \
            "'expected_reconciliation_state','manual'," \
            f"'expected_cash_projection_version',{snapshot['cash_version']}," \
            f"'expected_position_projection_version',{position_version}," \
            f"'expected_reservation_event_sequence',{snapshot['reservation_sequence']}," \
            f"'expected_control_epoch',{resolution_epoch}," \
            f"'terminal_status','{case['terminal']}'," \
            f"'missing_fills',{missing_fills}," \
            f"'requested_at','{snapshot['requested_at']}'::timestamptz," \
            f"'expires_at','{snapshot['requested_at']}'::timestamptz+interval '1 hour')"
        requested_raw = psql(container, jwt_claim_sql(OPERATOR) + f"""
with draft(value) as (select {request_draft}), grant_value(value) as (
  select api.issue_unknown_resolution_step_up_v2(jsonb_build_object(
    'schema_version',2,'bound_action','request',
    'bound_command_type','close_unknown_execution','command_payload',draft.value
  )) from draft
)
select api.request_unknown_resolution_v2(draft.value || grant_value.value)
from draft,grant_value;
""").stdout.strip().splitlines()[-1]
        requested = json.loads(requested_raw)
        if (
            requested["state"] != "requested"
            or requested["receipt_revision"] != 0
            or requested["accounting_mutation_allowed"] is not False
            or requested["resolution_complete"] is not False
        ):
            raise VerificationError(
                f"unknown V2 {case['name']} request mismatch: {requested}"
            )

        def review_draft(review_id: str) -> str:
            return (
                "jsonb_build_object('schema_version',2,"
                f"'review_id','{review_id}','command_id','{case['request']}',"
                "'command_type','close_unknown_execution',"
                "'reviewer_role','risk_approver','decision','approve',"
                "'reason_code','evidence_sufficient',"
                f"'expected_receipt_revision',{requested['receipt_revision']},"
                f"'expected_break_revision',{requested['break_revision']},"
                f"'request_digest_sha256','{requested['request_digest_sha256']}',"
                f"'evidence_sha256','{evidence_hash}',"
                "'reviewed_at',clock_timestamp())"
            )

        if index == 0:
            self_review_id = "a8a8a8a8-a8a8-48a8-88a8-a8a8a8a8a8a8"
            self_draft = review_draft(self_review_id)
            expect_failure(
                container,
                f"""
begin;
insert into private.role_assignments (user_id,role,reason)
values ('{OPERATOR}','risk_approver','unknown_v2_self_review_fixture');
{jwt_claim_sql(OPERATOR)}
with draft(value) as (select {self_draft}), grant_value(value) as (
  select api.issue_unknown_resolution_step_up_v2(jsonb_build_object(
    'schema_version',2,'bound_action','review',
    'bound_command_type','close_unknown_execution','command_payload',draft.value
  )) from draft
)
select api.review_unknown_resolution_v2(draft.value || grant_value.value)
from draft,grant_value;
""",
                "unknown_resolution_v2_self_review_forbidden",
            )

        approved_draft = review_draft(case["review"])
        approved_raw = psql(container, jwt_claim_sql(RISK) + f"""
with draft(value) as (select {approved_draft}), grant_value(value) as (
  select api.issue_unknown_resolution_step_up_v2(jsonb_build_object(
    'schema_version',2,'bound_action','review',
    'bound_command_type','close_unknown_execution','command_payload',draft.value
  )) from draft
)
select api.review_unknown_resolution_v2(draft.value || grant_value.value)
from draft,grant_value;
""").stdout.strip().splitlines()[-1]
        approved = json.loads(approved_raw)
        if (
            approved["state"] != "approved"
            or approved["receipt_revision"] != 1
            or approved["work_revision"] != 0
            or approved["accounting_mutation_allowed"] is not True
            or approved["resolution_complete"] is not False
        ):
            raise VerificationError(
                f"unknown V2 {case['name']} review mismatch: {approved}"
            )

        generic_count = psql(
            container,
            jwt_claim_sql(worker, role="service_role") + f"""
select count(*) from worker_api.claim_operation_command_batch(
  'paper-primary','{worker}','{RELEASE_SHA}',{fencing_token},
  clock_timestamp(),25
) where command_id='{case['request']}';
""",
        ).stdout.strip().splitlines()[-1]
        if generic_count != "0":
            raise VerificationError(
                f"generic command claim accepted unknown V2: {generic_count}"
            )
        if index == 0:
            expect_failure(
                container,
                jwt_claim_sql(worker, role="service_role") + f"""
select * from worker_api.claim_unknown_resolution_v2(
  '{case['request']}','{worker}','{RELEASE_SHA}',{fencing_token},
  {approved['receipt_revision']},{approved['work_revision'] + 1},
  clock_timestamp()
);
""",
                "unknown_resolution_v2_claim_stale_or_not_claimable",
            )
        claim = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select concat_ws('|',command_id,claim_token,command_revision,work_revision)
from worker_api.claim_unknown_resolution_v2(
  '{case['request']}','{worker}','{RELEASE_SHA}',{fencing_token},
  {approved['receipt_revision']},{approved['work_revision']},clock_timestamp()
);
""").stdout.strip().splitlines()[-1].split("|")
        command_revision = int(claim[2])
        work_revision = int(claim[3])
        claim_token = claim[1]
        if claim[0] != case["request"]:
            raise VerificationError(
                f"unknown V2 {case['name']} dedicated claim mismatch: {claim}"
            )

        if index == 0:
            expect_failure(
                container,
                jwt_claim_sql(worker, role="service_role") + f"""
select * from worker_api.acknowledge_operation_command(
  '{case['request']}','applied','paper-primary','{worker}','{RELEASE_SHA}',
  {fencing_token},{command_revision},clock_timestamp(),
  '{{}}'::jsonb,null
);
""",
                "operation_command_not_worker_applicable",
            )
            expect_failure(
                container,
                jwt_claim_sql(worker, role="service_role") + f"""
select * from worker_api.acknowledge_operation_command(
  '{case['request']}','failed','paper-primary','{worker}','{RELEASE_SHA}',
  {fencing_token},{command_revision},clock_timestamp(),
  '{{}}'::jsonb,'generic_ack_bypass_attempt'
);
""",
                "operation_command_not_worker_applicable",
            )
            for token_value, fence_value, epoch_value, expected_fragment in (
                (
                    "00000000-0000-4000-8000-000000000001",
                    fencing_token,
                    resolution_epoch,
                    "unknown_resolution_v2_apply_state_stale",
                ),
                (
                    claim_token,
                    fencing_token + 1,
                    resolution_epoch,
                    "unknown_resolution_v2_apply_gate_stale",
                ),
                (
                    claim_token,
                    fencing_token,
                    resolution_epoch + 1,
                    "unknown_resolution_v2_apply_gate_stale",
                ),
            ):
                expect_failure(
                    container,
                    jwt_claim_sql(worker, role="service_role") + f"""
select worker_api.apply_unknown_resolution_v2(
  '{case['request']}','{token_value}','{worker}','{RELEASE_SHA}',
  {fence_value},{command_revision},{work_revision},{epoch_value},
  clock_timestamp()
);
""",
                    expected_fragment,
                )

        if case["side"] == "sell":
            expect_failure(
                container,
                f"""
begin;
update private.position_projection
set average_cost_krw=average_cost_krw+1
where account_id='paper-primary' and symbol='{case['symbol']}';
{jwt_claim_sql(worker, role="service_role")}
select worker_api.apply_unknown_resolution_v2(
  '{case['request']}','{claim_token}','{worker}','{RELEASE_SHA}',
  {fencing_token},{command_revision},{work_revision},{resolution_epoch},
  clock_timestamp()
);
rollback;
""",
                "sell_fill_position_cost_not_pinned",
            )

        before_counts = psql(container, f"""
select concat_ws('|',
  (select count(*) from private.fills where intent_id='{case['intent']}'),
  (select count(*) from private.accounting_transactions
   where correlation_id='{case['intent']}'),
  (select count(*) from private.cash_settlement_obligations
   where intent_id='{case['intent']}'),
  coalesce((select bool_and(
      obligation.settlement_date =
        (obligation.trade_at at time zone 'Asia/Seoul')::date
      and obligation.trade_at::date <> obligation.settlement_date
    ) from private.cash_settlement_obligations as obligation
    where obligation.intent_id='{case['intent']}'),true)
);
""").stdout.strip()
        applied_raw = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select worker_api.apply_unknown_resolution_v2(
  '{case['request']}','{claim_token}','{worker}','{RELEASE_SHA}',
  {fencing_token},{command_revision},{work_revision},{resolution_epoch},
  clock_timestamp()
);
""").stdout.strip().splitlines()[-1]
        applied = json.loads(applied_raw)
        if (
            applied["state"] != "applied"
            or applied["receipt_revision"] != command_revision + 1
            or applied["work_revision"] != work_revision + 1
            or applied["resolution_complete"] is not True
            or applied["inserted"] is not True
        ):
            raise VerificationError(
                f"unknown V2 {case['name']} application mismatch: {applied}"
            )
        replay_raw = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select worker_api.apply_unknown_resolution_v2(
  '{case['request']}','{claim_token}','{worker}','{RELEASE_SHA}',
  {fencing_token},{applied['receipt_revision']},{applied['work_revision']},
  {resolution_epoch},clock_timestamp()
);
""").stdout.strip().splitlines()[-1]
        replay = json.loads(replay_raw)
        if (
            replay["inserted"] is not False
            or replay["application_id"] != applied["application_id"]
            or replay["application_sha256"] != applied["application_sha256"]
        ):
            raise VerificationError(
                f"unknown V2 {case['name']} exact replay mismatch: {replay}"
            )
        after_counts = psql(container, f"""
select concat_ws('|',
  (select count(*) from private.fills where intent_id='{case['intent']}'),
  (select count(*) from private.accounting_transactions
   where correlation_id='{case['intent']}'),
  (select count(*) from private.cash_settlement_obligations
   where intent_id='{case['intent']}'),
  coalesce((select bool_and(
      obligation.settlement_date =
        (obligation.trade_at at time zone 'Asia/Seoul')::date
      and obligation.trade_at::date <> obligation.settlement_date
    ) from private.cash_settlement_obligations as obligation
    where obligation.intent_id='{case['intent']}'),true)
);
""").stdout.strip()
        expected_fill_count = 1 if case["fill_quantity"] else 0
        if case["fill_quantity"]:
            # fill + settlement reclassification are both immutable journals.
            expected_transaction_count = 2
            expected_obligation_count = 1
        else:
            expected_transaction_count = 0
            expected_obligation_count = 0
        if before_counts != "0|0|0|t" or after_counts != (
            f"{expected_fill_count}|{expected_transaction_count}|"
            f"{expected_obligation_count}|t"
        ):
            raise VerificationError(
                f"unknown V2 {case['name']} replay cardinality mismatch: "
                f"{before_counts} -> {after_counts}"
            )
        final_state = psql(container, f"""
select concat_ws('|',
  (select state from private.operation_commands where id='{case['request']}'),
  (select state from private.reconciliation_breaks where id='{case['break']}'),
  (select state from private.execution_reconciliation_state
   where intent_id='{case['intent']}'),
  (select status from private.incidents
   where incident_type='execution_unknown'
     and correlation_id='{case['intent']}' order by opened_at desc limit 1),
  (select count(*) from private.unknown_execution_resolution_applications_v2
   where command_id='{case['request']}'),
  (select coalesce(sum(case when posting.side='debit' then posting.amount_krw
      else -posting.amount_krw end),0)
   from private.accounting_postings as posting
   join private.accounting_transactions as transaction
     on transaction.id=posting.journal_entry_id
   where transaction.correlation_id='{case['intent']}')
);
""").stdout.strip()
        expected_balance = "0.0000" if case["fill_quantity"] else "0"
        if final_state != (
            f"applied|resolved|complete|resolved|1|{expected_balance}"
        ):
            raise VerificationError(
                f"unknown V2 {case['name']} final invariant mismatch: {final_state}"
            )

    outcome_state = psql(container, f"""
select concat_ws('|',
  (select quantity from private.position_projection
   where account_id='paper-primary' and symbol='035720'),
  (select quantity from private.position_projection
   where account_id='paper-primary' and symbol='000660'),
  (select count(*) from private.fills
   where intent_id='{cases[2]['intent']}'),
  (select remaining_cash_krw from private.reservation_events
   where intent_id='{cases[2]['intent']}' order by event_sequence desc limit 1)
);
""").stdout.strip()
    if outcome_state != "2|0|0|0":
        raise VerificationError(
            f"unknown V2 buy/sell/no-fill outcomes mismatch: {outcome_state}"
        )

    sell_case = cases[1]
    sell_checkpoint_state = psql(container, f"""
select concat_ws('|',
  intent.position_cost_basis_method='moving_weighted_average_v1',
  intent.position_quantity_snapshot >= intent.quantity,
  intent.position_total_cost_krw = round(
    intent.position_quantity_snapshot * intent.position_average_cost_krw
  )::bigint,
  intent.position_cost_basis_sha256 = pg_catalog.encode(
    extensions.digest(
      pg_catalog.convert_to(
        jsonb_build_object(
          'method','moving_weighted_average_v1',
          'account_id',intent.account_id,
          'symbol',intent.symbol,
          'quantity',intent.position_quantity_snapshot,
          'average_cost_krw_4dp',to_char(
            intent.position_average_cost_krw,
            'FM99999999999999999999.0000'
          ),
          'total_cost_krw',intent.position_total_cost_krw,
          'projection_version',intent.position_projection_version
        )::text,
        'UTF8'
      ),
      'sha256'
    ),
    'hex'
  ),
  exists (
    select 1
    from private.accounting_transactions as transaction
    join private.accounting_postings as posting
      on posting.journal_entry_id=transaction.id
    join private.ledger_accounts as ledger
      on ledger.id=posting.ledger_account_id
    where transaction.correlation_id=intent.id
      and transaction.source_type='fill'
      and ledger.ledger_code='POSITION_COST'
      and posting.side='credit'
      and posting.amount_krw = floor(
        intent.position_total_cost_krw::numeric
          * {sell_case['fill_quantity']}
          / intent.position_quantity_snapshot
      )
  )
)
from private.order_intents as intent
where intent.id='{sell_case['intent']}';
""").stdout.strip()
    if sell_checkpoint_state != "t|t|t|t|t":
        raise VerificationError(
            "unknown V2 sell checkpoint accounting mismatch: "
            f"{sell_checkpoint_state}"
        )

    expected_cases = {case["intent"]: case for case in cases}
    for role_name, actor in (
        ("operator", OPERATOR),
        ("risk_approver", RISK),
        ("auditor", AUDITOR),
    ):
        projection_raw = psql(
            container,
            jwt_claim_sql(actor) + "select api.get_unknown_resolution_cases_v2();",
        ).stdout.strip().splitlines()[-1]
        projection = json.loads(projection_raw)
        if projection.get("schema_version") != 2 \
                or not isinstance(projection.get("cases"), list):
            raise VerificationError(
                f"unknown V2 {role_name} projection envelope mismatch: {projection}"
            )
        projected_by_intent = {
            item.get("intent_id"): item for item in projection["cases"]
            if item.get("intent_id") in expected_cases
        }
        if set(projected_by_intent) != set(expected_cases):
            raise VerificationError(
                f"unknown V2 {role_name} projection case set mismatch: "
                f"{sorted(projected_by_intent)}"
            )
        for intent_id, projected in projected_by_intent.items():
            expected = expected_cases[intent_id]
            request = projected.get("request") or {}
            review = projected.get("review") or {}
            work_receipt = projected.get("work_receipt") or {}
            application = projected.get("application_receipt") or {}
            postcondition = projected.get("postcondition") or {}
            if (
                projected.get("schema_version") != 2
                or projected.get("break_state") != "resolved"
                or projected.get("reconciliation_state") != "complete"
                or request.get("state") != "applied"
                or review.get("decision") != "approved"
                or work_receipt.get("state") != "applied"
                or application.get("terminal_status") != expected["terminal"]
                or postcondition.get("resolution_complete") is not True
                or postcondition.get("accounting_application_recorded") is not True
            ):
                raise VerificationError(
                    f"unknown V2 {role_name}/{expected['name']} projection state "
                    f"mismatch: {projected}"
                )

            pending = [projected]
            exposed_keys: set[str] = set()
            while pending:
                value = pending.pop()
                if isinstance(value, dict):
                    exposed_keys.update(value)
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)
            if "account_id" in exposed_keys or any(
                "raw" in key.lower() and "payload" in key.lower()
                for key in exposed_keys
            ):
                raise VerificationError(
                    f"unknown V2 {role_name} projection exposed a forbidden field"
                )
    psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select idempotent from worker_api.release_worker_lease(
  'paper-primary','{worker}',{fencing_token},clock_timestamp(),'{RELEASE_SHA}'
);
""")
    # The next test preserves the legacy evidence-only workflow. Re-open the
    # disposable fixture with the qualification trigger disabled only for this
    # superuser-owned setup statement; production paths remain fail closed.
    psql(container, """
alter table private.execution_controls
  disable trigger guard_execution_control_qualification_freshness_v1;
update private.execution_controls
set execution_enabled=true,
    control_epoch=control_epoch+1,
    effective_at=greatest(clock_timestamp(),updated_at+interval '1 microsecond'),
    expires_at=clock_timestamp()+interval '1 day',
    updated_reason_code='verifier_next_isolated_case',
    updated_at=greatest(clock_timestamp(),updated_at+interval '1 microsecond')
where account_id='paper-primary';
alter table private.execution_controls
  enable trigger guard_execution_control_qualification_freshness_v1;
""")
    print(
        "PASS unknown V2 buy/sell/no-fill accounting closure, exact replay, "
        "stale token/revision/fence/epoch, self-review, generic ACK denial and "
        "role-scoped Desktop projection"
    )


def verify_unknown_resolution_evidence_only(container: str) -> None:
    intent_id = "87878787-8787-4787-8787-878787878787"
    decision_id = "88888887-8888-4887-8888-888888888887"
    risk_id = "89898987-8989-4987-8989-898989898987"
    worker = "8a8a8a8a-8a8a-4a8a-8a8a-8a8a8a8a8a8a"
    command_id = "8b8b8b8b-8b8b-4b8b-8b8b-8b8b8b8b8b8b"
    idempotency_key = "8c8c8c8c-8c8c-4c8c-8c8c-8c8c8c8c8c8c"
    self_review_id = "8d8d8d8d-8d8d-4d8d-8d8d-8d8d8d8d8d8d"
    stale_review_id = "8e8e8e8e-8e8e-4e8e-8e8e-8e8e8e8e8e8e"
    review_id = "8f8f8f8f-8f8f-4f8f-8f8f-8f8f8f8f8f8f"
    request_evidence = "e" * 64
    review_evidence = "f" * 64
    setup = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select fencing_token from worker_api.acquire_worker_lease(
  'paper-primary','{worker}',clock_timestamp(),120,'{RELEASE_SHA}'
);
reset role;
with times as (
  select
    date_trunc('minute',clock_timestamp())-interval '1 minute' as decision_at,
    date_trunc('minute',clock_timestamp())-interval '2 minutes' as signal_from,
    date_trunc('minute',clock_timestamp())+interval '10 minutes' as signal_until,
    date_trunc('minute',clock_timestamp()) as eligible_at,
    clock_timestamp()+interval '5 minutes' as expires_at,
    clock_timestamp()-interval '30 seconds' as risk_at,
    clock_timestamp()+interval '5 minutes' as risk_expires
)
select jsonb_build_object(
  'decision_at',decision_at,'signal_from',signal_from,
  'signal_until',signal_until,'eligible_at',eligible_at,
  'expires_at',expires_at,'risk_at',risk_at,'risk_expires',risk_expires,
  'semantic_key',private.compute_order_semantic_key(
    'paper-primary','paper','dedupe-strategy','035420','buy',
    signal_from,signal_until,'dedupe-policy'
  ),
  'fencing_token',(select fencing_token from private.worker_leases
    where account_id='paper-primary'),
  'control_epoch',(select control_epoch from private.execution_controls
    where account_id='paper-primary')
)
from times;
""").stdout.strip().splitlines()[-1]
    values = json.loads(setup)
    token = int(values["fencing_token"])
    prepared = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select concat_ws('|',reserved,intent_id,reason_code)
from worker_api.reserve_order_intent(
  '{intent_id}','{values['semantic_key']}','paper-primary','paper',
  'dedupe-strategy','{decision_id}','{'7' * 64}','{risk_id}',true,
  array[]::text[],'{values['risk_at']}','{values['risk_expires']}',
  '035420','buy',1,10000,'{values['decision_at']}','{values['signal_from']}',
  '{values['signal_until']}','dedupe-policy','dedupe-cost','{'1' * 64}',
  10010,'{values['eligible_at']}','{values['expires_at']}',
  {values['control_epoch']},
  '{worker}',{token},'{RELEASE_SHA}'
);
select concat_ws('|',attempt_id,reason_code)
from worker_api.mark_dispatch_started(
  '{intent_id}','paper-primary','paper','{worker}',{token},
  {values['control_epoch']},
  clock_timestamp(),'{'2' * 64}','paper:{intent_id}'
);
""").stdout.strip().splitlines()[-2:]
    if not prepared[0].startswith(f"t|{intent_id}|") \
            or not prepared[1].endswith("|prepared"):
        raise VerificationError(f"unknown fixture reserve/dispatch mismatch: {prepared}")

    observed_at = psql(container, "select clock_timestamp();").stdout.strip()
    reason_code = "provider_state_ambiguous"
    observation_hash = psql(container, f"""
select encode(extensions.digest(convert_to(concat_ws('|',
  '{intent_id}','1','unknown_requires_manual_check','paper:{intent_id}',
  '',private.utc_iso8601('{observed_at}'::timestamptz),
  '0','0','0','0','','','',
  '{reason_code}'
),'UTF8'),'sha256'),'hex');
""").stdout.strip()
    unknown = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select concat_ws('|',inserted,quarantined,reason_code)
from worker_api.record_execution_observation(
  '{intent_id}',1,'unknown_requires_manual_check','paper:{intent_id}',
  null,'{observation_hash}','{observed_at}',0,0,0,0,
  null,null,null,'[]'::jsonb,'{reason_code}','{worker}',{token}
);
reset role;
select event.event_summary->>'reconciliation_break_id'
from private.order_events as event
where event.intent_id='{intent_id}' and event.observation_id is not null
order by event.occurred_at desc limit 1;
""").stdout.strip().splitlines()[-2:]
    if unknown[0] != "t|f|recorded" or not unknown[1]:
        raise VerificationError(f"accepted unknown did not create linked break: {unknown}")
    break_id = unknown[1]

    request_draft = f"jsonb_build_object('schema_version',1," \
        f"'request_id','{command_id}','environment','paper'," \
        f"'idempotency_key','{idempotency_key}'," \
        "'command_type','resolve_unknown_execution'," \
        f"'break_id','{break_id}','evidence_sha256','{request_evidence}'," \
        "'reason_code','evidence_review_requested','expected_break_state','open'," \
        "'expected_break_revision',0,'requested_at',clock_timestamp()," \
        "'expires_at',clock_timestamp()+interval '1 hour')"
    requested = psql(container, jwt_claim_sql(OPERATOR) + f"""
with draft(value) as (select {request_draft}), grant_value(value) as (
  select api.issue_step_up_grant_v1(jsonb_build_object(
    'schema_version',1,'bound_action','request',
    'bound_command_type','resolve_unknown_execution','command_payload',draft.value
  )) from draft
), response(value) as (
  select api.request_unknown_resolution_v1(draft.value || grant_value.value)
  from draft,grant_value
)
select concat_ws('|',value->>'state',value->>'break_revision',
  value->>'accounting_mutation_allowed',value->>'resolution_complete')
from response;
""").stdout.strip().splitlines()[-1]
    if requested != "requested|1|false|false":
        raise VerificationError(f"unknown resolution request mismatch: {requested}")

    def review_draft(review_value: str, break_revision: int) -> str:
        return f"jsonb_build_object('schema_version',1,'review_id','{review_value}'," \
            f"'command_id','{command_id}'," \
            "'command_type','resolve_unknown_execution'," \
            "'reviewer_role','risk_approver','decision','approve'," \
            "'reason_code','evidence_sufficient','expected_receipt_revision',0," \
            f"'expected_break_revision',{break_revision}," \
            f"'evidence_sha256','{review_evidence}'," \
            "'reviewed_at',clock_timestamp())"

    self_draft = review_draft(self_review_id, 1)
    expect_failure(
        container,
        f"""
begin;
insert into private.role_assignments (user_id,role,reason)
values ('{OPERATOR}','risk_approver','self_review_negative_fixture');
""" + jwt_claim_sql(OPERATOR) + f"""
with draft(value) as (select {self_draft}), grant_value(value) as (
  select api.issue_step_up_grant_v1(jsonb_build_object(
    'schema_version',1,'bound_action','review',
    'bound_command_type','resolve_unknown_execution','command_payload',draft.value
  )) from draft
)
select api.review_unknown_resolution_v1(draft.value || grant_value.value)
from draft,grant_value;
""",
        "unknown_resolution_self_review_forbidden",
    )
    stale_draft = review_draft(stale_review_id, 0)
    expect_failure(
        container,
        jwt_claim_sql(RISK) + f"""
with draft(value) as (select {stale_draft}), grant_value(value) as (
  select api.issue_step_up_grant_v1(jsonb_build_object(
    'schema_version',1,'bound_action','review',
    'bound_command_type','resolve_unknown_execution','command_payload',draft.value
  )) from draft
)
select api.review_unknown_resolution_v1(draft.value || grant_value.value)
from draft,grant_value;
""",
        "unknown_resolution_break_not_reviewable_or_stale",
    )
    approved_draft = review_draft(review_id, 1)
    approved = psql(container, jwt_claim_sql(RISK) + f"""
with draft(value) as (select {approved_draft}), grant_value(value) as (
  select api.issue_step_up_grant_v1(jsonb_build_object(
    'schema_version',1,'bound_action','review',
    'bound_command_type','resolve_unknown_execution','command_payload',draft.value
  )) from draft
), response(value) as (
  select api.review_unknown_resolution_v1(draft.value || grant_value.value)
  from draft,grant_value
)
select concat_ws('|',value->>'state',value->>'break_state',
  value->>'accounting_mutation_allowed',value->>'resolution_complete',
  value->>'requires_balanced_accounting_adjustment')
from response;
""").stdout.strip().splitlines()[-1]
    if approved != "approved|resolution_requested|false|false|true":
        raise VerificationError(f"unknown evidence review mismatch: {approved}")

    expect_failure(
        container,
        f"""
insert into private.order_intents
select (jsonb_populate_record(
  null::private.order_intents,
  to_jsonb(existing_intent) || jsonb_build_object(
    'id','90909090-9090-4090-8090-909090909090',
    'semantic_key_sha256','{'0' * 64}',
    'correlation_id','90909090-9090-4090-8090-909090909090'
  )
)).*
from private.order_intents as existing_intent
where existing_intent.id='{intent_id}';
""",
        "unresolved_reconciliation_break_blocks_order_intent",
    )

    final_state = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
create temp table unknown_claim as
select * from worker_api.claim_operation_command_batch(
  'paper-primary','{worker}','{RELEASE_SHA}',{token},clock_timestamp(),25
);
reset role;
select concat_ws('|',
  (select state from private.operation_commands where id='{command_id}'),
  (select revision from private.operation_commands where id='{command_id}'),
  (select state from private.reconciliation_breaks where id='{break_id}'),
  (select revision from private.reconciliation_breaks where id='{break_id}'),
  (select resolution_command_id='{command_id}'::uuid
    from private.reconciliation_breaks where id='{break_id}'),
  (select count(*) from private.operation_command_reviews
    where command_id='{command_id}' and evidence_sha256='{review_evidence}'
      and request_digest_sha256=(select command_sha256
        from private.operation_commands where id='{command_id}')),
  (select requested_change->>'accounting_mutation_allowed'
    from private.operation_commands where id='{command_id}'),
  (select requested_change->>'resolution_complete'
    from private.operation_commands where id='{command_id}'),
  (select count(*) from unknown_claim where command_id='{command_id}'),
  (select count(*) from private.fills where intent_id='{intent_id}'),
  (select count(*) from private.accounting_transactions
    where correlation_id='{intent_id}'),
  (select count(*) from private.accounting_postings as posting
    join private.accounting_transactions as transaction
      on transaction.id=posting.journal_entry_id
    where transaction.correlation_id='{intent_id}'),
  (select state from private.execution_reconciliation_state
    where intent_id='{intent_id}'),
  (select remaining_cash_krw from private.reservation_events
    where intent_id='{intent_id}' order by event_sequence desc limit 1),
  (select execution_enabled from private.execution_controls
    where account_id='paper-primary'),
  (select control_epoch from private.execution_controls
    where account_id='paper-primary'),
  (select updated_reason_code from private.execution_controls
    where account_id='paper-primary')
);
""").stdout.strip().splitlines()[-1]
    expected = (
        "approved|1|resolution_requested|2|t|1|false|false|0|0|0|0|"
        f"manual|10010|f|{values['control_epoch'] + 1}|"
        "unresolved_reconciliation_break"
    )
    if final_state != expected:
        raise VerificationError(f"unknown evidence-only invariants mismatch: {final_state}")
    psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select idempotent from worker_api.release_worker_lease(
  'paper-primary','{worker}',{token},clock_timestamp(),'{RELEASE_SHA}'
);
""")
    print("PASS accepted unknown is two-person evidence-only and cannot mutate ledger")


def verify_snapshot(container: str) -> None:
    psql(container, """
insert into private.position_projection (
  account_id,symbol,quantity,average_cost_krw,projection_version
) values ('paper-primary','005930',10,8000,1);
""")
    snapshot_audit_id = psql(container, f"""
select private.write_audit_event(
  'system',null,'snapshot_verifier',null,null,null,
  'incident_resolved','incident','{SNAPSHOT_AUDIT_RESOURCE_ID}',
  '{SNAPSHOT_AUDIT_RESOURCE_ID}',null,
  'snapshot_auditor_positive_control',null,array[]::text[],null,null,null
);
""").stdout.strip()
    if not snapshot_audit_id:
        raise VerificationError("snapshot audit positive-control fixture is missing")
    expected_reconciliation = psql(container, f"""
select concat_ws('|', break_row.id, break_row.run_id)
from private.order_events as event
join private.reconciliation_breaks as break_row
  on event.event_summary->>'reconciliation_break_id' = break_row.id::text
where event.intent_id='{SNAPSHOT_EVIDENCE_INTENT_ID}'
  and event.event_type='manual_check_quarantined'
order by event.occurred_at desc, event.id desc
limit 1;
""").stdout.strip()
    if "|" not in expected_reconciliation:
        raise VerificationError("snapshot evidence fixture break is missing")
    expected_break_id, expected_run_id = expected_reconciliation.split("|", 1)
    non_auditor_snapshots: dict[str, dict[str, object]] = {}
    for role_name, user_id in NON_AUDITOR_HUMAN_ROLES:
        role_raw = psql(
            container,
            jwt_claim_sql(user_id)
            + "select api.get_desktop_operations_snapshot_v1();",
        ).stdout.strip().splitlines()[-1]
        role_snapshot = json.loads(role_raw)
        if role_snapshot.get("audit_events") != []:
            raise VerificationError(
                f"{role_name} snapshot leaked audit events: "
                f"{role_snapshot.get('audit_events')}"
            )
        if role_snapshot.get("reconciliation_cases") != []:
            raise VerificationError(
                f"{role_name} snapshot leaked reconciliation cases: "
                f"{role_snapshot.get('reconciliation_cases')}"
            )
        non_auditor_snapshots[role_name] = role_snapshot
    snapshot = non_auditor_snapshots["viewer"]
    position = snapshot["positions"][0]
    if any(position[key] is not None for key in (
        "market_price_krw", "market_value_krw", "unrealized_pnl_krw",
        "market_data_source", "market_data_as_of",
    )) or position["market_data_status"] != "unavailable":
        raise VerificationError(f"snapshot fabricated valuation: {position}")
    runtime = snapshot["runtime_health"]
    if runtime["realtime_connected"] is not False or runtime["realtime_last_seen_at"] is not None:
        raise VerificationError("snapshot fabricated client Realtime connectivity")
    if "access_changes" not in snapshot:
        raise VerificationError("snapshot omitted access changes")

    auditor_raw = psql(
        container,
        jwt_claim_sql(AUDITOR) + "select api.get_desktop_operations_snapshot_v1();",
    ).stdout.strip().splitlines()[-1]
    auditor_snapshot = json.loads(auditor_raw)
    auditor_permissions = auditor_snapshot.get("access", {}).get("permissions")
    if not isinstance(auditor_permissions, list) \
            or "view_audit" not in auditor_permissions:
        raise VerificationError("auditor snapshot omitted view_audit permission")
    if "view_reconciliation" not in auditor_permissions:
        raise VerificationError(
            "auditor snapshot omitted view_reconciliation permission"
        )
    audit_evidence = next(
        (
            item
            for item in auditor_snapshot.get("audit_events", [])
            if item.get("audit_id") == snapshot_audit_id
            and item.get("resource_id") == SNAPSHOT_AUDIT_RESOURCE_ID
            and item.get("resource_type") == "incident"
            and item.get("action") == "incident_resolved"
            and item.get("reason_code") == "snapshot_auditor_positive_control"
            and item.get("outcome") == "success"
            and item.get("correlation_id") == SNAPSHOT_AUDIT_RESOURCE_ID
        ),
        None,
    )
    if audit_evidence is None:
        raise VerificationError("auditor snapshot omitted known audit evidence")
    reconciliation_evidence = next(
        (
            item
            for item in auditor_snapshot.get("reconciliation_cases", [])
            if item.get("case_id") == expected_break_id
            and item.get("order_id") == SNAPSHOT_EVIDENCE_INTENT_ID
            and item.get("environment") == "paper"
            and item.get("status") == "investigating"
            and item.get("reason_code") == "ambiguous_order_state"
            and item.get("resolution_code") is None
            and item.get("evidence_refs")
            == [f"reconciliation-run:{expected_run_id}"]
        ),
        None,
    )
    if reconciliation_evidence is None:
        raise VerificationError(
            "auditor snapshot omitted known reconciliation evidence"
        )
    print(
        "PASS snapshot non-auditor evidence denial, auditor exact evidence "
        "projection, null valuation, causal safety and fail-closed Realtime"
    )


def verify_arithmetic() -> None:
    # Fees and taxes post to their own ledgers and do not alter position cost.
    buy_total = 80_000 + (2 * 9_009)
    quantity = 12
    relief = (buy_total * 4) // quantity
    realized = (4 * 9_990) - relief
    residual = buy_total - relief
    if (buy_total, relief, realized, residual) != (98_018, 32_672, 7_288, 65_346):
        raise VerificationError("accounting golden-vector arithmetic mismatch")
    print("PASS integer moving-average golden vector")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def jwt_token(role: str, sub: str) -> str:
    now = int(time.time())
    header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = b64url(json.dumps({
        "role": role, "sub": sub, "aal": "aal2", "iat": now, "exp": now + 600,
        "session_id": f"postgrest-{sub}",
        "amr": [{"method": "totp", "timestamp": now}],
    }, separators=(",", ":")).encode())
    signature = b64url(hmac.new(JWT_SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def http_post(url: str, token: str | None, *, profile: str = "api", body: dict | None = None) -> tuple[int, str]:
    headers = {"Content-Type": "application/json", "Content-Profile": profile, "Accept-Profile": profile}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=json.dumps(body or {}).encode(), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, response.read().decode()
    except HTTPError as error:
        return error.code, error.read().decode()


def require_postgrest_schema_denial(
    status: int,
    body: str,
    *,
    schema: str,
    label: str,
) -> None:
    expected = {
        "code": "42501",
        "details": None,
        "hint": None,
        "message": f"permission denied for schema {schema}",
    }
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise VerificationError(
            f"{label} returned a non-JSON denial body: status={status}, body={body}"
        ) from error
    if status != 401 or payload != expected:
        raise VerificationError(
            f"{label} denial contract mismatch: status={status}, "
            f"body={payload}, expected_status=401, expected_body={expected}"
        )


def verify_postgrest(pg: str, network: str, postgrest: str) -> None:
    run([
        "docker", "run", "-d", "--name", postgrest, "--network", network,
        "-p", "127.0.0.1::3000",
        "-e", f"PGRST_DB_URI=postgres://authenticator:{DB_PASSWORD}@{pg}:5432/postgres",
        "-e", "PGRST_DB_SCHEMAS=api,worker_api",
        "-e", "PGRST_DB_ANON_ROLE=anon",
        "-e", f"PGRST_JWT_SECRET={JWT_SECRET}",
        POSTGREST_IMAGE,
    ])
    port_text = run(["docker", "port", postgrest, "3000/tcp"]).stdout.strip()
    port = port_text.rsplit(":", 1)[-1]
    probe_host = os.environ.get("G1_G2_POSTGREST_HOST", "127.0.0.1")
    root = f"http://{probe_host}:{port}"
    for _ in range(60):
        try:
            with urlopen(root, timeout=1):
                break
        except HTTPError as error:
            if error.code < 500:
                break
            time.sleep(0.5)
        except Exception:
            time.sleep(0.5)
    else:
        logs = run(["docker", "logs", postgrest], check=False)
        raise VerificationError(
            "PostgREST did not become ready:\n" + logs.stdout + logs.stderr
        )
    non_auditor_tokens = tuple(
        (role_name, jwt_token("authenticated", user_id))
        for role_name, user_id in NON_AUDITOR_HUMAN_ROLES
    )
    auth = dict(non_auditor_tokens)["viewer"]
    auditor = jwt_token("authenticated", AUDITOR)
    operator = jwt_token("authenticated", OPERATOR)
    service_holder = "00000000-0000-4000-8000-000000000099"
    service = jwt_token("service_role", service_holder)
    service_fencing_token = psql(
        pg,
        jwt_claim_sql(service_holder, role="service_role") + f"""
select fencing_token from worker_api.acquire_worker_lease(
  'paper-primary','{service_holder}',clock_timestamp(),120,'{RELEASE_SHA}'
);
""",
    ).stdout.strip().splitlines()[-1]
    for endpoint in (
        "get_desktop_operations_snapshot_v1",
        "get_unknown_resolution_cases_v2",
    ):
        status, body = http_post(f"{root}/rpc/{endpoint}", None)
        require_postgrest_schema_denial(
            status,
            body,
            schema="api",
            label=f"anonymous {endpoint}",
        )
    for role_name, role_token in non_auditor_tokens:
        status, body = http_post(
            f"{root}/rpc/get_desktop_operations_snapshot_v1", role_token
        )
        if status != 200:
            raise VerificationError(
                f"{role_name} snapshot failed through PostgREST: {status}"
            )
        role_snapshot = json.loads(body)
        if role_snapshot.get("audit_events") != [] \
                or role_snapshot.get("reconciliation_cases") != []:
            raise VerificationError(
                f"{role_name} PostgREST snapshot leaked auditor evidence: "
                f"{role_snapshot}"
            )
    status, body = http_post(
        f"{root}/rpc/get_desktop_operations_snapshot_v1", auditor
    )
    if status != 200:
        raise VerificationError(
            f"auditor snapshot failed through PostgREST: {status}"
        )
    auditor_snapshot = json.loads(body)
    auditor_permissions = auditor_snapshot.get("access", {}).get("permissions")
    if not isinstance(auditor_permissions, list):
        raise VerificationError(
            "auditor PostgREST snapshot permissions have invalid type"
        )
    if not all(
        permission in auditor_permissions
        for permission in ("view_audit", "view_reconciliation")
    ):
        raise VerificationError(
            "auditor PostgREST snapshot omitted evidence permissions"
        )
    expected_audit_id = psql(pg, f"""
select id
from private.audit_events
where resource_id='{SNAPSHOT_AUDIT_RESOURCE_ID}'
  and reason_code='snapshot_auditor_positive_control'
order by occurred_at desc, id desc
limit 1;
""").stdout.strip()
    expected_reconciliation = psql(pg, f"""
select concat_ws('|', break_row.id, break_row.run_id)
from private.order_events as event
join private.reconciliation_breaks as break_row
  on event.event_summary->>'reconciliation_break_id' = break_row.id::text
where event.intent_id='{SNAPSHOT_EVIDENCE_INTENT_ID}'
  and event.event_type='manual_check_quarantined'
order by event.occurred_at desc, event.id desc
limit 1;
""").stdout.strip()
    if not expected_audit_id or "|" not in expected_reconciliation:
        raise VerificationError("PostgREST snapshot evidence fixture is missing")
    expected_break_id, expected_run_id = expected_reconciliation.split("|", 1)
    if not any(
        item.get("audit_id") == expected_audit_id
        and item.get("resource_id") == SNAPSHOT_AUDIT_RESOURCE_ID
        and item.get("resource_type") == "incident"
        and item.get("action") == "incident_resolved"
        and item.get("reason_code") == "snapshot_auditor_positive_control"
        and item.get("outcome") == "success"
        and item.get("correlation_id") == SNAPSHOT_AUDIT_RESOURCE_ID
        for item in auditor_snapshot.get("audit_events", [])
    ):
        raise VerificationError(
            "auditor PostgREST snapshot omitted known audit evidence"
        )
    if not any(
        item.get("case_id") == expected_break_id
        and item.get("order_id") == SNAPSHOT_EVIDENCE_INTENT_ID
        and item.get("environment") == "paper"
        and item.get("status") == "investigating"
        and item.get("reason_code") == "ambiguous_order_state"
        and item.get("resolution_code") is None
        and item.get("evidence_refs")
        == [f"reconciliation-run:{expected_run_id}"]
        for item in auditor_snapshot.get("reconciliation_cases", [])
    ):
        raise VerificationError(
            "auditor PostgREST snapshot omitted known reconciliation evidence"
        )
    status, body = http_post(
        f"{root}/rpc/get_unknown_resolution_cases_v2", operator
    )
    if status != 200:
        raise VerificationError(
            f"operator unknown V2 projection failed through PostgREST: {status}"
        )
    projection = json.loads(body)
    projected_intents = {
        item.get("intent_id") for item in projection.get("cases", [])
    }
    expected_intents = {
        "a2a2a2a2-a2a2-42a2-82a2-a2a2a2a2a2a2",
        "b2a2a2a2-a2a2-42a2-82a2-a2a2a2a2a2a2",
        "c2a2a2a2-a2a2-42a2-82a2-a2a2a2a2a2a2",
    }
    if projection.get("schema_version") != 2 \
            or not expected_intents.issubset(projected_intents):
        raise VerificationError(
            "operator unknown V2 PostgREST projection contract mismatch"
        )
    status, body = http_post(f"{root}/rpc/get_unknown_resolution_cases_v2", auth)
    viewer_projection = json.loads(body) if status == 200 else None
    if status != 200 or viewer_projection.get("cases") != []:
        raise VerificationError(
            f"viewer unknown V2 projection was not empty through PostgREST: {status}"
        )
    worker_body = {
        "p_account_id": "paper-primary",
        "p_holder_id": service_holder,
        "p_release_sha": RELEASE_SHA,
        "p_fencing_token": int(service_fencing_token),
        "p_now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "p_limit": 1,
    }
    status, body = http_post(
        f"{root}/rpc/claim_operation_command_batch",
        None,
        profile="worker_api",
        body=worker_body,
    )
    require_postgrest_schema_denial(
        status,
        body,
        schema="worker_api",
        label="anonymous claim_operation_command_batch",
    )
    status, _ = http_post(
        f"{root}/rpc/claim_operation_command_batch", auth, profile="worker_api",
        body=worker_body,
    )
    if status not in (401, 403, 404):
        raise VerificationError(f"authenticated worker RPC unexpectedly allowed: {status}")
    status, _ = http_post(
        f"{root}/rpc/claim_operation_command_batch", service, profile="worker_api",
        body=worker_body,
    )
    if status != 200:
        raise VerificationError(f"service worker RPC failed through PostgREST: {status}")
    status, _ = http_post(f"{root}/rpc/get_desktop_operations_snapshot_v1", service)
    if status not in (401, 403, 404):
        raise VerificationError(f"service desktop snapshot unexpectedly allowed: {status}")
    status, _ = http_post(f"{root}/rpc/get_unknown_resolution_cases_v2", service)
    if status not in (401, 403, 404):
        raise VerificationError(
            f"service unknown V2 projection unexpectedly allowed: {status}"
        )
    psql(pg, jwt_claim_sql(service_holder, role="service_role") + f"""
select idempotent from worker_api.release_worker_lease(
  'paper-primary','{service_holder}',{service_fencing_token},
  clock_timestamp(),'{RELEASE_SHA}'
);
""")
    print(
        "PASS actual PostgREST anon/authenticated/service boundary and unknown "
        "V2 role projection"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--skip-postgrest",
        action="store_true",
        help="local parser debugging only; returns partial status 2",
    )
    args = parser.parse_args()
    suffix = uuid4().hex[:10]
    pg = f"msp-g1g2-pg-{suffix}"
    preinstalled_pg = f"msp-g1g2-preinstalled-{suffix}"
    upgrade_public_pg = f"msp-g1g2-upgrade-public-{suffix}"
    upgrade_extensions_pg = f"msp-g1g2-upgrade-extensions-{suffix}"
    operational_upgrade_pg = f"msp-g1g2-operational-upgrade-{suffix}"
    postgrest = f"msp-g1g2-rest-{suffix}"
    network = f"msp-g1g2-net-{suffix}"
    try:
        verify_repository_inputs()
        run(["docker", "info"])
        run(["docker", "network", "create", network])
        run([
            "docker", "run", "-d", "--name", pg, "--network", network,
            "-e", f"POSTGRES_PASSWORD={DB_PASSWORD}", POSTGRES_IMAGE,
        ])
        wait_for_postgres(pg)
        apply_repository(pg)
        verify_pgcrypto_convergence_idempotency(pg)
        psql(pg, fixture_sql())
        verify_catalog(pg)
        verify_strict_auth(pg)
        verify_access_maker_checker(pg)
        verify_account_opening(pg)
        verify_qualification_suite_upgrade_retry(pg)
        verify_command_claim_allowlist(pg)
        verify_command_ack_expiry(pg)
        verify_command_claim_generation_fencing(pg)
        verify_qualification_expiry_at_application(pg)
        verify_reconciliation_keyset(pg)
        canonical = verify_semantic_dedupe_concurrency(pg)
        verify_single_active_sell_reservation(pg, canonical)
        verify_aggregate_paper_bar_participation(pg, canonical)
        verify_execution_transition_guards(pg)
        verify_pre_dispatch_recovery(pg, canonical)
        verify_partial_resume_accounting_and_expiry(pg)
        verify_cash_settlement_maturity(pg)
        verify_unknown_resolution_v2(pg)
        verify_unknown_resolution_evidence_only(pg)
        verify_lease_and_outbox(pg)
        verify_snapshot(pg)
        verify_manual_reconciliation_atomicity(pg)
        verify_arithmetic()
        run([
            "docker", "run", "-d", "--name", preinstalled_pg, "--network", network,
            "-e", f"POSTGRES_PASSWORD={DB_PASSWORD}", POSTGRES_IMAGE,
        ])
        wait_for_postgres(preinstalled_pg)
        verify_controlled_non_superuser_preinstalled_replay(preinstalled_pg)
        run([
            "docker", "run", "-d", "--name", upgrade_public_pg, "--network", network,
            "-e", f"POSTGRES_PASSWORD={DB_PASSWORD}", POSTGRES_IMAGE,
        ])
        wait_for_postgres(upgrade_public_pg)
        verify_populated_0015_upgrade(
            upgrade_public_pg,
            legacy_pgcrypto_schema="public",
        )
        run([
            "docker", "run", "-d", "--name", upgrade_extensions_pg,
            "--network", network,
            "-e", f"POSTGRES_PASSWORD={DB_PASSWORD}", POSTGRES_IMAGE,
        ])
        wait_for_postgres(upgrade_extensions_pg)
        verify_populated_0015_upgrade(
            upgrade_extensions_pg,
            legacy_pgcrypto_schema="extensions",
        )
        run([
            "docker", "run", "-d", "--name", operational_upgrade_pg,
            "--network", network,
            "-e", f"POSTGRES_PASSWORD={DB_PASSWORD}", POSTGRES_IMAGE,
        ])
        wait_for_postgres(operational_upgrade_pg)
        verify_populated_0023_operational_upgrade(operational_upgrade_pg)
        if not args.skip_postgrest:
            verify_postgrest(pg, network, postgrest)
        else:
            print("WARN PostgREST integration skipped by explicit flag")
            print("FINAL=PARTIAL postgrest_not_verified")
            return 2
        print("FINAL=PASS g1_g2_migration_verifier")
        return 0
    except (VerificationError, OSError, json.JSONDecodeError) as error:
        print(f"FINAL=FAIL {error}", file=sys.stderr)
        return 1
    finally:
        run(["docker", "rm", "-f", postgrest], check=False)
        run(["docker", "rm", "-f", operational_upgrade_pg], check=False)
        run(["docker", "rm", "-f", upgrade_extensions_pg], check=False)
        run(["docker", "rm", "-f", upgrade_public_pg], check=False)
        run(["docker", "rm", "-f", preinstalled_pg], check=False)
        run(["docker", "rm", "-f", pg], check=False)
        run(["docker", "network", "rm", network], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
