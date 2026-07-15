from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[5]
SCRIPT = ROOT / ".github" / "scripts" / "repository_safety.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("repository_safety", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _safe_repository(tmp_path: Path) -> Path:
    migration_dir = tmp_path / "supabase" / "migrations"
    migration_dir.mkdir(parents=True)
    (migration_dir / "0001.sql").write_text(
        "create table if not exists public.events (id bigint);\n"
        "alter table public.events enable row level security;\n",
        encoding="utf-8",
    )
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "ci.yml").write_text("on: [push]\n", encoding="utf-8")
    (tmp_path / "render.yaml").write_text(
        'services:\n  - autoDeployTrigger: "off"\n',
        encoding="utf-8",
    )
    return tmp_path


def test_current_repository_passes_central_safety_policy() -> None:
    module = _module()

    assert module.check_migration_safety(ROOT) == []
    assert module.check_workflow_safety(ROOT) == []


def test_if_not_exists_public_table_without_rls_is_rejected(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / "supabase" / "migrations" / "0002.sql").write_text(
        "create table if not exists public.unsafe_table (id bigint);\n",
        encoding="utf-8",
    )

    findings = module.check_migration_safety(root)

    assert "exposed table missing RLS: public.unsafe_table" in findings


def test_quoted_unlogged_public_table_without_rls_is_rejected(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / "supabase" / "migrations" / "0002.sql").write_text(
        'create unlogged table "public"."unsafe_table" (id bigint);\n',
        encoding="utf-8",
    )

    findings = module.check_migration_safety(root)

    assert "exposed table missing RLS: public.unsafe_table" in findings


def test_migration_cannot_disable_public_table_rls(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / "supabase" / "migrations" / "0002.sql").write_text(
        'alter table "public"."events" disable row level security;\n',
        encoding="utf-8",
    )

    findings = module.check_migration_safety(root)

    assert "exposed table disables RLS: public.events" in findings


def test_api_table_without_rls_is_rejected(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / "supabase" / "migrations" / "0002.sql").write_text(
        "create table api.runtime_status (id bigint);\n",
        encoding="utf-8",
    )

    findings = module.check_migration_safety(root)

    assert "exposed table missing RLS: api.runtime_status" in findings


def test_api_view_requires_security_invoker(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / "supabase" / "migrations" / "0002.sql").write_text(
        "create view api.runtime_status as select 1 as id;\n",
        encoding="utf-8",
    )

    findings = module.check_migration_safety(root)

    assert "api view missing security_invoker=true: runtime_status" in findings


def test_api_security_invoker_view_is_allowed(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / "supabase" / "migrations" / "0002.sql").write_text(
        "create view api.runtime_status with (security_invoker = true) "
        "as select 1 as id;\n",
        encoding="utf-8",
    )

    findings = module.check_migration_safety(root)

    assert not any("api view missing security_invoker" in item for item in findings)


def test_exposed_security_definer_function_is_rejected(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / "supabase" / "migrations" / "0002.sql").write_text(
        "create function api.unsafe_rpc() returns void language plpgsql "
        "security definer set search_path = '' "
        "as $$ begin null; end; $$;\n",
        encoding="utf-8",
    )

    findings = module.check_migration_safety(root)

    assert "exposed function uses SECURITY DEFINER: api.unsafe_rpc" in findings
    assert (
        "exposed function missing explicit SECURITY INVOKER: api.unsafe_rpc"
        in findings
    )


def test_exposed_function_requires_explicit_security_invoker(
    tmp_path: Path,
) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / "supabase" / "migrations" / "0002.sql").write_text(
        "create function worker_api.acquire_worker_lease() returns void "
        "language sql as $$ select null; $$;\n",
        encoding="utf-8",
    )

    findings = module.check_migration_safety(root)

    assert (
        "exposed function missing explicit SECURITY INVOKER: "
        "worker_api.acquire_worker_lease"
    ) in findings


def test_explicit_security_invoker_wrapper_is_allowed(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / "supabase" / "migrations" / "0002.sql").write_text(
        "create function worker_api.acquire_worker_lease() returns void "
        "language sql security invoker set search_path = '' "
        "as $$ select null; $$;\n",
        encoding="utf-8",
    )

    findings = module.check_migration_safety(root)

    assert not any("exposed function" in finding for finding in findings)


def test_private_definer_requires_empty_search_path(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / "supabase" / "migrations" / "0002.sql").write_text(
        "create function private.unsafe_impl() returns void language plpgsql "
        "security definer as $$ begin null; end; $$;\n",
        encoding="utf-8",
    )

    findings = module.check_migration_safety(root)

    assert (
        "private SECURITY DEFINER missing empty search_path: private.unsafe_impl"
        in findings
    )


def test_worker_api_function_must_be_allowlisted(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / "supabase" / "migrations" / "0002.sql").write_text(
        "create function worker_api.place_live_order() returns void "
        "language sql as 'select';\n",
        encoding="utf-8",
    )

    findings = module.check_migration_safety(root)

    assert "worker_api function outside allowlist: place_live_order" in findings


def test_worker_api_execute_cannot_be_granted_to_authenticated(
    tmp_path: Path,
) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / "supabase" / "migrations" / "0002.sql").write_text(
        "grant execute on function worker_api.acquire_worker_lease(uuid) "
        "to authenticated;\n",
        encoding="utf-8",
    )

    findings = module.check_migration_safety(root)

    assert "worker_api EXECUTE granted to desktop/public role" in findings


def test_multiline_anon_write_policy_is_rejected(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / "supabase" / "migrations" / "0002.sql").write_text(
        "create policy unsafe_write\n"
        "on public.events\n"
        "for update\n"
        "to anon\n"
        "using (true);\n",
        encoding="utf-8",
    )

    findings = module.check_migration_safety(root)

    assert any("anon/public write-capable policy" in finding for finding in findings)


def test_anon_in_multi_role_policy_is_rejected(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / "supabase" / "migrations" / "0002.sql").write_text(
        "create policy unsafe_write on public.events "
        "for insert to authenticated, anon with check (true);\n",
        encoding="utf-8",
    )

    findings = module.check_migration_safety(root)

    assert any("anon/public write-capable policy" in finding for finding in findings)


def test_write_policy_without_target_defaults_to_public_and_is_rejected(
    tmp_path: Path,
) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / "supabase" / "migrations" / "0002.sql").write_text(
        "create policy unsafe_write on public.events for update using (true);\n",
        encoding="utf-8",
    )

    findings = module.check_migration_safety(root)

    assert any("anon/public write-capable policy" in finding for finding in findings)


@pytest.mark.parametrize("policy_name", ['"to authenticated"', '"for select"'])
def test_policy_name_cannot_spoof_policy_command_or_roles(
    tmp_path: Path,
    policy_name: str,
) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / "supabase" / "migrations" / "0002.sql").write_text(
        f"create policy {policy_name} on public.events for update using (true);\n",
        encoding="utf-8",
    )

    findings = module.check_migration_safety(root)

    assert any("anon/public write-capable policy" in finding for finding in findings)


def test_explicit_anon_select_policy_is_read_only_and_allowed(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / "supabase" / "migrations" / "0002.sql").write_text(
        "create policy read_only on public.events for select to anon using (true);\n",
        encoding="utf-8",
    )

    findings = module.check_migration_safety(root)

    assert not any("anon/public write-capable policy" in finding for finding in findings)


def test_yaml_workflow_is_included_in_policy_scan(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / ".github" / "workflows" / "unsafe.yaml").write_text(
        "on:\n  pull_request_target:\n",
        encoding="utf-8",
    )

    findings = module.check_workflow_safety(root)

    assert any("unsafe.yaml: pull_request_target" in finding for finding in findings)


def test_every_protected_secret_is_rejected_in_workflows(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    workflow = root / ".github" / "workflows" / "unsafe.yaml"

    for secret_name in module.PROTECTED_SECRET_NAMES:
        workflow.write_text(
            f"env:\n  VALUE: ${{{{ secrets.{secret_name} }}}}\n",
            encoding="utf-8",
        )
        findings = module.check_workflow_safety(root)
        assert any("production secret reference" in finding for finding in findings)


def test_protected_secret_bracket_and_case_variants_are_rejected(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    workflow = root / ".github" / "workflows" / "unsafe.yaml"
    workflow.write_text(
        "env:\n"
        "  FIRST: ${{ secrets['supabase_secret_key'] }}\n"
        "  SECOND: ${{ SECRETS.TOSS_CLIENT_SECRET }}\n",
        encoding="utf-8",
    )

    findings = module.check_workflow_safety(root)

    assert any("production secret reference" in finding for finding in findings)


def test_every_protected_secret_env_key_is_rejected_in_workflows(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    workflow = root / ".github" / "workflows" / "unsafe.yml"

    for secret_name in module.PROTECTED_SECRET_NAMES:
        workflow.write_text(
            f"env:\n  {secret_name}: placeholder\n",
            encoding="utf-8",
        )
        findings = module.check_workflow_safety(root)
        assert any("production secret env key" in finding for finding in findings)


def test_automatic_render_deploy_is_rejected(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / ".github" / "workflows" / "unsafe.yaml").write_text(
        "jobs:\n  deploy:\n    steps:\n      - run: render deploy\n",
        encoding="utf-8",
    )

    findings = module.check_workflow_safety(root)

    assert any("automatic Render deployment" in finding for finding in findings)


def test_workflow_action_must_be_pinned_to_full_commit_sha(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    workflow = root / ".github" / "workflows" / "unsafe.yaml"
    workflow.write_text(
        "jobs:\n  test:\n    steps:\n      - uses: actions/checkout@v4\n",
        encoding="utf-8",
    )

    findings = module.check_workflow_safety(root)

    assert any("action not pinned to full commit SHA" in finding for finding in findings)


def test_workflow_action_accepts_full_commit_sha(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    workflow = root / ".github" / "workflows" / "safe-action.yaml"
    workflow.write_text(
        "jobs:\n  test:\n    steps:\n      - uses: actions/checkout@"
        + "a" * 40
        + " # v4\n",
        encoding="utf-8",
    )

    findings = module.check_workflow_safety(root)

    assert not any("action not pinned" in finding for finding in findings)


def test_all_policy_workflows_use_the_central_repository_safety_command() -> None:
    expected_commands = {
        ROOT / ".github" / "workflows" / "ci.yml": ("migrations", "workflows"),
        ROOT / ".github" / "workflows" / "migration-check.yml": ("migrations",),
        ROOT / ".github" / "workflows" / "security.yml": ("workflows",),
    }

    for workflow, scopes in expected_commands.items():
        text = workflow.read_text(encoding="utf-8")
        for scope in scopes:
            assert f"python .github/scripts/repository_safety.py {scope}" in text


def test_security_audits_are_blocking_gates() -> None:
    text = (ROOT / ".github" / "workflows" / "security.yml").read_text(
        encoding="utf-8"
    )

    for name in ("npm audit gate", "pip-audit gate", "bandit gate"):
        start = text.index(f"- name: {name}")
        next_step = text.find("\n      - name:", start + 1)
        block = text[start:] if next_step == -1 else text[start:next_step]
        assert "continue-on-error" not in block
