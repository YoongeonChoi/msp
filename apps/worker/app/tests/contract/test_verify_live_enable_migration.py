from pathlib import Path
from types import SimpleNamespace

import pytest

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
        "anon_begin_worker_deployment_denied",
        "authenticated_complete_worker_deployment_denied",
    ]

    for fragment in required_fragments:
        assert fragment in source


def test_verifier_checks_service_role_rpc_success_path() -> None:
    source = _source()

    assert "create role service_role nologin bypassrls;" in source
    assert "set role service_role;" in source
    assert "set_config('request.jwt.claim.role', 'service_role', false)" in source
    assert "select public.database_size_bytes();" in source
    assert "select public.run_retention_cleanup(true);" in source
    assert "select public.begin_worker_deployment" in source
    assert "select public.complete_worker_deployment" in source
    assert "expected_sticky_live_reenable_to_require_new_approval" in source
    assert "deployment_lock_did_not_fail_closed" in source
    assert "deployment_lock_did_not_release_safely" in source
    assert "FINAL=PASS live_enable_consumed_once rpc_hardening" in source


def test_docker_probe_handles_missing_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("verify_live_enable_migration", VERIFIER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def missing_docker(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(module, "_run", missing_docker)

    assert module._docker_ready() is False


def test_postgres_wait_requires_a_real_psql_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("verify_live_enable_migration", VERIFIER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return_codes = iter((0, 1, 0, 0))
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(returncode=next(return_codes))

    monkeypatch.setattr(module, "_run", fake_run)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    module._wait_for_postgres("test-container", 1)

    assert [command[3] for command in commands] == [
        "pg_isready",
        "psql",
        "pg_isready",
        "psql",
    ]
