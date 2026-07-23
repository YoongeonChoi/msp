from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

PROTECTED_SECRET_NAMES = (
    "SUPABASE_SECRET_KEY",
    "TOSS_CLIENT_SECRET",
    "OPENAI_API_KEY",
    "NAVER_CLIENT_SECRET",
    "KRX_API_KEY",
    "OPENDART_API_KEY",
    "ALERT_WEBHOOK_URL",
    "ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64",
    "ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64",
    "DEAD_MAN_ALERT_WEBHOOK_URL",
    "DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64",
    "DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64",
    "RENDER_DEPLOY_HOOK_URL",
    "SUPABASE_LIVE_REQUESTER_JWT",
    "SUPABASE_LIVE_REVIEWER_JWT",
)

RENDER_RECEIVER_SYNC_FALSE_NAMES = (
    "ALERT_WEBHOOK_URL",
    "ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_ID",
    "ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64",
    "ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_ID",
    "ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64",
)

RENDER_FORBIDDEN_DEAD_MAN_NAMES = (
    "DEAD_MAN_ALERT_WEBHOOK_URL",
    "DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_ID",
    "DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64",
    "DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_ID",
    "DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64",
)

_RENDER_CANONICAL_SERVICE_FIELDS = frozenset(
    {
        "autoDeployTrigger",
        "branch",
        "buildCommand",
        "envVars",
        "maxShutdownDelaySeconds",
        "name",
        "numInstances",
        "plan",
        "region",
        "rootDir",
        "runtime",
        "startCommand",
    }
)

_DESTRUCTIVE_APPROVAL_PATTERN = re.compile(
    r"\b(?:rollback\s+note|destructive\s+migration\s+approved)\s*:\s*\S",
    re.IGNORECASE,
)
_DOLLAR_QUOTE_PATTERN = re.compile(r"\$(?:[a-z_][a-z0-9_]*)?\$", re.IGNORECASE)
_EXPOSED_TABLE_PATTERN = re.compile(
    r"\bcreate\s+(?:unlogged\s+)?table\s+(?:if\s+not\s+exists\s+)?"
    r'(?P<schema>public|api|"public"|"api")\s*\.\s*'
    r'"?(?P<table>[a-z_][a-z0-9_]*)"?',
    re.IGNORECASE,
)
_RLS_PATTERN = re.compile(
    r"\balter\s+table\s+(?:if\s+exists\s+)?"
    r'(?P<schema>public|api|"public"|"api")\s*\.\s*'
    r'"?(?P<table>[a-z_][a-z0-9_]*)"?'
    r"\s+enable\s+row\s+level\s+security",
    re.IGNORECASE,
)
_RLS_DISABLE_PATTERN = re.compile(
    r"\balter\s+table\s+(?:if\s+exists\s+)?"
    r'(?P<schema>public|api|"public"|"api")\s*\.\s*'
    r'"?(?P<table>[a-z_][a-z0-9_]*)"?'
    r"\s+disable\s+row\s+level\s+security",
    re.IGNORECASE,
)
_API_VIEW_PATTERN = re.compile(
    r"\bcreate\s+(?:or\s+replace\s+)?view\s+"
    r'(?:api|"api")\s*\.\s*"?([a-z_][a-z0-9_]*)"?'
    r"(?P<options>.*?)\bas\b",
    re.IGNORECASE | re.DOTALL,
)
_WORKER_API_FUNCTION_PATTERN = re.compile(
    r"\bcreate\s+(?:or\s+replace\s+)?function\s+"
    r'(?:worker_api|"worker_api")\s*\.\s*"?([a-z_][a-z0-9_]*)"?',
    re.IGNORECASE,
)
_EXPOSED_FUNCTION_PATTERN = re.compile(
    r"\bcreate\s+(?:or\s+replace\s+)?function\s+"
    r'(?P<schema>api|worker_api|"api"|"worker_api")\s*\.\s*'
    r'"?(?P<name>[a-z_][a-z0-9_]*)"?',
    re.IGNORECASE,
)
_PRIVATE_FUNCTION_PATTERN = re.compile(
    r"\bcreate\s+(?:or\s+replace\s+)?function\s+"
    r'(?:private|"private")\s*\.\s*'
    r'"?(?P<name>[a-z_][a-z0-9_]*)"?',
    re.IGNORECASE,
)
_FUNCTION_BODY_START_PATTERN = re.compile(
    r"\bas\s+\$(?:[a-z_][a-z0-9_]*)?\$",
    re.IGNORECASE,
)
_WORKER_API_UNSAFE_EXECUTE_GRANT_PATTERN = re.compile(
    r"\bgrant\s+execute\s+on\s+function\s+"
    r'(?:worker_api|"worker_api")\s*\.\s*[^;]*?'
    r"\bto\s+(?:anon|authenticated|public)\b",
    re.IGNORECASE,
)
_POLICY_PATTERN = re.compile(r"\bcreate\s+policy\b.*?;", re.IGNORECASE | re.DOTALL)
_UNSAFE_WORKFLOW_DEPLOY_PATTERN = re.compile(
    r"\brender\s+deploy\b|render\.com/deploy|autoDeployTrigger\s*:\s*[\"']?on\b",
    re.IGNORECASE,
)
_WORKFLOW_ACTION_PATTERN = re.compile(
    r"^\s*(?:-\s*)?uses\s*:\s*['\"]?([^'\"#\s]+)",
    re.IGNORECASE | re.MULTILINE,
)
_FULL_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_GITLEAKS_FINGERPRINT_PATTERN = re.compile(
    r"^[0-9a-f]{40}:[^:\r\n]+:[a-z0-9][a-z0-9-]*:[1-9][0-9]*$"
)
APPROVED_GITLEAKS_FINGERPRINTS = frozenset(
    {
        "731556ab70f0e434a062f43e974049aa9953bf27:apps/desktop/playwright.config.ts:generic-api-key:19",
        "836840973aae66189e78f4ff855dccf9505a0d37:render.yaml:generic-api-key:67",
        "836840973aae66189e78f4ff855dccf9505a0d37:render.yaml:generic-api-key:69",
        "b958e605633f6d6e933eef88859ec113cd0e4713:supabase/verify_g1_g2_migration.py:generic-api-key:52",
        "f43029dc833718ef79087f0c8eeb1359616d7f36:apps/desktop/playwright.config.ts:generic-api-key:16",
        "f43029dc833718ef79087f0c8eeb1359616d7f36:apps/worker/app/tests/unit/test_webhook_alert_notifier.py:generic-api-key:27",
        "f43029dc833718ef79087f0c8eeb1359616d7f36:apps/worker/app/tests/unit/test_redaction.py:generic-api-key:8",
        "8904120101ec6bec62b8585f3e3341f66930edcd:apps/worker/app/tests/unit/test_redaction.py:generic-api-key:5",
        "2c499a729405f68966a7aa1f8d7bcecd95d7b0ef:apps/desktop/playwright.config.ts:generic-api-key:19",
        "2c499a729405f68966a7aa1f8d7bcecd95d7b0ef:apps/desktop/tests/supabaseConfigSecurity.test.ts:generic-api-key:5",
        "1dc8a627a16373b2728a0f6d16343013bc29c5bf:supabase/migration-checksums.v1.json:generic-api-key:15",
        "1dc8a627a16373b2728a0f6d16343013bc29c5bf:supabase/migration-checksums.v1.json:generic-api-key:22",
        "1dc8a627a16373b2728a0f6d16343013bc29c5bf:supabase/migration-checksums.v1.json:generic-api-key:23",
    }
)

RENDER_NO_LIVE_ENV = {
    "ENV": "production",
    "BOT_DEFAULT_MODE": "paper",
    "TOSS_CREDENTIAL_SCOPE": "read_only",
    "TOSS_ORDER_CAPABLE_CREDENTIALS": "false",
    "LIVE_ORDER_EXECUTION_ENABLED": "false",
    "TOSS_ORDER_ENDPOINT_ENABLED": "false",
    # V2 hosted rollout needs separate approval; Production Live remains forbidden.
    "EXECUTION_V2_ENABLED": "false",
    "EXECUTION_V2_WORKER_API_ENABLED": "false",
    "EXECUTION_V2_PAPER_RESUME_INPUT_ENABLED": "false",
    "EXECUTION_V2_PAPER_SOURCE_INPUT_ENABLED": "false",
}

WORKER_API_ALLOWLIST = frozenset(
    {
        "acquire_worker_lease",
        "renew_worker_lease",
        "release_worker_lease",
        "record_worker_heartbeat",
        "reserve_order_intent",
        "mark_dispatch_started",
        "record_execution_observation",
        "claim_delivery_outbox",
        "claim_cash_settlement_batch",
        "complete_outbox_delivery",
        "complete_cash_settlement",
        "fail_outbox_delivery",
        "fail_cash_settlement_attempt",
        "acknowledge_operation_command",
        "claim_execution_reconciliation_batch",
        "complete_execution_reconciliation",
        "expire_paper_intent_remainder",
        "fail_reserved_intent_pre_dispatch",
        "load_paper_execution_checkpoint",
        "claim_operation_command_batch",
        "capture_qualification_snapshot_v1",
        "register_qualification_run_v1",
        "register_qualification_run_v2",
        "get_dead_man_snapshot_v1",
        "list_due_cash_settlement_accounts",
        "ingest_paper_bar_fixture_v1",
        "enqueue_paper_execution_candidate_v1",
        "claim_paper_execution_v1",
        "load_claimed_paper_execution_bundle_v1",
        "complete_paper_execution_source_v1",
        "list_unknown_resolution_v2",
        "claim_unknown_resolution_v2",
        "apply_unknown_resolution_v2",
        "append_pit_candle_observation_v1",
        "append_pit_kr_daily_session_observation_v1",
        "append_pit_daily_candle_timing_evidence_v1",
        "list_pit_daily_candles_as_of_v1",
        "list_pit_kr_daily_sessions_as_of_v1",
        "load_or_create_kr_calendar_collection_job_v1",
        "inspect_kr_calendar_collection_job_v1",
        "begin_kr_calendar_collection_date_attempt_v1",
        "pause_kr_calendar_collection_date_attempt_v1",
        "block_kr_calendar_collection_date_attempt_v1",
        "confirm_kr_calendar_collection_date_v1",
        "load_or_create_pit_daily_candle_collection_job_v1",
        "inspect_pit_daily_candle_collection_job_v1",
        "begin_pit_daily_candle_collection_attempt_v1",
        "fence_pit_daily_candle_collection_candidate_v1",
        "pause_pit_daily_candle_collection_attempt_v1",
        "block_pit_daily_candle_collection_attempt_v1",
        "confirm_pit_daily_candle_collection_attempt_v1",
    }
)


def _qualified_objects(pattern: re.Pattern[str], sql: str) -> set[tuple[str, str]]:
    return {
        (
            match.group("schema").strip('"').casefold(),
            match.group("table").casefold(),
        )
        for match in pattern.finditer(sql)
    }


def _check_migration_checksums(repo_root: Path, paths: list[Path]) -> list[str]:
    manifest_path = repo_root / "supabase" / "migration-checksums.v1.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return ["supabase/migration-checksums.v1.json: regular file required"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ["supabase/migration-checksums.v1.json: invalid UTF-8 JSON"]
    if not isinstance(manifest, dict):
        return ["supabase/migration-checksums.v1.json: object required"]
    if manifest.get("algorithm") != "sha256":
        return ["supabase/migration-checksums.v1.json: sha256 required"]
    if manifest.get("canonicalization") != "utf-8-lf":
        return ["supabase/migration-checksums.v1.json: utf-8-lf required"]
    expected = manifest.get("migrations")
    if not isinstance(expected, dict) or any(
        not isinstance(name, str) or not isinstance(digest, str)
        for name, digest in expected.items()
    ):
        return ["supabase/migration-checksums.v1.json: migration map required"]

    findings: list[str] = []
    actual_names = {path.name for path in paths}
    expected_names = set(expected)
    findings.extend(
        f"migration checksum missing: {name}" for name in sorted(actual_names - expected_names)
    )
    findings.extend(
        f"migration checksum references missing file: {name}"
        for name in sorted(expected_names - actual_names)
    )
    for path in paths:
        if path.name not in expected:
            continue
        if path.is_symlink() or not path.is_file():
            findings.append(f"migration must be a regular file: {path.name}")
            continue
        expected_digest = expected[path.name]
        if re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
            findings.append(f"invalid migration checksum: {path.name}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            findings.append(f"migration is not canonical UTF-8: {path.name}")
            continue
        if text.startswith("\ufeff"):
            findings.append(f"migration contains UTF-8 BOM: {path.name}")
            continue
        actual_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if actual_digest != expected_digest:
            findings.append(f"historical migration checksum changed: {path.name}")
    return findings


def check_migration_safety(repo_root: Path) -> list[str]:
    migration_dir = repo_root / "supabase" / "migrations"
    paths = sorted(migration_dir.glob("*.sql"))
    if not paths:
        return ["supabase/migrations: no SQL migrations found"]

    migration_sql = [(path, path.read_text(encoding="utf-8")) for path in paths]
    sql = "\n".join(text for _path, text in migration_sql)
    normalized = _strip_sql_comments(sql)
    exposed_tables = _qualified_objects(_EXPOSED_TABLE_PATTERN, normalized)
    rls_tables = _qualified_objects(_RLS_PATTERN, normalized)
    findings = _check_migration_checksums(repo_root, paths)
    findings.extend(
        f"exposed table missing RLS: {schema}.{table}"
        for schema, table in sorted(exposed_tables - rls_tables)
    )
    findings.extend(
        f"{path.relative_to(repo_root).as_posix()}: destructive migration "
        "missing rollback note or approval"
        for path, text in migration_sql
        if _contains_destructive_migration_sql(text)
        and not _has_same_file_destructive_approval(text)
    )
    findings.extend(
        f"exposed table disables RLS: {schema}.{table}"
        for schema, table in sorted(_qualified_objects(_RLS_DISABLE_PATTERN, normalized))
    )

    for match in _API_VIEW_PATTERN.finditer(normalized):
        options = " ".join(match.group("options").casefold().split())
        if re.search(r"\bsecurity_invoker\s*=\s*true\b", options) is None:
            findings.append(f"api view missing security_invoker=true: {match.group(1)}")

    for match, header in _function_headers(_EXPOSED_FUNCTION_PATTERN, normalized):
        schema = match.group("schema").strip('"').casefold()
        name = match.group("name").casefold()
        if re.search(r"\bsecurity\s+invoker\b", header, re.IGNORECASE) is None:
            findings.append(f"exposed function missing explicit SECURITY INVOKER: {schema}.{name}")
        if re.search(r"\bsecurity\s+definer\b", header, re.IGNORECASE):
            findings.append(f"exposed function uses SECURITY DEFINER: {schema}.{name}")

    for match, header in _function_headers(_PRIVATE_FUNCTION_PATTERN, normalized):
        if (
            re.search(r"\bsecurity\s+definer\b", header, re.IGNORECASE)
            and re.search(r"\bset\s+search_path\s*=\s*''", header, re.IGNORECASE) is None
        ):
            findings.append(
                "private SECURITY DEFINER missing empty search_path: "
                f"private.{match.group('name').casefold()}"
            )

    worker_functions = {
        name.casefold() for name in _WORKER_API_FUNCTION_PATTERN.findall(normalized)
    }
    findings.extend(
        f"worker_api function outside allowlist: {name}"
        for name in sorted(worker_functions - WORKER_API_ALLOWLIST)
    )
    if _WORKER_API_UNSAFE_EXECUTE_GRANT_PATTERN.search(normalized):
        findings.append("worker_api EXECUTE granted to desktop/public role")

    for statement in _POLICY_PATTERN.findall(normalized):
        if _policy_grants_anon_or_public_write(statement):
            compact = " ".join(statement.split())[:160]
            findings.append(f"anon/public write-capable policy: {compact}")
    return findings


def _contains_destructive_migration_sql(sql: str) -> bool:
    tokens = _sql_tokens_outside_string_literals(_strip_sql_comments(sql))
    return any(
        token == "truncate"
        or (
            token == "drop" and index + 1 < len(tokens) and tokens[index + 1] in {"table", "column"}
        )
        for index, token in enumerate(tokens)
    )


def _has_same_file_destructive_approval(sql: str) -> bool:
    return any(
        _DESTRUCTIVE_APPROVAL_PATTERN.search(
            comment[2:-2] if comment.startswith("/*") else comment[2:]
        )
        is not None
        for comment in _top_level_sql_comments(sql)
    )


def _strip_sql_comments(sql: str) -> str:
    """Remove SQL comments without treating markers inside quotes as comments."""

    pieces: list[str] = []
    cursor = 0
    index = 0
    while index < len(sql):
        if sql[index] in {"'", '"'}:
            index = _quoted_sql_end(sql, index)
            continue
        if sql.startswith("--", index):
            pieces.append(sql[cursor:index])
            line_end = sql.find("\n", index + 2)
            if line_end == -1:
                pieces.append(" ")
                cursor = len(sql)
                index = len(sql)
                continue
            pieces.append("\n")
            cursor = line_end + 1
            index = cursor
            continue
        if sql.startswith("/*", index):
            pieces.append(sql[cursor:index])
            index = _block_comment_end(sql, index)
            pieces.append(" ")
            cursor = index
            continue
        index += 1
    pieces.append(sql[cursor:])
    return "".join(pieces)


def _top_level_sql_comments(sql: str) -> list[str]:
    """Return comments outside quoted values and dollar-quoted function bodies."""

    comments: list[str] = []
    index = 0
    while index < len(sql):
        if sql[index] in {"'", '"'}:
            index = _quoted_sql_end(sql, index)
            continue
        if sql[index] == "$":
            delimiter_match = _DOLLAR_QUOTE_PATTERN.match(sql, index)
            if delimiter_match is not None:
                delimiter = delimiter_match.group(0)
                closing = sql.find(delimiter, delimiter_match.end())
                index = len(sql) if closing == -1 else closing + len(delimiter)
                continue
        if sql.startswith("--", index):
            line_end = sql.find("\n", index + 2)
            line_end = len(sql) if line_end == -1 else line_end
            comments.append(sql[index:line_end])
            index = line_end
            continue
        if sql.startswith("/*", index):
            comment_end = _block_comment_end(sql, index)
            comments.append(sql[index:comment_end])
            index = comment_end
            continue
        index += 1
    return comments


def _quoted_sql_end(sql: str, start: int) -> int:
    quote = sql[start]
    index = start + 1
    while index < len(sql):
        if sql[index] == "\\" and index + 1 < len(sql):
            index += 2
            continue
        if sql[index] == quote:
            if index + 1 < len(sql) and sql[index + 1] == quote:
                index += 2
                continue
            return index + 1
        index += 1
    return len(sql)


def _block_comment_end(sql: str, start: int) -> int:
    depth = 1
    index = start + 2
    while index < len(sql) and depth > 0:
        if sql.startswith("/*", index):
            depth += 1
            index += 2
            continue
        if sql.startswith("*/", index):
            depth -= 1
            index += 2
            continue
        index += 1
    return index


def _function_headers(pattern: re.Pattern[str], sql: str) -> list[tuple[re.Match[str], str]]:
    """Return declarations up to the dollar-quoted function body."""

    headers: list[tuple[re.Match[str], str]] = []
    next_function_pattern = re.compile(r"\bcreate\s+(?:or\s+replace\s+)?function\b", re.IGNORECASE)
    for match in pattern.finditer(sql):
        body_start = _FUNCTION_BODY_START_PATTERN.search(sql, match.end())
        next_function = next_function_pattern.search(sql, match.end())
        if body_start is None or (
            next_function is not None and next_function.start() < body_start.start()
        ):
            headers.append((match, sql[match.start() : match.end()]))
            continue
        headers.append((match, sql[match.start() : body_start.start()]))
    return headers


def _policy_grants_anon_or_public_write(statement: str) -> bool:
    tokens = _sql_tokens_outside_string_literals(statement)
    try:
        policy_index = tokens.index("policy")
        on_index = tokens.index("on", policy_index + 2)
    except ValueError:
        return True

    command = "all"
    roles = {"public"}
    index = on_index + 2
    while index < len(tokens):
        token = tokens[index]
        if token == "for":
            if index + 1 >= len(tokens):
                return True
            command = tokens[index + 1]
            index += 2
            continue
        if token == "to":
            parsed_roles: set[str] = set()
            index += 1
            while index < len(tokens) and tokens[index] not in {"using", "with"}:
                if tokens[index] not in {"as", "for"}:
                    parsed_roles.add(tokens[index])
                index += 1
            if not parsed_roles:
                return True
            roles = parsed_roles
            continue
        index += 1
    return command != "select" and bool(roles & {"anon", "public"})


def _sql_tokens_outside_string_literals(statement: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    while index < len(statement):
        character = statement[index]
        if character.isspace() or character in ",;()":
            index += 1
            continue
        if character == "'":
            index += 1
            while index < len(statement):
                if statement[index] == "'":
                    if index + 1 < len(statement) and statement[index + 1] == "'":
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue
        if character == '"':
            index += 1
            value: list[str] = []
            while index < len(statement):
                if statement[index] == '"':
                    if index + 1 < len(statement) and statement[index + 1] == '"':
                        value.append('"')
                        index += 2
                        continue
                    index += 1
                    break
                value.append(statement[index])
                index += 1
            tokens.append("".join(value).casefold())
            continue
        start = index
        while (
            index < len(statement)
            and not statement[index].isspace()
            and statement[index] not in ",;()'\""
        ):
            index += 1
        tokens.append(statement[start:index].casefold())
    return tokens


def check_workflow_safety(repo_root: Path) -> list[str]:
    workflow_dir = repo_root / ".github" / "workflows"
    paths = sorted({*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")})
    findings: list[str] = []
    protected_env_pattern = re.compile(
        r"^\s*['\"]?(?:" + "|".join(map(re.escape, PROTECTED_SECRET_NAMES)) + r")[\'\"]?\s*:",
        re.IGNORECASE,
    )
    protected_secret_reference_pattern = re.compile(
        r"secrets\s*(?:\.\s*(?:"
        + "|".join(map(re.escape, PROTECTED_SECRET_NAMES))
        + r")\b|\[\s*['\"](?:"
        + "|".join(map(re.escape, PROTECTED_SECRET_NAMES))
        + r")[\'\"]\s*\])",
        re.IGNORECASE,
    )

    for path in paths:
        relative_path = path.relative_to(repo_root)
        text = path.read_text(encoding="utf-8")
        if re.search(r"^\s*pull_request_target\s*:", text, re.MULTILINE):
            findings.append(f"{relative_path}: pull_request_target")
        if _UNSAFE_WORKFLOW_DEPLOY_PATTERN.search(text):
            findings.append(f"{relative_path}: automatic Render deployment")
        for action_reference in _WORKFLOW_ACTION_PATTERN.findall(text):
            if action_reference.startswith("./"):
                continue
            if action_reference.startswith("docker://"):
                if "@sha256:" not in action_reference:
                    findings.append(f"{relative_path}: unpinned Docker action")
                continue
            _action, separator, revision = action_reference.rpartition("@")
            if not separator or _FULL_COMMIT_SHA_PATTERN.fullmatch(revision) is None:
                findings.append(f"{relative_path}: action not pinned to full commit SHA")
        for line in text.splitlines():
            if protected_secret_reference_pattern.search(line):
                findings.append(f"{relative_path}: production secret reference")
                break
            if protected_env_pattern.match(line):
                findings.append(f"{relative_path}: production secret env key")
                break

    render_path = repo_root / "render.yaml"
    if not render_path.is_file():
        findings.append("render.yaml: missing")
    else:
        render_text = render_path.read_text(encoding="utf-8")
        render_services, unsupported_service_syntax = _render_services(render_text)
        worker_services = [
            service for service in render_services if service.name == "kr-trading-worker"
        ]
        if (
            unsupported_service_syntax
            or len(worker_services) != 1
            or worker_services[0].service_type != "worker"
        ):
            findings.append(
                "render.yaml: exactly one canonical kr-trading-worker service is required"
            )
        worker_block = worker_services[0].block if len(worker_services) == 1 else ""
        if not _render_auto_deploy_is_off(worker_block):
            findings.append("render.yaml: worker autoDeployTrigger must remain off")
        if not _render_worker_commands_are_quoted(worker_block):
            findings.append("render.yaml: worker build and start commands must use quoted scalars")
        render_items, unsupported_environment_syntax = _render_environment_items(worker_block)
        if unsupported_environment_syntax:
            findings.append(
                "render.yaml: envVars must be one canonical section of key/value-or-sync items"
            )
        render_env_names = [item.name for item in render_items]
        for key in sorted(
            name for name in set(render_env_names) if render_env_names.count(name) > 1
        ):
            findings.append(f"render.yaml: duplicate env key {key}")
        render_env = {item.name: item.value for item in render_items if item.field == "value"}
        for key, expected in RENDER_NO_LIVE_ENV.items():
            if render_env.get(key, "") != expected:
                findings.append(f"render.yaml: {key} must remain {expected} until hosted approval")
        receiver_items = {item.name: item for item in render_items}
        for key in RENDER_RECEIVER_SYNC_FALSE_NAMES:
            item = receiver_items.get(key)
            if item is None:
                findings.append(f"render.yaml: {key} must be declared on worker")
                continue
            if item.field == "value":
                findings.append(f"render.yaml: {key} must not contain a literal value")
            if item.field != "sync" or item.value != "false":
                findings.append(f"render.yaml: {key} must use sync: false")
        for key in RENDER_FORBIDDEN_DEAD_MAN_NAMES:
            if key in receiver_items:
                findings.append(f"render.yaml: {key} must not be available to main worker")
    findings.extend(_check_gitleaks_ignore(repo_root))
    return findings


def _check_gitleaks_ignore(repo_root: Path) -> list[str]:
    """Allow only finding-specific history exceptions, never broad scan bypasses."""

    path = repo_root / ".gitleaksignore"
    if not path.is_file():
        return []
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    findings = [
        f".gitleaksignore:{index}: fingerprint must be exact"
        for index, line in enumerate(lines, start=1)
        if _GITLEAKS_FINGERPRINT_PATTERN.fullmatch(line) is None
    ]
    findings.extend(
        f".gitleaksignore:{index}: fingerprint is not approved"
        for index, line in enumerate(lines, start=1)
        if _GITLEAKS_FINGERPRINT_PATTERN.fullmatch(line) is not None
        and line not in APPROVED_GITLEAKS_FINGERPRINTS
    )
    if len(lines) != len(set(lines)):
        findings.append(".gitleaksignore: duplicate fingerprint")
    return findings


class _RenderEnvironmentItem(NamedTuple):
    name: str
    field: str
    value: str
    block: str


class _RenderService(NamedTuple):
    service_type: str
    name: str
    block: str


def _render_services(render_text: str) -> tuple[list[_RenderService], bool]:
    """Parse the narrow Render services form whose safety meaning is unambiguous."""

    lines = render_text.splitlines()
    root_property_pattern = re.compile(
        r"^(?P<quote>['\"]?)(?P<name>[A-Za-z][A-Za-z0-9]*)(?P=quote)[ \t]*:"
    )
    root_lines = [
        line
        for line in lines
        if line.strip()
        and not line.lstrip().startswith("#")
        and len(line) == len(line.lstrip(" \t"))
    ]
    root_matches = [root_property_pattern.match(line) for line in root_lines]
    if (
        len(root_lines) != 1
        or any(match is None for match in root_matches)
        or [match.group("name") for match in root_matches if match is not None] != ["services"]
    ):
        return [], True
    heading_candidate_pattern = re.compile(
        r"(?:^|[{,])[ \t]*(?:services|\"services\"|'services')[ \t]*:"
    )
    heading_indexes = [
        index
        for index, line in enumerate(lines)
        if not line.lstrip().startswith("#") and heading_candidate_pattern.search(line) is not None
    ]
    if len(heading_indexes) != 1:
        return [], True
    heading_index = heading_indexes[0]
    if (
        re.fullmatch(
            r"(?:services|\"services\"|'services')[ \t]*:[ \t]*(?:#[^\r\n]*)?",
            lines[heading_index],
        )
        is None
    ):
        return [], True

    section_end = len(lines)
    for index in range(heading_index + 1, len(lines)):
        line = lines[index]
        stripped = line.lstrip(" \t")
        if not stripped or stripped.startswith("#"):
            continue
        if len(line) == len(stripped):
            section_end = index
            break

    service_starts: list[int] = []
    for index in range(heading_index + 1, section_end):
        line = lines[index]
        stripped = line.lstrip(" \t")
        if not stripped or stripped.startswith("#"):
            continue
        indentation = len(line) - len(stripped)
        if indentation == 2:
            if not stripped.startswith("-"):
                return [], True
            service_starts.append(index)
        elif indentation < 2 or not service_starts:
            return [], True
    if not service_starts:
        return [], True

    type_pattern = re.compile(
        r"^ {2}-[ \t]+(?:type|\"type\"|'type')[ \t]*:[ \t]*"
        r"(?P<quote>['\"]?)(?P<value>[A-Za-z][A-Za-z0-9_-]{0,63})(?P=quote)"
        r"[ \t]*(?:#[^\r\n]*)?$"
    )
    direct_property_pattern = re.compile(
        r"^ {4}(?P<quote>['\"]?)(?P<name>[A-Za-z][A-Za-z0-9]*)(?P=quote)"
        r"[ \t]*:"
    )
    name_pattern = re.compile(
        r"^ {4}(?:name|\"name\"|'name')[ \t]*:[ \t]*"
        r"(?P<quote>['\"]?)(?P<value>[A-Za-z0-9][A-Za-z0-9._-]{0,127})(?P=quote)"
        r"[ \t]*(?:#[^\r\n]*)?$"
    )
    services: list[_RenderService] = []
    for position, start in enumerate(service_starts):
        end = service_starts[position + 1] if position + 1 < len(service_starts) else section_end
        type_match = type_pattern.fullmatch(lines[start])
        if type_match is None:
            return services, True
        block_lines = lines[start:end]
        direct_lines = [
            line
            for line in block_lines[1:]
            if line.strip()
            and not line.lstrip().startswith("#")
            and len(line) - len(line.lstrip(" \t")) == 4
        ]
        direct_matches = [direct_property_pattern.match(line) for line in direct_lines]
        if any(match is None for match in direct_matches):
            return services, True
        direct_names = [match.group("name") for match in direct_matches if match is not None]
        if (
            len(direct_names) != len(set(direct_names))
            or not set(direct_names) <= _RENDER_CANONICAL_SERVICE_FIELDS
            or direct_names.count("name") != 1
        ):
            return services, True
        if any(
            not _render_service_property_is_canonical(name, line)
            for name, line in zip(direct_names, direct_lines, strict=True)
        ):
            return services, True
        name_line = direct_lines[direct_names.index("name")]
        name_match = name_pattern.fullmatch(name_line)
        if name_match is None:
            return services, True
        services.append(
            _RenderService(
                service_type=type_match.group("value"),
                name=name_match.group("value"),
                block="\n".join(block_lines),
            )
        )
    return services, False


def _render_service_property_is_canonical(name: str, line: str) -> bool:
    field = rf"(?:{re.escape(name)}|\"{re.escape(name)}\"|'{re.escape(name)}')"
    comment = r"[ \t]*(?:#[^\r\n]*)?$"
    if name == "envVars":
        return re.fullmatch(rf"^ {{4}}{field}[ \t]*:{comment}", line) is not None
    if name in {"buildCommand", "startCommand"}:
        scalar = r"(?P<quote>['\"])(?P<value>[^'\"\r\n]+)(?P=quote)"
    elif name == "autoDeployTrigger":
        scalar = r"(?P<quote>['\"]?)(?P<value>on|off)(?P=quote)"
    elif name in {"maxShutdownDelaySeconds", "numInstances"}:
        scalar = r"(?:0|[1-9][0-9]{0,9})"
    else:
        scalar = (
            r"(?P<quote>['\"]?)"
            r"(?P<value>[A-Za-z0-9][A-Za-z0-9._/-]{0,255})"
            r"(?P=quote)"
        )
    return (
        re.fullmatch(
            rf"^ {{4}}{field}[ \t]*:[ \t]*{scalar}{comment}",
            line,
        )
        is not None
    )


def _render_environment_items(
    render_text: str,
) -> tuple[list[_RenderEnvironmentItem], bool]:
    """Parse the narrow Render envVars form whose safety meaning is unambiguous."""

    lines = render_text.splitlines()
    heading_candidate_pattern = re.compile(
        r"(?:^|[{,])[ \t]*(?:envVars|\"envVars\"|'envVars')[ \t]*:"
    )
    heading_pattern = re.compile(
        r"^ {4}(?:envVars|\"envVars\"|'envVars')[ \t]*:[ \t]*"
        r"(?:#[^\r\n]*)?$"
    )
    heading_indexes = [
        index
        for index, line in enumerate(lines)
        if not line.lstrip().startswith("#") and heading_candidate_pattern.search(line) is not None
    ]
    if len(heading_indexes) != 1:
        return [], True
    heading_index = heading_indexes[0]
    if heading_pattern.fullmatch(lines[heading_index]) is None:
        return [], True
    if any(
        re.match(r"^ {4}(?:<<|\"<<\"|'<<')[ \t]*:", line) is not None
        for line in lines
        if not line.lstrip().startswith("#")
    ):
        return [], True

    section_end = len(lines)
    for index in range(heading_index + 1, len(lines)):
        line = lines[index]
        stripped = line.lstrip(" \t")
        if not stripped or stripped.startswith("#"):
            continue
        if len(line) - len(stripped) <= 4:
            section_end = index
            break

    key_pattern = re.compile(
        r"^ {6}-[ \t]+(?:key|\"key\"|'key')[ \t]*:[ \t]*"
        r"(?P<quote>['\"]?)(?P<name>[A-Z][A-Z0-9_]*)(?P=quote)"
        r"[ \t]*(?:#[^\r\n]*)?$"
    )
    property_pattern = re.compile(
        r"^ {8}(?P<field>value|sync)[ \t]*:[ \t]*"
        r"(?P<scalar>\"[^\"\r\n]*\"|'[^'\r\n]*'|"
        r"[A-Za-z0-9_./+=:@?%&$~!#-]+)"
        r"[ \t]*(?:#[^\r\n]*)?$"
    )
    items: list[_RenderEnvironmentItem] = []
    index = heading_index + 1
    while index < section_end:
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        key_match = key_pattern.fullmatch(line)
        if key_match is None:
            return items, True
        item_start = index
        index += 1
        properties: list[re.Match[str]] = []
        while index < section_end:
            candidate = lines[index]
            if not candidate.strip() or candidate.lstrip().startswith("#"):
                index += 1
                continue
            if key_pattern.fullmatch(candidate) is not None:
                break
            property_match = property_pattern.fullmatch(candidate)
            if property_match is None:
                return items, True
            properties.append(property_match)
            index += 1
        if len(properties) != 1:
            return items, True
        field = properties[0].group("field")
        scalar = properties[0].group("scalar")
        if field == "sync" and scalar not in {"true", "false"}:
            return items, True
        value = scalar[1:-1] if scalar[:1] in {"'", '"'} else scalar
        items.append(
            _RenderEnvironmentItem(
                name=key_match.group("name"),
                field=field,
                value=value,
                block="\n".join(lines[item_start + 1 : index]),
            )
        )
    if not items:
        return [], True
    return items, False


def _render_environment_blocks(render_text: str) -> dict[str, str]:
    items, _unsupported = _render_environment_items(render_text)
    return {item.name: item.block for item in items}


def _render_auto_deploy_is_off(worker_block: str) -> bool:
    candidates = [
        line
        for line in worker_block.splitlines()
        if not line.lstrip().startswith("#")
        and re.search(
            r"(?:^|[{,])[ \t]*(?:autoDeployTrigger|\"autoDeployTrigger\"|"
            r"'autoDeployTrigger')[ \t]*:",
            line,
        )
        is not None
    ]
    if len(candidates) != 1:
        return False
    match = re.fullmatch(
        r"^ {4}(?:autoDeployTrigger|\"autoDeployTrigger\"|'autoDeployTrigger')"
        r"[ \t]*:[ \t]*(?P<quote>['\"]?)(?P<value>on|off)(?P=quote)"
        r"[ \t]*(?:#[^\r\n]*)?$",
        candidates[0],
    )
    return match is not None and match.group("value") == "off"


def _render_worker_commands_are_quoted(worker_block: str) -> bool:
    for field in ("buildCommand", "startCommand"):
        candidates = [
            line
            for line in worker_block.splitlines()
            if not line.lstrip().startswith("#")
            and re.match(
                rf"^ {{4}}(?:{field}|\"{field}\"|'{field}')[ \t]*:",
                line,
            )
            is not None
        ]
        if len(candidates) != 1:
            return False
        if (
            re.fullmatch(
                rf"^ {{4}}(?:{field}|\"{field}\"|'{field}')[ \t]*:[ \t]*"
                r"(?P<quote>['\"])(?P<value>[^'\"\r\n]+)(?P=quote)"
                r"[ \t]*(?:#[^\r\n]*)?$",
                candidates[0],
            )
            is None
        ):
            return False
    return True


def _render_service_blocks(render_text: str, service_name: str) -> list[str]:
    services, unsupported = _render_services(render_text)
    if unsupported:
        return []
    return [service.block for service in services if service.name == service_name]


def _render_service_block(render_text: str, service_name: str) -> str | None:
    blocks = _render_service_blocks(render_text, service_name)
    return blocks[0] if len(blocks) == 1 else None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check repository safety invariants.")
    parser.add_argument("scope", choices=("all", "migrations", "workflows"))
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    findings: list[str] = []
    if args.scope in {"all", "migrations"}:
        findings.extend(check_migration_safety(repo_root))
    if args.scope in {"all", "workflows"}:
        findings.extend(check_workflow_safety(repo_root))
    if findings:
        print("::error::Repository safety policy violation.")
        print("\n".join(sorted(set(findings))))
        return 1
    print(f"Repository safety checks passed: {args.scope}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
