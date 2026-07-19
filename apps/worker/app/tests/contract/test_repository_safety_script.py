from __future__ import annotations

import importlib.util
import re
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


def _workflow_job_block(text: str, job_name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(job_name)}:\n.*?(?=^  [a-z0-9-]+:\n|\Z)",
        text,
    )
    assert match is not None
    return match.group(0)


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


def test_destructive_approval_cannot_cross_migration_files(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    migration_dir = root / "supabase" / "migrations"
    (migration_dir / "0002.sql").write_text(
        "-- Rollback note: this note applies only to migration 0002.\n",
        encoding="utf-8",
    )
    (migration_dir / "0003.sql").write_text(
        "drop table private.execution_history;\n",
        encoding="utf-8",
    )

    findings = module.check_migration_safety(root)

    assert (
        "supabase/migrations/0003.sql: destructive migration missing "
        "rollback note or approval"
    ) in findings


def test_destructive_sql_accepts_same_file_rollback_note(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / "supabase" / "migrations" / "0002.sql").write_text(
        "-- Rollback note: restore private.execution_history from backup.\n"
        "drop table private.execution_history;\n",
        encoding="utf-8",
    )

    findings = module.check_migration_safety(root)

    assert not any("destructive migration" in finding for finding in findings)


def test_destructive_words_in_sql_comments_do_not_trigger_guard(
    tmp_path: Path,
) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / "supabase" / "migrations" / "0002.sql").write_text(
        "-- Example only: drop table private.execution_history.\n"
        "select 1;\n",
        encoding="utf-8",
    )

    findings = module.check_migration_safety(root)

    assert not any("destructive migration" in finding for finding in findings)


def test_multiline_drop_column_requires_same_file_approval(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / "supabase" / "migrations" / "0002.sql").write_text(
        "alter table private.execution_history\n"
        "  drop\n"
        "  column provider_payload;\n",
        encoding="utf-8",
    )

    findings = module.check_migration_safety(root)

    assert (
        "supabase/migrations/0002.sql: destructive migration missing "
        "rollback note or approval"
    ) in findings


def test_approval_phrase_inside_sql_string_is_not_evidence(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / "supabase" / "migrations" / "0002.sql").write_text(
        "select '-- Rollback note: fake approval';\n"
        "truncate table private.execution_history;\n",
        encoding="utf-8",
    )

    findings = module.check_migration_safety(root)

    assert (
        "supabase/migrations/0002.sql: destructive migration missing "
        "rollback note or approval"
    ) in findings


@pytest.mark.parametrize(
    "comment",
    (
        "-- No rollback note is available.\n",
        "-- Rollback note:\n",
        "/* Destructive migration approved: */\n",
    ),
)
def test_destructive_approval_requires_explicit_comment_detail(
    tmp_path: Path,
    comment: str,
) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / "supabase" / "migrations" / "0002.sql").write_text(
        comment + "drop table private.execution_history;\n",
        encoding="utf-8",
    )

    findings = module.check_migration_safety(root)

    assert (
        "supabase/migrations/0002.sql: destructive migration missing "
        "rollback note or approval"
    ) in findings


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


def test_contract_qualification_v2_worker_rpc_is_allowlisted(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / "supabase" / "migrations" / "0002.sql").write_text(
        "create function worker_api.register_qualification_run_v2(jsonb) "
        "returns jsonb language sql security invoker set search_path = '' "
        "as $$ select '{}'::jsonb; $$;\n",
        encoding="utf-8",
    )

    findings = module.check_migration_safety(root)

    assert not any(
        "worker_api function outside allowlist: register_qualification_run_v2"
        in finding
        for finding in findings
    )


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


def test_gitleaks_ignore_accepts_only_exact_fingerprints(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / ".gitleaksignore").write_text(
        sorted(module.APPROVED_GITLEAKS_FINGERPRINTS)[0] + "\n",
        encoding="utf-8",
    )

    findings = module.check_workflow_safety(root)

    assert not any(".gitleaksignore" in finding for finding in findings)


@pytest.mark.parametrize(
    "unsafe_entry",
    (
        "tests/.*",
        "a" * 40,
        "a" * 40 + ":tests/example.py:generic-api-key:*",
        "# broad fixture exemption",
    ),
)
def test_gitleaks_ignore_rejects_broad_or_malformed_entries(
    tmp_path: Path,
    unsafe_entry: str,
) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / ".gitleaksignore").write_text(unsafe_entry + "\n", encoding="utf-8")

    findings = module.check_workflow_safety(root)

    assert any("fingerprint must be exact" in finding for finding in findings)


def test_gitleaks_ignore_rejects_unreviewed_exact_fingerprint(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / ".gitleaksignore").write_text(
        "b" * 40 + ":tests/example.py:generic-api-key:12\n",
        encoding="utf-8",
    )

    findings = module.check_workflow_safety(root)

    assert any("fingerprint is not approved" in finding for finding in findings)


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


def test_security_workflow_runs_on_develop_and_main_pushes() -> None:
    text = (ROOT / ".github" / "workflows" / "security.yml").read_text(
        encoding="utf-8"
    )

    lines = text.splitlines()
    push_index = lines.index("  push:")
    branches_line = lines[push_index + 1].strip()
    assert branches_line.startswith("branches: [")
    assert branches_line.endswith("]")
    branches = {
        branch.strip().strip("'\"")
        for branch in branches_line.removeprefix("branches: [")[:-1].split(",")
    }
    assert {"main", "develop"}.issubset(branches)

    for job_name in (
        "codeql",
        "security-audits",
        "secret-scan",
        "secret-pattern-scan",
        "workflow-policy",
    ):
        assert "\n    if:" not in _workflow_job_block(text, job_name)

    dependency_review = _workflow_job_block(text, "dependency-review")
    assert "\n    if: github.event_name == 'pull_request'" in dependency_review
