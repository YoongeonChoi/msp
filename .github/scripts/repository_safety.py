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

_SQL_COMMENT_PATTERN = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_PUBLIC_TABLE_PATTERN = re.compile(
    r"\bcreate\s+(?:unlogged\s+)?table\s+(?:if\s+not\s+exists\s+)?"
    r'(?:public|"public")\s*\.\s*"?([a-z_][a-z0-9_]*)"?',
    re.IGNORECASE,
)
_RLS_PATTERN = re.compile(
    r"\balter\s+table\s+(?:if\s+exists\s+)?"
    r'(?:public|"public")\s*\.\s*"?([a-z_][a-z0-9_]*)"?'
    r"\s+enable\s+row\s+level\s+security",
    re.IGNORECASE,
)
_RLS_DISABLE_PATTERN = re.compile(
    r"\balter\s+table\s+(?:if\s+exists\s+)?"
    r'(?:public|"public")\s*\.\s*"?([a-z_][a-z0-9_]*)"?'
    r"\s+disable\s+row\s+level\s+security",
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


def check_migration_safety(repo_root: Path) -> list[str]:
    migration_dir = repo_root / "supabase" / "migrations"
    paths = sorted(migration_dir.glob("*.sql"))
    if not paths:
        return ["supabase/migrations: no SQL migrations found"]

    sql = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    normalized = _SQL_COMMENT_PATTERN.sub(" ", sql)
    public_tables = set(_PUBLIC_TABLE_PATTERN.findall(normalized))
    rls_tables = set(_RLS_PATTERN.findall(normalized))
    findings = [
        f"public table missing RLS: {table}"
        for table in sorted(public_tables - rls_tables)
    ]
    findings.extend(
        f"public table disables RLS: {table}"
        for table in sorted(set(_RLS_DISABLE_PATTERN.findall(normalized)))
    )

    for statement in _POLICY_PATTERN.findall(normalized):
        if _policy_grants_anon_or_public_write(statement):
            compact = " ".join(statement.split())[:160]
            findings.append(f"anon/public write-capable policy: {compact}")
    return findings


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
    return findings


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
