from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260723162000_desktop_operations_sensitive_projection_gate.sql"
)
CHECKSUMS = ROOT / "supabase" / "migration-checksums.v1.json"
VERIFIER = ROOT / "supabase" / "verify_g1_g2_migration.py"
PREFLIGHT = ROOT / "supabase" / "preflight" / "pgcrypto_replay_preflight.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _function_body(sql: str) -> str:
    marker = "create or replace function private.get_desktop_operations_snapshot_v1_impl()"
    start = sql.index(marker)
    body_start = sql.index("as $$", start) + len("as $$")
    return sql[body_start : sql.index("$$;", body_start)]


def _permission_guard(body: str, permission: str) -> str:
    matches: list[str] = re.findall(
        rf"if\s+'{permission}'\s*=\s*any\(permissions\)\s+then(.*?)end if;",
        body,
        flags=re.DOTALL,
    )
    assert len(matches) == 1
    return matches[0]


def test_forward_only_migration_redefines_only_the_snapshot_implementation() -> None:
    sql = _sql()
    lowered = sql.lower()

    assert sql.startswith("begin;\n")
    assert sql.rstrip().endswith("commit;")
    assert sql.count("create or replace function") == 1
    assert ("create or replace function private.get_desktop_operations_snapshot_v1_impl()") in sql
    assert "create or replace function api.get_desktop_operations_snapshot_v1" not in sql
    for forbidden in ("drop function", "drop table", "truncate ", "delete from", "alter table"):
        assert forbidden not in lowered


def test_sensitive_queries_are_independently_permission_gated() -> None:
    body = _function_body(_sql())
    audit_guard = _permission_guard(body, "view_audit")
    reconciliation_guard = _permission_guard(body, "view_reconciliation")

    assert "audit_value jsonb := '[]'::jsonb;" in body
    assert "reconciliation_value jsonb := '[]'::jsonb;" in body
    assert body.count("from private.audit_events") == 1
    assert "from private.audit_events" in audit_guard
    assert body.count("from private.reconciliation_breaks as break_row") == 1
    assert "from private.reconciliation_breaks as break_row" in reconciliation_guard
    assert "from private.audit_events" not in reconciliation_guard
    assert "from private.reconciliation_breaks as break_row" not in audit_guard
    assert body.count("'audit_events', audit_value") == 1
    assert body.count("'reconciliation_cases', reconciliation_value") == 1


def test_snapshot_security_properties_and_exact_acl_are_reasserted() -> None:
    sql = _sql()
    normalized = re.sub(r"\s+", " ", sql)
    header = sql[: sql.index("as $$")]

    assert "language plpgsql" in header
    assert "volatile" in header
    assert "security definer" in header
    assert "set search_path = ''" in header
    assert (
        "revoke execute on function "
        "private.get_desktop_operations_snapshot_v1_impl() "
        "from public, anon, authenticated, authenticator, service_role;"
    ) in normalized
    assert (
        "grant execute on function "
        "private.get_desktop_operations_snapshot_v1_impl() to authenticated;"
    ) in normalized
    assert (
        "revoke execute on function api.get_desktop_operations_snapshot_v1() "
        "from public, anon, authenticated, authenticator, service_role;"
    ) in normalized
    assert (
        "grant execute on function api.get_desktop_operations_snapshot_v1() to authenticated;"
    ) in normalized
    assert "procedure.prosrc" in sql
    assert "select private.get_desktop_operations_snapshot_v1_impl();" in sql
    assert "forbidden_effective_execute_count" in sql
    assert "forbidden_owner_count" in sql
    assert "owner_role.rolname" in sql
    for forbidden_owner in (
        "'anon'",
        "'authenticated'",
        "'authenticator'",
        "'service_role'",
    ):
        assert forbidden_owner in sql
    assert "desktop_operations_sensitive_projection_security_contract_failed" in sql


def test_disposable_verifier_has_role_matrix_and_exact_positive_controls() -> None:
    verifier = VERIFIER.read_text(encoding="utf-8")

    for marker in (
        "NON_AUDITOR_HUMAN_ROLES",
        "for role_name, user_id in NON_AUDITOR_HUMAN_ROLES",
        "snapshot leaked audit events",
        "snapshot leaked reconciliation cases",
        "auditor snapshot omitted view_audit permission",
        "auditor snapshot omitted view_reconciliation permission",
        "auditor snapshot omitted known audit evidence",
        "auditor snapshot omitted known reconciliation evidence",
        "PostgREST snapshot leaked auditor evidence",
        "auditor PostgREST snapshot permissions have invalid type",
        "snapshot_auditor_positive_control",
        '[f"reconciliation-run:{expected_run_id}"]',
    ):
        assert marker in verifier
    for role_name in (
        "platform_admin",
        "operator",
        "risk_approver",
        "strategy_reviewer",
        "release_manager",
        "viewer",
    ):
        assert f'("{role_name}",' in verifier
    assert "jwt_claim_sql(AUDITOR)" in verifier
    assert 'jwt_token("authenticated", AUDITOR)' in verifier


def test_pgcrypto_preflight_accepts_the_exact_new_repository_tail() -> None:
    preflight = PREFLIGHT.read_text(encoding="utf-8")

    assert "'20260723162000'" in preflight
    assert "'desktop_operations_sensitive_projection_gate'" in preflight
    assert preflight.index("'20260719090000'") < preflight.index("'20260723162000'")
    assert preflight.index("'pit_daily_candle_collection_job_store'") < preflight.index(
        "'desktop_operations_sensitive_projection_gate'"
    )


def test_checksum_manifest_pins_new_migration_without_rewriting_history() -> None:
    manifest = json.loads(CHECKSUMS.read_text(encoding="utf-8"))
    migrations = manifest["migrations"]
    expected = hashlib.sha256(MIGRATION.read_text(encoding="utf-8").encode("utf-8")).hexdigest()

    assert manifest["algorithm"] == "sha256"
    assert manifest["canonicalization"] == "utf-8-lf"
    assert migrations[MIGRATION.name] == expected
    assert migrations["0020_canonical_operations_contract.sql"] == (
        "08815f3cfafa0c7eba24714c5983e703616723d92a0fa68ee6a9c17964a28d75"
    )
    assert migrations["0022_operational_workflows.sql"] == (
        "a33a2881161024b67dbff0f6aa457302f419136d44102be359dab2a088065ae1"
    )
    assert list(migrations) == sorted(migrations)
