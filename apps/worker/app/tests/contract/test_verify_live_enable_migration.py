from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
VERIFIER = ROOT / "supabase" / "verify_live_enable_migration.py"


def _source() -> str:
    return VERIFIER.read_text(encoding="utf-8")


def test_verifier_checks_security_definer_rpc_denials() -> None:
    source = _source()

    required_fragments = [
        "_verify_security_definer_rpc_grants(container)",
        "anon_run_retention_cleanup_denied",
        "authenticated_run_retention_cleanup_denied",
        "anon_database_size_denied",
        "authenticated_database_size_denied",
        "permission denied",
    ]

    for fragment in required_fragments:
        assert fragment in source


def test_verifier_checks_service_role_rpc_success_path() -> None:
    source = _source()

    assert "set role service_role;" in source
    assert "select public.database_size_bytes();" in source
    assert "select public.run_retention_cleanup(true);" in source
    assert "FINAL=PASS live_enable_consumed_once rpc_hardening" in source
