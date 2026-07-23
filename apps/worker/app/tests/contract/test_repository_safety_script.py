from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[5]
SCRIPT = ROOT / ".github" / "scripts" / "repository_safety.py"
SECURITY_TOOLS_LOCK = ROOT / ".github" / "security-tools.lock"


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
    migration_text = (
        "create table if not exists public.events (id bigint);\n"
        "alter table public.events enable row level security;\n"
    )
    (migration_dir / "0001.sql").write_text(
        migration_text,
        encoding="utf-8",
        newline="\n",
    )
    (tmp_path / "supabase" / "migration-checksums.v1.json").write_text(
        json.dumps(
            {
                "algorithm": "sha256",
                "canonicalization": "utf-8-lf",
                "migrations": {
                    "0001.sql": hashlib.sha256(migration_text.encode("utf-8")).hexdigest(),
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
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


def test_receiver_secret_names_are_consistent_across_policy_and_examples() -> None:
    module = _module()
    expected_secret_names = {
        "ALERT_WEBHOOK_URL",
        "ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64",
        "ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64",
        "DEAD_MAN_ALERT_WEBHOOK_URL",
        "DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64",
        "DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64",
    }
    example_names = expected_secret_names | {
        "ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_ID",
        "ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_ID",
        "DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_ID",
        "DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_ID",
    }

    assert expected_secret_names <= set(module.PROTECTED_SECRET_NAMES)
    for example in (ROOT / ".env.example", ROOT / "apps" / "worker" / ".env.example"):
        assignments = {
            line.partition("=")[0]: line.partition("=")[2]
            for line in example.read_text(encoding="utf-8").splitlines()
            if "=" in line
        }
        assert example_names <= assignments.keys()
        assert all(assignments[name] == "" for name in expected_secret_names)

    scanner_path = ROOT / ".github" / "scripts" / "secret_assignment_scan.sh"
    assert scanner_path.is_file()
    assert not scanner_path.is_symlink()
    assert "*.sh text eol=lf" in (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assignment_scan = scanner_path.read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "security.yml").read_text(encoding="utf-8")
    assert "run: bash .github/scripts/secret_assignment_scan.sh" in workflow
    assert expected_secret_names <= {
        name for name in expected_secret_names if name in assignment_scan
    }
    assert "':!**/tests/**'" not in assignment_scan
    assert "quoted_secret_assignment_pattern=" in assignment_scan
    assert "quoted_shell_secret_assignment_pattern=" in assignment_scan
    assert "env_secret_assignment_pattern=" in assignment_scan
    assert "yaml_secret_assignment_pattern=" in assignment_scan
    assert "yaml_block_secret_assignment_pattern=" in assignment_scan
    assert "yaml_indirect_secret_assignment_pattern=" in assignment_scan
    assert "yaml_multiline_secret_assignment_pattern=" in assignment_scan
    assert "yaml_escaped_key_pattern=" in assignment_scan
    assert "yaml_noncanonical_key_prefix_pattern=" in assignment_scan
    assert "Secret assignment scanner contract self-check failed." in assignment_scan
    assert "negative_probes=" in assignment_scan
    assert "non_yaml_negative_probes=" in assignment_scan
    assert "url_secret_probe_value=" in assignment_scan
    assert "dollar_url_secret_probe_value=" in assignment_scan
    assert "indented_export_secret_probe=" in assignment_scan
    assert "local_secret_probe=" in assignment_scan
    assert "readonly_secret_probe=" in assignment_scan
    assert "semicolon_secret_probe=" in assignment_scan
    assert "command_secret_probe=" in assignment_scan
    assert "prior_assignment_secret_probe=" in assignment_scan
    assert "multi_export_secret_probe=" in assignment_scan
    assert "command_env_secret_probe=" in assignment_scan
    assert "subshell_secret_probe=" in assignment_scan
    assert "quoted_export_secret_probe=" in assignment_scan
    assert "quoted_env_secret_probe=" in assignment_scan
    assert "quoted_declare_secret_probe=" in assignment_scan
    assert "append_secret_probe=" in assignment_scan
    assert "unquoted_yaml_url_secret_probe=" in assignment_scan
    assert "flow_yaml_url_secret_probe=" in assignment_scan
    assert "block_yaml_secret_probe=" in assignment_scan
    assert "tagged_yaml_secret_probe=" in assignment_scan
    assert "multiline_yaml_secret_probe=" in assignment_scan
    assert "unicode_yaml_secret_probe=" in assignment_scan
    assert "hex_yaml_secret_probe=" in assignment_scan
    assert "long_unicode_yaml_secret_probe=" in assignment_scan
    assert "explicit_yaml_secret_probe=" in assignment_scan
    assert "tagged_yaml_key_secret_probe=" in assignment_scan
    assert "anchored_yaml_key_secret_probe=" in assignment_scan
    assert "alias_yaml_key_probe=" in assignment_scan
    assert "unicode_yaml_value_probe=" in assignment_scan
    assert '"ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64": previous_key_b64,' in assignment_scan
    assert "<https-receiver-url>" in assignment_scan
    assert '[^<>\\"[:space:]]{15,}' in assignment_scan
    assert "[^<>'[:space:]]{15,}" in assignment_scan
    assert "}[[:space:]]*[:=][[:space:]]*[\\\"']?" not in assignment_scan
    assert "shell_assignment_lead_pattern=" in assignment_scan
    assert "${shell_assignment_lead_pattern}${protected_secret_name_pattern}" in assignment_scan
    assert "${literal_first_pattern}${literal_rest_pattern}" in assignment_scan
    assert "literal_rest_pattern='[A-Za-z0-9_./+=:@?%&$~#!-]{15,}'" in assignment_scan
    assert "-- ." in assignment_scan
    assert "':!docs/**'" not in assignment_scan


def test_render_receiver_secrets_are_sync_only_and_failure_domain_scoped() -> None:
    module = _module()
    render_text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    worker_block = module._render_service_block(render_text, "kr-trading-worker")

    assert worker_block is not None
    blocks = module._render_environment_blocks(worker_block)
    for name in module.RENDER_RECEIVER_SYNC_FALSE_NAMES:
        assert name in blocks
        assert re.search(r"^\s+sync:\s*false\s*$", blocks[name], re.MULTILINE)
        assert re.search(r"^\s+value\s*:", blocks[name], re.MULTILINE) is None
    assert set(module.RENDER_FORBIDDEN_DEAD_MAN_NAMES).isdisjoint(blocks)


def test_render_receiver_secret_literal_or_dead_man_leak_is_rejected(
    tmp_path: Path,
) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    render_text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    unsafe_literal = render_text.replace(
        "      - key: ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64\n        sync: false",
        "      - key: ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64\n"
        "        value: not-a-production-secret",
    ).replace(
        "      - key: ALERT_WEBHOOK_TIMEOUT_SEC",
        "      - key: DEAD_MAN_ALERT_WEBHOOK_URL\n"
        "        sync: false\n"
        "      - key: ALERT_WEBHOOK_TIMEOUT_SEC",
    )
    (root / "render.yaml").write_text(unsafe_literal, encoding="utf-8")

    findings = module.check_workflow_safety(root)

    assert (
        "render.yaml: ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64 must not contain a literal value"
    ) in findings
    assert (
        "render.yaml: DEAD_MAN_ALERT_WEBHOOK_URL must not be available to main worker"
    ) in findings


@pytest.mark.parametrize(
    "duplicate_key_line",
    (
        "      - key : LIVE_ORDER_EXECUTION_ENABLED",
        '      - "key" : "LIVE_ORDER_EXECUTION_ENABLED"',
        "      - 'key' : 'LIVE_ORDER_EXECUTION_ENABLED'",
        "      - key: LIVE_ORDER_EXECUTION_ENABLED # hidden duplicate",
    ),
)
def test_render_duplicate_environment_key_yaml_variants_are_rejected(
    tmp_path: Path,
    duplicate_key_line: str,
) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    render_text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    safe_block = '      - key: LIVE_ORDER_EXECUTION_ENABLED\n        value: "false"'
    assert render_text.count(safe_block) == 1
    (root / "render.yaml").write_text(
        render_text.replace(
            safe_block,
            f'{safe_block}\n{duplicate_key_line}\n        value: "true"',
        ),
        encoding="utf-8",
    )

    findings = module.check_workflow_safety(root)

    assert "render.yaml: duplicate env key LIVE_ORDER_EXECUTION_ENABLED" in findings
    assert (
        "render.yaml: LIVE_ORDER_EXECUTION_ENABLED must remain false until hosted approval"
        in findings
    )


@pytest.mark.parametrize(
    "unsupported_entry",
    (
        '      - { key: LIVE_ORDER_EXECUTION_ENABLED, value: "true" }',
        '      - key: !!str LIVE_ORDER_EXECUTION_ENABLED\n        value: "true"',
        '      - &unsafe\n        key: LIVE_ORDER_EXECUTION_ENABLED\n        value: "true"',
    ),
)
def test_render_unsupported_environment_key_syntax_fails_closed(
    tmp_path: Path,
    unsupported_entry: str,
) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    render_text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    safe_block = '      - key: LIVE_ORDER_EXECUTION_ENABLED\n        value: "false"'
    assert render_text.count(safe_block) == 1
    (root / "render.yaml").write_text(
        render_text.replace(safe_block, f"{safe_block}\n{unsupported_entry}"),
        encoding="utf-8",
    )

    findings = module.check_workflow_safety(root)

    assert (
        "render.yaml: envVars must be one canonical section of key/value-or-sync items" in findings
    )


@pytest.mark.parametrize(
    ("auto_deploy_replacement", "suffix"),
    (
        (
            '    autoDeployTrigger: "on"',
            '\n  - type: worker\n    name: harmless-decoy\n    autoDeployTrigger: "off"\n',
        ),
        (
            '    <<: { autoDeployTrigger: "off" }\n    autoDeployTrigger: "on"',
            "",
        ),
        (
            '    autoDeployTrigger: "off"\n    autoDeployTrigger: "on"',
            "",
        ),
    ),
)
def test_render_worker_auto_deploy_decoys_fail_closed(
    tmp_path: Path,
    auto_deploy_replacement: str,
    suffix: str,
) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    render_text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    safe_line = '    autoDeployTrigger: "off"'
    assert render_text.count(safe_line) == 1
    (root / "render.yaml").write_text(
        render_text.replace(safe_line, auto_deploy_replacement) + suffix,
        encoding="utf-8",
    )

    findings = module.check_workflow_safety(root)

    assert "render.yaml: worker autoDeployTrigger must remain off" in findings


@pytest.mark.parametrize("unsafe_value", ('"OFF"', "OFF"))
def test_render_worker_auto_deploy_value_is_case_sensitive(
    tmp_path: Path,
    unsafe_value: str,
) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    render_text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    safe_line = '    autoDeployTrigger: "off"'
    assert render_text.count(safe_line) == 1
    (root / "render.yaml").write_text(
        render_text.replace(safe_line, f"    autoDeployTrigger: {unsafe_value}"),
        encoding="utf-8",
    )

    findings = module.check_workflow_safety(root)

    assert "render.yaml: worker autoDeployTrigger must remain off" in findings


@pytest.mark.parametrize(
    "env_vars_replacement",
    (
        "    envVars: &worker-env",
        "    envVars: !!seq",
        "    envVars:\n"
        '      - { key: LIVE_ORDER_EXECUTION_ENABLED, value: "true" }\n'
        '    "envVars":',
    ),
)
def test_render_env_vars_heading_must_be_single_and_canonical(
    tmp_path: Path,
    env_vars_replacement: str,
) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    render_text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    safe_heading = "    envVars:"
    assert render_text.count(safe_heading) == 1
    (root / "render.yaml").write_text(
        render_text.replace(safe_heading, env_vars_replacement),
        encoding="utf-8",
    )

    findings = module.check_workflow_safety(root)

    assert (
        "render.yaml: envVars must be one canonical section of key/value-or-sync items" in findings
    )


@pytest.mark.parametrize(
    "unsafe_block",
    (
        '      - key: LIVE_ORDER_EXECUTION_ENABLED\n        value: "false"\n        value: "true"',
        "      - key: LIVE_ORDER_EXECUTION_ENABLED\n"
        '        <<: { value: "false" }\n'
        '        value: "true"',
        "      - key: LIVE_ORDER_EXECUTION_ENABLED\n"
        "        key: LIVE_ORDER_EXECUTION_ENABLED\n"
        '        value: "true"',
        '      - key: LIVE_ORDER_EXECUTION_ENABLED\n        "value": "true"',
    ),
)
def test_render_environment_item_schema_decoys_fail_closed(
    tmp_path: Path,
    unsafe_block: str,
) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    render_text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    safe_block = '      - key: LIVE_ORDER_EXECUTION_ENABLED\n        value: "false"'
    assert render_text.count(safe_block) == 1
    (root / "render.yaml").write_text(
        render_text.replace(safe_block, unsafe_block),
        encoding="utf-8",
    )

    findings = module.check_workflow_safety(root)

    assert (
        "render.yaml: envVars must be one canonical section of key/value-or-sync items" in findings
    )


@pytest.mark.parametrize("unsafe_sync", ('"false"', "FALSE", '"FALSE"'))
def test_render_environment_sync_boolean_must_be_canonical(
    tmp_path: Path,
    unsafe_sync: str,
) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    render_text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    safe_block = "      - key: ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64\n        sync: false"
    assert render_text.count(safe_block) == 1
    (root / "render.yaml").write_text(
        render_text.replace(safe_block, safe_block.replace("false", unsafe_sync)),
        encoding="utf-8",
    )

    findings = module.check_workflow_safety(root)

    assert (
        "render.yaml: envVars must be one canonical section of key/value-or-sync items" in findings
    )


def test_render_live_safety_values_are_case_sensitive(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    render_text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    safe_block = '      - key: LIVE_ORDER_EXECUTION_ENABLED\n        value: "false"'
    assert render_text.count(safe_block) == 1
    (root / "render.yaml").write_text(
        render_text.replace(safe_block, safe_block.replace('"false"', '"FALSE"')),
        encoding="utf-8",
    )

    findings = module.check_workflow_safety(root)

    assert (
        "render.yaml: LIVE_ORDER_EXECUTION_ENABLED must remain false until hosted approval"
        in findings
    )


def test_render_duplicate_worker_service_is_rejected(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    render_text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    duplicate = (
        "\n  - type: worker\n"
        "    name: kr-trading-worker\n"
        '    autoDeployTrigger: "off"\n'
        "    envVars:\n"
        "      - key: ENV\n"
        "        value: production\n"
    )
    (root / "render.yaml").write_text(
        render_text + duplicate,
        encoding="utf-8",
    )

    findings = module.check_workflow_safety(root)

    assert "render.yaml: exactly one canonical kr-trading-worker service is required" in findings


@pytest.mark.parametrize(
    ("prefix", "needle", "replacement", "suffix"),
    (
        (
            "",
            "    name: kr-trading-worker",
            "    name: kr-trading-worker",
            "\n  - type: worker\n"
            "    name: &duplicate kr-trading-worker\n"
            '    autoDeployTrigger: "off"\n',
        ),
        (
            "",
            "    name: kr-trading-worker",
            "    name: kr-trading-worker",
            '\n  - type: worker\n    name: !!str kr-trading-worker\n    autoDeployTrigger: "off"\n',
        ),
        (
            "",
            "    name: kr-trading-worker",
            "    name: kr-trading-worker",
            '\n  - { type: worker, name: kr-trading-worker, autoDeployTrigger: "off" }\n',
        ),
        (
            "workerName: &canonical-worker kr-trading-worker\n",
            "    name: kr-trading-worker",
            "    name: kr-trading-worker",
            '\n  - type: worker\n    name: *canonical-worker\n    autoDeployTrigger: "off"\n',
        ),
        (
            "",
            "    name: kr-trading-worker",
            "    name: kr-trading-worker\n    name: harmless-decoy",
            "",
        ),
        (
            "",
            "  - type: worker",
            "  - type: worker\n    type: web",
            "",
        ),
        (
            "",
            "    name: kr-trading-worker",
            "    name: kr-trading-worker\n" r'    "na\u006de": harmless-decoy',
            "",
        ),
        (
            "",
            '    autoDeployTrigger: "off"',
            '    autoDeployTrigger: "off"\n'
            r'    "autoDeploy\u0054rigger": "on"',
            "",
        ),
        (
            "",
            "    envVars:",
            "    envVars:\n" r'    "env\u0056ars": []',
            "",
        ),
    ),
)
def test_render_noncanonical_service_schema_decoys_fail_closed(
    tmp_path: Path,
    prefix: str,
    needle: str,
    replacement: str,
    suffix: str,
) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    render_text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert render_text.count(needle) == 1
    (root / "render.yaml").write_text(
        prefix + render_text.replace(needle, replacement) + suffix,
        encoding="utf-8",
    )

    findings = module.check_workflow_safety(root)

    assert "render.yaml: exactly one canonical kr-trading-worker service is required" in findings


@pytest.mark.parametrize(
    "top_level_override",
    (
        r'"serv\u0069ces": []',
        "!!str services: []",
        "&services-key services: []",
        "services: []",
    ),
)
def test_render_top_level_service_overrides_fail_closed(
    tmp_path: Path,
    top_level_override: str,
) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    render_text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    (root / "render.yaml").write_text(
        render_text + "\n" + top_level_override + "\n",
        encoding="utf-8",
    )

    findings = module.check_workflow_safety(root)

    assert "render.yaml: exactly one canonical kr-trading-worker service is required" in findings


@pytest.mark.parametrize(
    ("canonical_line", "unsafe_line"),
    (
        ("    region: singapore", "    region: singapore: invalid"),
        ("    plan: starter", "    plan: !!str starter"),
        ("    branch: main", "    branch: &branch main"),
        ("    rootDir: apps/worker", "    rootDir: *root"),
        ("    numInstances: 1", '    numInstances: "1"'),
        (
            "    maxShutdownDelaySeconds: 120",
            "    maxShutdownDelaySeconds: 12_0",
        ),
    ),
)
def test_render_worker_direct_scalars_must_be_canonical(
    tmp_path: Path,
    canonical_line: str,
    unsafe_line: str,
) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    render_text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert render_text.count(canonical_line) == 1
    (root / "render.yaml").write_text(
        render_text.replace(canonical_line, unsafe_line),
        encoding="utf-8",
    )

    findings = module.check_workflow_safety(root)

    assert "render.yaml: exactly one canonical kr-trading-worker service is required" in findings


@pytest.mark.parametrize(
    ("quoted_command", "unsafe_command"),
    (
        (
            "    buildCommand: 'python -m app.tools.write_release_metadata && "
            "python -m pip install --require-hashes --only-binary=:all: "
            "-r requirements.lock'",
            "    buildCommand: python -m app.tools.write_release_metadata && "
            "python -m pip install --require-hashes --only-binary=:all: "
            "-r requirements.lock",
        ),
        (
            "    startCommand: 'python -m app.main'",
            "    startCommand: python -m app.main",
        ),
        (
            "    startCommand: 'python -m app.main'",
            "    startCommand: !!str 'python -m app.main'",
        ),
        (
            "    startCommand: 'python -m app.main'",
            "    startCommand: >-\n      python -m app.main",
        ),
    ),
)
def test_render_worker_commands_require_quoted_yaml_scalars(
    tmp_path: Path,
    quoted_command: str,
    unsafe_command: str,
) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    render_text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert render_text.count(quoted_command) == 1
    (root / "render.yaml").write_text(
        render_text.replace(quoted_command, unsafe_command),
        encoding="utf-8",
    )

    findings = module.check_workflow_safety(root)

    assert "render.yaml: worker build and start commands must use quoted scalars" in findings


def test_render_environment_must_remain_production_for_receiver_startup_gate(
    tmp_path: Path,
) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    render_text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    (root / "render.yaml").write_text(
        render_text.replace(
            "      - key: ENV\n        value: production",
            "      - key: ENV\n        value: local",
        ),
        encoding="utf-8",
    )

    findings = module.check_workflow_safety(root)

    assert "render.yaml: ENV must remain production until hosted approval" in findings


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
        "supabase/migrations/0003.sql: destructive migration missing rollback note or approval"
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
        "-- Example only: drop table private.execution_history.\nselect 1;\n",
        encoding="utf-8",
    )

    findings = module.check_migration_safety(root)

    assert not any("destructive migration" in finding for finding in findings)


def test_multiline_drop_column_requires_same_file_approval(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / "supabase" / "migrations" / "0002.sql").write_text(
        "alter table private.execution_history\n  drop\n  column provider_payload;\n",
        encoding="utf-8",
    )

    findings = module.check_migration_safety(root)

    assert (
        "supabase/migrations/0002.sql: destructive migration missing rollback note or approval"
    ) in findings


def test_approval_phrase_inside_sql_string_is_not_evidence(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / "supabase" / "migrations" / "0002.sql").write_text(
        "select '-- Rollback note: fake approval';\ntruncate table private.execution_history;\n",
        encoding="utf-8",
    )

    findings = module.check_migration_safety(root)

    assert (
        "supabase/migrations/0002.sql: destructive migration missing rollback note or approval"
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
        "supabase/migrations/0002.sql: destructive migration missing rollback note or approval"
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
        "create view api.runtime_status with (security_invoker = true) as select 1 as id;\n",
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
    assert "exposed function missing explicit SECURITY INVOKER: api.unsafe_rpc" in findings


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
        "exposed function missing explicit SECURITY INVOKER: worker_api.acquire_worker_lease"
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

    assert "private SECURITY DEFINER missing empty search_path: private.unsafe_impl" in findings


def test_worker_api_function_must_be_allowlisted(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / "supabase" / "migrations" / "0002.sql").write_text(
        "create function worker_api.place_live_order() returns void language sql as 'select';\n",
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
        "worker_api function outside allowlist: register_qualification_run_v2" in finding
        for finding in findings
    )


def test_pit_daily_candle_collection_worker_rpcs_are_exactly_allowlisted() -> None:
    module = _module()
    expected = {
        "load_or_create_pit_daily_candle_collection_job_v1",
        "inspect_pit_daily_candle_collection_job_v1",
        "begin_pit_daily_candle_collection_attempt_v1",
        "fence_pit_daily_candle_collection_candidate_v1",
        "pause_pit_daily_candle_collection_attempt_v1",
        "block_pit_daily_candle_collection_attempt_v1",
        "confirm_pit_daily_candle_collection_attempt_v1",
    }

    assert expected <= module.WORKER_API_ALLOWLIST
    assert "start_append_pit_daily_candle_collection_attempt_v1" not in (
        module.WORKER_API_ALLOWLIST
    )


def test_worker_api_execute_cannot_be_granted_to_authenticated(
    tmp_path: Path,
) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / "supabase" / "migrations" / "0002.sql").write_text(
        "grant execute on function worker_api.acquire_worker_lease(uuid) to authenticated;\n",
        encoding="utf-8",
    )

    findings = module.check_migration_safety(root)

    assert "worker_api EXECUTE granted to desktop/public role" in findings


def test_multiline_anon_write_policy_is_rejected(tmp_path: Path) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    (root / "supabase" / "migrations" / "0002.sql").write_text(
        "create policy unsafe_write\non public.events\nfor update\nto anon\nusing (true);\n",
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


@pytest.mark.parametrize(
    "key",
    [
        "'alert_webhook_receiver_ack_current_key_b64'",
        '"DEAD_MAN_ALERT_WEBHOOK_URL"',
    ],
)
def test_quoted_protected_secret_env_keys_are_rejected(
    tmp_path: Path,
    key: str,
) -> None:
    module = _module()
    root = _safe_repository(tmp_path)
    workflow = root / ".github" / "workflows" / "unsafe.yml"
    workflow.write_text(
        f"env:\n  {key}: placeholder\n",
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
        "jobs:\n  test:\n    steps:\n      - uses: actions/checkout@" + "a" * 40 + " # v4\n",
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
    text = (ROOT / ".github" / "workflows" / "security.yml").read_text(encoding="utf-8")

    for name in ("npm audit gate", "pip-audit gate", "bandit gate"):
        start = text.index(f"- name: {name}")
        next_step = text.find("\n      - name:", start + 1)
        block = text[start:] if next_step == -1 else text[start:next_step]
        assert "continue-on-error" not in block


def test_security_workflow_runs_on_develop_and_main_pushes() -> None:
    text = (ROOT / ".github" / "workflows" / "security.yml").read_text(encoding="utf-8")

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
        "dependency-evidence",
        "workflow-policy",
    ):
        assert "\n    if:" not in _workflow_job_block(text, job_name)

    dependency_review = _workflow_job_block(text, "dependency-review")
    assert "\n    if: github.event_name == 'pull_request'" in dependency_review


def test_security_audit_tools_use_complete_hashed_lock() -> None:
    workflow = (ROOT / ".github" / "workflows" / "security.yml").read_text(encoding="utf-8")
    audit_job = _workflow_job_block(workflow, "security-audits")
    install_command = (
        "python -m pip install --force-reinstall --require-hashes "
        "--only-binary=:all: -r .github/security-tools.lock"
    )
    assert "\n    runs-on: ubuntu-24.04\n" in audit_job
    assert 'python-version: "3.12.13"' in audit_job
    assert f'run: "{install_command}"' in audit_job
    pip_install_commands = [
        line.strip().removeprefix("run:").strip().strip('"')
        for line in audit_job.splitlines()
        if re.search(r"\bpip\s+install\b", line)
    ]
    assert pip_install_commands == [install_command]

    dependency_check_command = "python -m pip check"
    tool_audit_command = "python -m pip_audit -r .github/security-tools.lock"
    worker_audit_command = "python -m pip_audit -r requirements.lock"
    assert dependency_check_command in audit_job
    assert tool_audit_command in audit_job
    assert audit_job.index(install_command) < audit_job.index(dependency_check_command)
    assert audit_job.index(dependency_check_command) < audit_job.index(tool_audit_command)
    assert audit_job.index(tool_audit_command) < audit_job.index(worker_audit_command)

    requirement_pattern = re.compile(
        r"^(?P<name>[A-Za-z0-9_.-]+)==(?P<version>[^ ]+) "
        r"--hash=sha256:(?P<digest>[0-9a-f]{64})$"
    )
    requirements = [
        line
        for line in SECURITY_TOOLS_LOCK.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    matches = [requirement_pattern.fullmatch(line) for line in requirements]
    assert requirements
    assert all(match is not None for match in matches)

    normalized_names = [
        match.group("name").casefold().replace("_", "-").replace(".", "-")
        for match in matches
        if match is not None
    ]
    assert len(normalized_names) == len(set(normalized_names))
    assert {"bandit", "pip", "pip-audit"}.issubset(normalized_names)
