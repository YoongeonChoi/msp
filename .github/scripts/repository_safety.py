from __future__ import annotations

import argparse
import re
from collections.abc import Sequence
from pathlib import Path

PROTECTED_SECRET_NAMES = (
    "SUPABASE_SECRET_KEY",
    "TOSS_CLIENT_SECRET",
    "OPENAI_API_KEY",
    "NAVER_CLIENT_SECRET",
    "KRX_API_KEY",
    "OPENDART_API_KEY",
    "ALERT_WEBHOOK_URL",
    "RENDER_DEPLOY_HOOK_URL",
    "SUPABASE_LIVE_REQUESTER_JWT",
    "SUPABASE_LIVE_REVIEWER_JWT",
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
    }
)

RENDER_NO_LIVE_ENV = {
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
    }
)


def _qualified_objects(
    pattern: re.Pattern[str], sql: str
) -> set[tuple[str, str]]:
    return {
        (
            match.group("schema").strip('"').casefold(),
            match.group("table").casefold(),
        )
        for match in pattern.finditer(sql)
    }


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
    findings = [
        f"exposed table missing RLS: {schema}.{table}"
        for schema, table in sorted(exposed_tables - rls_tables)
    ]
    findings.extend(
        f"{path.relative_to(repo_root).as_posix()}: destructive migration "
        "missing rollback note or approval"
        for path, text in migration_sql
        if _contains_destructive_migration_sql(text)
        and not _has_same_file_destructive_approval(text)
    )
    findings.extend(
        f"exposed table disables RLS: {schema}.{table}"
        for schema, table in sorted(
            _qualified_objects(_RLS_DISABLE_PATTERN, normalized)
        )
    )

    for match in _API_VIEW_PATTERN.finditer(normalized):
        options = " ".join(match.group("options").casefold().split())
        if re.search(r"\bsecurity_invoker\s*=\s*true\b", options) is None:
            findings.append(f"api view missing security_invoker=true: {match.group(1)}")

    for match, header in _function_headers(_EXPOSED_FUNCTION_PATTERN, normalized):
        schema = match.group("schema").strip('"').casefold()
        name = match.group("name").casefold()
        if re.search(r"\bsecurity\s+invoker\b", header, re.IGNORECASE) is None:
            findings.append(
                f"exposed function missing explicit SECURITY INVOKER: {schema}.{name}"
            )
        if re.search(r"\bsecurity\s+definer\b", header, re.IGNORECASE):
            findings.append(f"exposed function uses SECURITY DEFINER: {schema}.{name}")

    for match, header in _function_headers(_PRIVATE_FUNCTION_PATTERN, normalized):
        if re.search(r"\bsecurity\s+definer\b", header, re.IGNORECASE) and re.search(
            r"\bset\s+search_path\s*=\s*''", header, re.IGNORECASE
        ) is None:
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
            token == "drop"
            and index + 1 < len(tokens)
            and tokens[index + 1] in {"table", "column"}
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


def _function_headers(
    pattern: re.Pattern[str], sql: str
) -> list[tuple[re.Match[str], str]]:
    """Return declarations up to the dollar-quoted function body."""

    headers: list[tuple[re.Match[str], str]] = []
    next_function_pattern = re.compile(
        r"\bcreate\s+(?:or\s+replace\s+)?function\b", re.IGNORECASE
    )
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
        r"^\s*(?:" + "|".join(map(re.escape, PROTECTED_SECRET_NAMES)) + r")\s*:",
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
        if not re.search(
            r"^\s*autoDeployTrigger\s*:\s*[\"']?off[\"']?\s*$",
            render_text,
            re.IGNORECASE | re.MULTILINE,
        ):
            findings.append("render.yaml: autoDeployTrigger must remain off")
        render_env = _render_environment_values(render_text)
        for key, expected in RENDER_NO_LIVE_ENV.items():
            if render_env.get(key, "").casefold() != expected:
                findings.append(
                    f"render.yaml: {key} must remain {expected} until hosted approval"
                )
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


def _render_environment_values(render_text: str) -> dict[str, str]:
    """Read literal Render env values without treating YAML as executable input."""

    values: dict[str, str] = {}
    matches = list(
        re.finditer(
            r"^\s*-\s+key:\s*([A-Z][A-Z0-9_]*)\s*$",
            render_text,
            re.MULTILINE,
        )
    )
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(render_text)
        block = render_text[match.end() : end]
        value_match = re.search(
            r"^\s+value:\s*['\"]?([^'\"#\r\n]+?)['\"]?\s*$",
            block,
            re.MULTILINE,
        )
        if value_match is not None:
            values[match.group(1)] = value_match.group(1).strip()
    return values


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
