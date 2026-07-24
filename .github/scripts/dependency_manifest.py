from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import re
import subprocess
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

MANIFEST_SCHEMA = "kr-auto-trading-lab/dependency-lock-manifest/v1"
PROVENANCE_SCHEMA = "kr-auto-trading-lab/dependency-provenance/v1"
MANIFEST_PATH = PurePosixPath(".github/dependency-lock-manifest.v1.json")
GENERATOR_PATH = PurePosixPath(".github/scripts/dependency_manifest.py")
WORKER_PROJECT_PATH = PurePosixPath("apps/worker/pyproject.toml")
WORKER_REQUIREMENTS_PATH = PurePosixPath("apps/worker/requirements.txt")
WORKER_LOCK_PATH = PurePosixPath("apps/worker/requirements.lock")
SECURITY_LOCK_PATH = PurePosixPath(".github/security-tools.lock")
CARGO_MANIFEST_PATH = PurePosixPath("apps/desktop/src-tauri/Cargo.toml")
CARGO_LOCK_PATH = PurePosixPath("apps/desktop/src-tauri/Cargo.lock")
NPM_REGISTRY = "https://registry.npmjs.org"
CARGO_REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"

_FULL_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_HEX_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PACKAGE_NAME_PATTERN = re.compile(
    r"^(?:@[a-z0-9][a-z0-9._~-]*/[a-z0-9._~-]+|"
    r"[a-z0-9][a-z0-9._~-]*)$"
)
_SAFE_RELATIVE_PATH_PATTERN = re.compile(
    r"^[A-Za-z0-9._@+~-]+(?:/[A-Za-z0-9._@+~-]+)*$"
)
_WINDOWS_RESERVED_PATH_STEMS = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
_NPM_VERSION_PATTERN = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_NPM_DECLARATION_FIELDS = (
    "dependencies",
    "devDependencies",
    "optionalDependencies",
    "peerDependencies",
    "peerDependenciesMeta",
)
_NPM_ALIAS_PATTERN = re.compile(
    r"^npm:(?P<name>(?:@[a-z0-9][a-z0-9._~-]*/[a-z0-9._~-]+|"
    r"[a-z0-9][a-z0-9._~-]*))"
    r"(?:@(?P<selector>[!-~](?:[ -~]*[!-~])?))?$"
)
_PYTHON_VERSION_TOKEN = r"[0-9]+(?:\.[0-9]+)*"
_PYTHON_REQUIREMENT_PATTERN = re.compile(
    rf"^(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)=="
    rf"(?P<version>{_PYTHON_VERSION_TOKEN})"
    rf"(?:\s*;\s*(?P<marker>.+))?$"
)
_PYTHON_DECLARATION_PATTERN = re.compile(
    rf"^(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)"
    rf"(?P<operator>===|==|~=|!=|<=|>=|<|>)"
    rf"(?P<version>{_PYTHON_VERSION_TOKEN})"
    rf"(?:\s*;\s*(?P<marker>.+))?$"
)
_PYTHON_HASH_PATTERN = re.compile(r"--hash=sha256:([0-9a-f]{64})")
_MARKER_VARIABLE = (
    r"(?:implementation_name|implementation_version|os_name|platform_machine|"
    r"platform_python_implementation|platform_release|platform_system|"
    r"platform_version|python_full_version|python_version|sys_platform|extra)"
)
_MARKER_ATOM_PATTERN = re.compile(
    rf"(?P<variable>{_MARKER_VARIABLE})\s*"
    rf"(?P<operator>not\s+in|===|==|!=|<=|>=|~=|<|>|in)\s*"
    rf"(?P<quote>[\"'])(?P<value>[A-Za-z0-9][A-Za-z0-9._+!*-]*)"
    rf"(?P=quote)"
)
_MARKER_CONNECTOR_PATTERN = re.compile(r"\s+(?P<connector>and|or)\s+")
_CARGO_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class ManifestError(ValueError):
    """A safe, field-level dependency manifest validation error."""


def _safe_relative_path(value: str | PurePosixPath) -> PurePosixPath:
    raw = value if isinstance(value, str) else value.as_posix()
    parts = raw.split("/")
    path = PurePosixPath(raw)
    if (
        _SAFE_RELATIVE_PATH_PATTERN.fullmatch(raw) is None
        or path.is_absolute()
        or path.as_posix() != raw
        or any(part in {"", ".", ".."} for part in parts)
        or any(part.endswith(".") for part in parts)
        or any(
            part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_PATH_STEMS
            for part in parts
        )
    ):
        raise ManifestError("repository input path is not a fixed safe relative path")
    return path


def _path(repo_root: Path, relative_path: PurePosixPath) -> Path:
    safe_path = _safe_relative_path(relative_path)
    root = repo_root.resolve()
    if not root.is_dir():
        raise ManifestError("repository root is not a directory")
    candidate = root
    for part in safe_path.parts:
        candidate /= part
        if candidate.is_symlink() or candidate.is_junction():
            raise ManifestError("repository input path contains a symbolic link or junction")
    if not candidate.resolve().is_relative_to(root):
        raise ManifestError("repository input path escapes the repository root")
    return candidate


def _normalized_text_bytes(repo_root: Path, relative_path: PurePosixPath) -> bytes:
    path = _path(repo_root, relative_path)
    if not path.is_file() or path.is_symlink():
        raise ManifestError(f"{relative_path.as_posix()}: required regular file is missing")
    return _normalize_utf8_text(path.read_bytes(), relative_path.as_posix())


def _normalize_utf8_text(payload: bytes, source: str) -> bytes:
    try:
        text = payload.decode("utf-8")
    except UnicodeError as exc:
        raise ManifestError(f"{source}: required file is not UTF-8 text") from exc
    text = text.replace("\r\n", "\n")
    if "\r" in text:
        raise ManifestError(f"{source}: bare carriage return is forbidden")
    return text.encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_object(repo_root: Path, relative_path: PurePosixPath) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ManifestError(f"{relative_path.as_posix()}: duplicate JSON key")
            value[key] = item
        return value

    def reject_constant(_value: str) -> None:
        raise ManifestError(f"{relative_path.as_posix()}: non-finite JSON number")

    try:
        value = json.loads(
            _normalized_text_bytes(repo_root, relative_path),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{relative_path.as_posix()}: malformed JSON") from exc
    if not isinstance(value, dict):
        raise ManifestError(f"{relative_path.as_posix()}: JSON root must be an object")
    return value


def _toml_object(repo_root: Path, relative_path: PurePosixPath) -> dict[str, Any]:
    try:
        value = tomllib.loads(
            _normalized_text_bytes(repo_root, relative_path).decode("utf-8")
        )
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"{relative_path.as_posix()}: malformed TOML") from exc
    if not isinstance(value, dict):
        raise ManifestError(f"{relative_path.as_posix()}: TOML root must be a table")
    return value


def _string_field(
    value: Mapping[str, Any],
    field: str,
    source: str,
) -> str:
    result = value.get(field)
    if (
        not isinstance(result, str)
        or not result
        or any(character.isspace() for character in result)
    ):
        raise ManifestError(f"{source}: {field} must be a non-empty token")
    return result


def _normalize_python_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).casefold()


def _valid_npm_name(name: str) -> bool:
    if len(name) > 214 or _PACKAGE_NAME_PATTERN.fullmatch(name) is None:
        return False
    package_tail = name.rsplit("/", 1)[-1]
    return package_tail not in {".", ".."}


def _valid_npm_version(version: str) -> bool:
    match = _NPM_VERSION_PATTERN.fullmatch(version)
    if match is None:
        return False
    prerelease = match.group("prerelease")
    if prerelease is None:
        return True
    return all(
        not (identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0"))
        for identifier in prerelease.split(".")
    )


def _npm_declaration_metadata(
    value: Mapping[str, Any],
    source: str,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for field in _NPM_DECLARATION_FIELDS:
        if field not in value:
            continue
        table = value[field]
        if not isinstance(table, dict):
            raise ManifestError(f"{source}: {field} must be an object")
        normalized: dict[str, Any] = {}
        for raw_name, declaration in table.items():
            if (
                not isinstance(raw_name, str)
                or not _valid_npm_name(raw_name)
            ):
                raise ManifestError(f"{source}: invalid {field} package name")
            if field == "peerDependenciesMeta":
                if (
                    not isinstance(declaration, dict)
                    or not set(declaration).issubset({"optional"})
                    or (
                        "optional" in declaration
                        and not isinstance(declaration["optional"], bool)
                    )
                ):
                    raise ManifestError(f"{source}: invalid peer dependency metadata")
            elif (
                not isinstance(declaration, str)
                or not declaration
                or any(character in declaration for character in "\x00\r\n")
            ):
                raise ManifestError(f"{source}: invalid {field} declaration")
            normalized[raw_name] = declaration
        metadata[field] = normalized
    return metadata


def _npm_alias_pairs(metadata: Mapping[str, Any]) -> set[tuple[str, str]]:
    aliases: set[tuple[str, str]] = set()
    for field in _NPM_DECLARATION_FIELDS[:-1]:
        table = metadata.get(field, {})
        if not isinstance(table, dict):
            raise ManifestError("package-lock.json: invalid dependency metadata")
        for installed_name, declaration in table.items():
            if not isinstance(declaration, str) or not declaration.startswith("npm:"):
                continue
            match = _NPM_ALIAS_PATTERN.fullmatch(declaration)
            if match is None or not _valid_npm_name(match.group("name")):
                raise ManifestError("package-lock.json: invalid npm alias declaration")
            aliases.add((installed_name, match.group("name")))
    return aliases


def _npm_locator_name(lock_path: str, workspace_paths: set[str]) -> str:
    safe_path = _safe_relative_path(lock_path)
    parts = safe_path.parts
    remaining = parts
    workspace_prefixes = sorted(
        (PurePosixPath(path).parts for path in workspace_paths),
        key=len,
        reverse=True,
    )
    for prefix in workspace_prefixes:
        if (
            len(parts) > len(prefix)
            and parts[: len(prefix)] == prefix
            and parts[len(prefix)] == "node_modules"
        ):
            remaining = parts[len(prefix) :]
            break
    if not remaining or remaining[0] != "node_modules":
        raise ManifestError("package-lock.json: package locator is not workspace anchored")

    index = 0
    installed_name = ""
    while index < len(remaining):
        if remaining[index] != "node_modules":
            raise ManifestError("package-lock.json: package locator is malformed")
        index += 1
        if index >= len(remaining):
            raise ManifestError("package-lock.json: package locator is malformed")
        segment = remaining[index]
        if segment.startswith("@"):
            if index + 1 >= len(remaining):
                raise ManifestError("package-lock.json: scoped package locator is incomplete")
            installed_name = f"{segment}/{remaining[index + 1]}"
            index += 2
        else:
            installed_name = segment
            index += 1
        if not _valid_npm_name(installed_name):
            raise ManifestError("package-lock.json: invalid installed package identity")
        if index < len(remaining) and remaining[index] != "node_modules":
            raise ManifestError("package-lock.json: package locator is malformed")
    return installed_name


def _validate_npm_integrity(integrity: str) -> None:
    if not integrity.startswith("sha512-"):
        raise ManifestError("package-lock.json: registry package integrity must use sha512")
    encoded = integrity.removeprefix("sha512-")
    try:
        digest = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ManifestError("package-lock.json: malformed registry package integrity") from exc
    if len(digest) != 64:
        raise ManifestError("package-lock.json: malformed registry package integrity")


def _validate_npm_resolved(resolved: str, name: str, version: str) -> None:
    package_tail = name.rsplit("/", 1)[-1]
    expected = f"{NPM_REGISTRY}/{name}/-/{package_tail}-{version}.tgz"
    if resolved != expected:
        raise ManifestError("package-lock.json: registry artifact identity mismatch")


def _npm_inventory(
    repo_root: Path,
) -> tuple[list[dict[str, Any]], list[PurePosixPath]]:
    package_path = PurePosixPath("package.json")
    lock_path = PurePosixPath("package-lock.json")
    package = _json_object(repo_root, package_path)
    lock = _json_object(repo_root, lock_path)
    if lock.get("lockfileVersion") != 3:
        raise ManifestError("package-lock.json: only lockfileVersion 3 is supported")
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        raise ManifestError("package-lock.json: packages must be an object")

    root_name = _string_field(package, "name", "package.json")
    root_version = _string_field(package, "version", "package.json")
    if lock.get("name") != root_name or lock.get("version") != root_version:
        raise ManifestError("package-lock.json: root name or version differs from package.json")

    workspaces = package.get("workspaces")
    if not isinstance(workspaces, list) or not workspaces:
        raise ManifestError("package.json: workspaces must be a non-empty fixed path list")
    root_metadata = _npm_declaration_metadata(package, "package.json")
    workspace_packages: dict[str, tuple[str, str, dict[str, Any]]] = {}
    workspace_names = {root_name}
    input_paths = [package_path, lock_path]
    for workspace in workspaces:
        if not isinstance(workspace, str):
            raise ManifestError("package.json: workspace path must be a string")
        safe_workspace = _safe_relative_path(workspace)
        workspace_package_path = safe_workspace / "package.json"
        workspace_package = _json_object(repo_root, workspace_package_path)
        name = _string_field(
            workspace_package,
            "name",
            workspace_package_path.as_posix(),
        )
        version = _string_field(
            workspace_package,
            "version",
            workspace_package_path.as_posix(),
        )
        metadata = _npm_declaration_metadata(
            workspace_package,
            workspace_package_path.as_posix(),
        )
        if safe_workspace.as_posix() in workspace_packages:
            raise ManifestError("package.json: duplicate workspace path")
        if name in workspace_names:
            raise ManifestError("package.json: duplicate workspace package name")
        workspace_names.add(name)
        workspace_packages[safe_workspace.as_posix()] = (name, version, metadata)
        input_paths.append(workspace_package_path)

    local_packages = {"": (root_name, root_version, ".", root_metadata)}
    local_packages.update(
        {
            workspace: (name, version, workspace, metadata)
            for workspace, (name, version, metadata) in workspace_packages.items()
        }
    )
    descriptor_metadata: dict[str, dict[str, Any]] = {}
    alias_pairs: set[tuple[str, str]] = set()
    for raw_lock_path, raw_entry in packages.items():
        if not isinstance(raw_lock_path, str) or not isinstance(raw_entry, dict):
            raise ManifestError("package-lock.json: package entry is malformed")
        if raw_lock_path:
            _safe_relative_path(raw_lock_path)
        metadata = _npm_declaration_metadata(raw_entry, "package-lock.json")
        descriptor_metadata[raw_lock_path] = metadata
        alias_pairs.update(_npm_alias_pairs(metadata))
    if not set(local_packages).issubset(packages):
        raise ManifestError("package-lock.json: local package descriptor is missing")

    components: list[dict[str, Any]] = []
    observed_links: dict[str, str] = {}
    for raw_lock_path, raw_entry in packages.items():
        if raw_entry.get("link") is True:
            resolved = raw_entry.get("resolved")
            if set(raw_entry) != {"link", "resolved"}:
                raise ManifestError("package-lock.json: workspace link descriptor is malformed")
            if not isinstance(resolved, str):
                raise ManifestError("package-lock.json: workspace link target is invalid")
            safe_resolved = _safe_relative_path(resolved).as_posix()
            if safe_resolved != resolved or resolved not in workspace_packages:
                raise ManifestError("package-lock.json: workspace link target is invalid")
            expected_name, expected_version, _metadata = workspace_packages[resolved]
            if _npm_locator_name(raw_lock_path, set(workspace_packages)) != expected_name:
                raise ManifestError("package-lock.json: workspace link identity mismatch")
            if resolved in observed_links:
                raise ManifestError("package-lock.json: duplicate workspace link")
            observed_links[resolved] = raw_lock_path
            components.append(
                {
                    "ecosystem": "npm",
                    "kind": "workspace-link",
                    "lock_path": raw_lock_path,
                    "name": expected_name,
                    "resolved": resolved,
                    "version": expected_version,
                    "workspace": resolved,
                }
            )
            continue

        if raw_lock_path in local_packages:
            expected_name, expected_version, workspace, expected_metadata = local_packages[
                raw_lock_path
            ]
            if (
                raw_entry.get("name") != expected_name
                or raw_entry.get("version") != expected_version
            ):
                raise ManifestError("package-lock.json: workspace package metadata mismatch")
            if descriptor_metadata[raw_lock_path] != expected_metadata:
                raise ManifestError("package-lock.json: dependency declarations differ")
            if any(field in raw_entry for field in ("resolved", "integrity")):
                raise ManifestError("package-lock.json: workspace package has registry fields")
            components.append(
                {
                    "ecosystem": "npm",
                    "kind": "workspace",
                    "lock_path": raw_lock_path,
                    "name": expected_name,
                    "version": expected_version,
                    "workspace": workspace,
                }
            )
            continue

        if "link" in raw_entry:
            raise ManifestError("package-lock.json: registry package has invalid link metadata")
        installed_name = _npm_locator_name(raw_lock_path, set(workspace_packages))
        version = _string_field(raw_entry, "version", "package-lock.json")
        if not _valid_npm_version(version):
            raise ManifestError("package-lock.json: registry package version is not SemVer")
        declared_name = raw_entry.get("name")
        alias: str | None = None
        if declared_name is None:
            name = installed_name
        else:
            if (
                not isinstance(declared_name, str)
                or not _valid_npm_name(declared_name)
                or declared_name == installed_name
                or (installed_name, declared_name) not in alias_pairs
            ):
                raise ManifestError("package-lock.json: unproven npm alias identity")
            name = declared_name
            alias = installed_name
        resolved = _string_field(raw_entry, "resolved", "package-lock.json")
        integrity = _string_field(raw_entry, "integrity", "package-lock.json")
        _validate_npm_resolved(resolved, name, version)
        _validate_npm_integrity(integrity)
        component: dict[str, Any] = {
            "ecosystem": "npm",
            "integrity": integrity,
            "kind": "registry",
            "lock_path": raw_lock_path,
            "name": name,
            "resolved": resolved,
            "version": version,
        }
        if alias is not None:
            component["alias"] = alias
        if "optional" in raw_entry and not isinstance(raw_entry["optional"], bool):
            raise ManifestError("package-lock.json: optional flag must be boolean")
        if raw_entry.get("optional") is True:
            component["optional"] = True
        components.append(component)

    if set(observed_links) != set(workspace_packages):
        raise ManifestError("package-lock.json: workspace links are incomplete")
    return components, input_paths


def _logical_requirement_records(text: str, source: str) -> list[str]:
    records: list[str] = []
    buffer = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            if buffer:
                raise ManifestError(f"{source}: requirement continuation is interrupted")
            continue
        if "#" in line:
            raise ManifestError(f"{source}: inline comments are not allowed")
        continued = line.endswith("\\")
        fragment = line[:-1].rstrip() if continued else line
        buffer = f"{buffer} {fragment}".strip()
        if not continued:
            records.append(buffer)
            buffer = ""
    if buffer:
        raise ManifestError(f"{source}: unfinished requirement continuation")
    return records


def _normalize_python_marker(marker: str, source: str) -> str:
    parts: list[str] = []
    position = 0
    while position < len(marker):
        atom = _MARKER_ATOM_PATTERN.match(marker, position)
        if atom is None:
            raise ManifestError(f"{source}: environment marker is outside the safe subset")
        operator = " ".join(atom.group("operator").split())
        value = json.dumps(atom.group("value"), ensure_ascii=True)
        parts.append(f"{atom.group('variable')} {operator} {value}")
        position = atom.end()
        if position == len(marker):
            break
        connector = _MARKER_CONNECTOR_PATTERN.match(marker, position)
        if connector is None:
            raise ManifestError(f"{source}: environment marker is outside the safe subset")
        parts.append(connector.group("connector"))
        position = connector.end()
    if not parts:
        raise ManifestError(f"{source}: environment marker is outside the safe subset")
    return " ".join(parts)


def _parse_requirement_record(record: str, source: str) -> dict[str, Any]:
    hash_start = record.find(" --hash=")
    if hash_start < 0:
        raise ManifestError(f"{source}: every requirement needs a SHA-256 hash")
    requirement = record[:hash_start]
    hash_text = record[hash_start:].strip()
    match = _PYTHON_REQUIREMENT_PATTERN.fullmatch(requirement)
    if match is None:
        raise ManifestError(f"{source}: requirement must use exact name==version syntax")
    tokens = hash_text.split()
    hashes: list[str] = []
    for token in tokens:
        hash_match = _PYTHON_HASH_PATTERN.fullmatch(token)
        if hash_match is None:
            raise ManifestError(f"{source}: unsupported requirement option")
        hashes.append(hash_match.group(1))
    if not hashes or len(hashes) != len(set(hashes)):
        raise ManifestError(f"{source}: hashes must be present and unique")
    marker = match.group("marker")
    normalized_marker: str | None = None
    if marker is not None:
        normalized_marker = _normalize_python_marker(marker, source)
    result: dict[str, Any] = {
        "hashes": sorted(hashes),
        "name": _normalize_python_name(match.group("name")),
        "version": match.group("version"),
    }
    if normalized_marker is not None:
        result["marker"] = normalized_marker
    return result


def _python_lock_inventory(
    repo_root: Path,
    relative_path: PurePosixPath,
    scope: str,
) -> list[dict[str, Any]]:
    text = _normalized_text_bytes(repo_root, relative_path).decode("utf-8")
    components: list[dict[str, Any]] = []
    observed_names: set[str] = set()
    for record in _logical_requirement_records(text, relative_path.as_posix()):
        parsed = _parse_requirement_record(record, relative_path.as_posix())
        name = parsed["name"]
        if name in observed_names:
            raise ManifestError(f"{relative_path.as_posix()}: duplicate normalized package")
        observed_names.add(name)
        component = {
            "ecosystem": "pypi",
            "kind": "hashed-requirement",
            "lock_path": relative_path.as_posix(),
            "scope": scope,
            **parsed,
        }
        components.append(component)
    if not components:
        raise ManifestError(f"{relative_path.as_posix()}: lock is empty")
    return components


def _parse_declared_python_requirement(
    declaration: str,
    source: str,
) -> tuple[str, str, str | None]:
    match = _PYTHON_DECLARATION_PATTERN.fullmatch(declaration)
    if match is None:
        raise ManifestError(f"{source}: unsupported direct dependency declaration")
    marker = match.group("marker")
    normalized_marker = (
        _normalize_python_marker(marker, source) if marker is not None else None
    )
    return (
        _normalize_python_name(match.group("name")),
        f"{match.group('operator')}{match.group('version')}",
        normalized_marker,
    )


def _declared_python_requirements(
    project: Mapping[str, Any],
) -> dict[str, tuple[str, str | None]]:
    project_table = project.get("project")
    if not isinstance(project_table, dict):
        raise ManifestError("apps/worker/pyproject.toml: project table is missing")
    _string_field(project_table, "name", "apps/worker/pyproject.toml")
    _string_field(project_table, "version", "apps/worker/pyproject.toml")
    dependencies = project_table.get("dependencies")
    if not isinstance(dependencies, list):
        raise ManifestError("apps/worker/pyproject.toml: dependencies must be a list")
    declarations: dict[str, tuple[str, str | None]] = {}
    for dependency in dependencies:
        if not isinstance(dependency, str):
            raise ManifestError("apps/worker/pyproject.toml: dependency must be a string")
        name, specifier, marker = _parse_declared_python_requirement(
            dependency,
            "apps/worker/pyproject.toml",
        )
        if name in declarations:
            raise ManifestError("apps/worker/pyproject.toml: duplicate direct dependency")
        declarations[name] = (specifier, marker)
    return declarations


def _requirements_python_declarations(
    repo_root: Path,
) -> dict[str, tuple[str, str | None]]:
    text = _normalized_text_bytes(repo_root, WORKER_REQUIREMENTS_PATH).decode("utf-8")
    declarations: dict[str, tuple[str, str | None]] = {}
    for record in _logical_requirement_records(
        text,
        WORKER_REQUIREMENTS_PATH.as_posix(),
    ):
        name, specifier, marker = _parse_declared_python_requirement(
            record,
            WORKER_REQUIREMENTS_PATH.as_posix(),
        )
        if name in declarations:
            raise ManifestError("apps/worker/requirements.txt: duplicate direct dependency")
        declarations[name] = (specifier, marker)
    if not declarations:
        raise ManifestError("apps/worker/requirements.txt: dependency list is empty")
    return declarations


def _cargo_inventory(repo_root: Path) -> list[dict[str, Any]]:
    manifest = _toml_object(repo_root, CARGO_MANIFEST_PATH)
    lock = _toml_object(repo_root, CARGO_LOCK_PATH)
    if lock.get("version") != 4:
        raise ManifestError("apps/desktop/src-tauri/Cargo.lock: only version 4 is supported")
    package_table = manifest.get("package")
    if not isinstance(package_table, dict):
        raise ManifestError("apps/desktop/src-tauri/Cargo.toml: package table is missing")
    root_name = _string_field(
        package_table,
        "name",
        "apps/desktop/src-tauri/Cargo.toml",
    )
    root_version = _string_field(
        package_table,
        "version",
        "apps/desktop/src-tauri/Cargo.toml",
    )
    packages = lock.get("package")
    if not isinstance(packages, list) or not packages:
        raise ManifestError("apps/desktop/src-tauri/Cargo.lock: package list is missing")
    components: list[dict[str, Any]] = []
    identities: set[tuple[str, str, str]] = set()
    local_roots = 0
    root_locked_names: set[str] | None = None
    for package in packages:
        if not isinstance(package, dict):
            raise ManifestError("apps/desktop/src-tauri/Cargo.lock: package entry is malformed")
        name = _string_field(package, "name", "apps/desktop/src-tauri/Cargo.lock")
        version = _string_field(package, "version", "apps/desktop/src-tauri/Cargo.lock")
        source = package.get("source")
        identity_source = source if isinstance(source, str) else "workspace"
        identity = (name, version, identity_source)
        if identity in identities:
            raise ManifestError("apps/desktop/src-tauri/Cargo.lock: duplicate package identity")
        identities.add(identity)
        if source is None:
            local_roots += 1
            if name != root_name or version != root_version or package.get("checksum") is not None:
                raise ManifestError("apps/desktop/src-tauri/Cargo.lock: local root mismatch")
            root_dependencies = package.get("dependencies", [])
            if not isinstance(root_dependencies, list):
                raise ManifestError(
                    "apps/desktop/src-tauri/Cargo.lock: local root dependencies are invalid"
                )
            root_locked_names = set()
            for dependency in root_dependencies:
                if not isinstance(dependency, str) or not dependency:
                    raise ManifestError(
                        "apps/desktop/src-tauri/Cargo.lock: root dependency is invalid"
                    )
                dependency_name = dependency.split(" ", 1)[0]
                if _CARGO_NAME_PATTERN.fullmatch(dependency_name) is None:
                    raise ManifestError(
                        "apps/desktop/src-tauri/Cargo.lock: root dependency is invalid"
                    )
                root_locked_names.add(dependency_name.replace("_", "-"))
            components.append(
                {
                    "ecosystem": "cargo",
                    "kind": "workspace",
                    "name": name,
                    "version": version,
                    "workspace": "apps/desktop/src-tauri",
                }
            )
            continue
        if source != CARGO_REGISTRY:
            raise ManifestError("apps/desktop/src-tauri/Cargo.lock: unapproved package source")
        checksum = package.get("checksum")
        if not isinstance(checksum, str) or _HEX_SHA256_PATTERN.fullmatch(checksum) is None:
            raise ManifestError("apps/desktop/src-tauri/Cargo.lock: registry checksum is invalid")
        components.append(
            {
                "checksum": f"sha256:{checksum}",
                "ecosystem": "cargo",
                "kind": "registry",
                "name": name,
                "source": source,
                "version": version,
            }
        )
    if local_roots != 1:
        raise ManifestError("apps/desktop/src-tauri/Cargo.lock: exactly one local root is required")

    direct_names: set[str] = set()
    for table_name in ("dependencies", "build-dependencies"):
        table = manifest.get(table_name, {})
        if not isinstance(table, dict):
            raise ManifestError(
                f"apps/desktop/src-tauri/Cargo.toml: {table_name} must be a table"
            )
        for dependency_name, declaration in table.items():
            if (
                not isinstance(dependency_name, str)
                or _CARGO_NAME_PATTERN.fullmatch(dependency_name) is None
            ):
                raise ManifestError(
                    f"apps/desktop/src-tauri/Cargo.toml: invalid {table_name} name"
                )
            if isinstance(declaration, dict) and "package" in declaration:
                raise ManifestError(
                    "apps/desktop/src-tauri/Cargo.toml: renamed dependencies are unsupported"
                )
            direct_names.add(dependency_name.replace("_", "-"))
    if root_locked_names is None or direct_names != root_locked_names:
        raise ManifestError(
            "apps/desktop/src-tauri/Cargo.lock: root dependency names differ"
        )
    return components


def _component_sort_key(component: Mapping[str, Any]) -> tuple[str, ...]:
    return (
        str(component.get("ecosystem", "")),
        str(component.get("lock_path", "")),
        str(component.get("name", "")),
        str(component.get("version", "")),
        str(component.get("scope", "")),
        str(component.get("source", "")),
    )


def build_manifest(repo_root: Path) -> dict[str, Any]:
    root = repo_root.resolve()
    npm_components, npm_inputs = _npm_inventory(root)
    worker_components = _python_lock_inventory(root, WORKER_LOCK_PATH, "worker-runtime")
    security_components = _python_lock_inventory(root, SECURITY_LOCK_PATH, "security-tools")
    project = _toml_object(root, WORKER_PROJECT_PATH)
    declared_python = _declared_python_requirements(project)
    requirements_python = _requirements_python_declarations(root)
    if declared_python != requirements_python:
        raise ManifestError(
            "apps/worker/requirements.txt: declarations differ from pyproject.toml"
        )
    locked_python = {component["name"]: component for component in worker_components}
    if not set(declared_python).issubset(locked_python):
        raise ManifestError("apps/worker/requirements.lock: direct dependency is missing")
    for name, (_specifier, marker) in declared_python.items():
        if locked_python[name].get("marker") != marker:
            raise ManifestError(
                "apps/worker/requirements.lock: direct dependency marker differs"
            )
    cargo_components = _cargo_inventory(root)
    components = sorted(
        npm_components + worker_components + security_components + cargo_components,
        key=_component_sort_key,
    )
    input_paths = sorted(
        {
            GENERATOR_PATH,
            *npm_inputs,
            WORKER_PROJECT_PATH,
            WORKER_REQUIREMENTS_PATH,
            WORKER_LOCK_PATH,
            SECURITY_LOCK_PATH,
            CARGO_MANIFEST_PATH,
            CARGO_LOCK_PATH,
        },
        key=lambda path: path.as_posix(),
    )
    inputs = [
        {
            "path": relative_path.as_posix(),
            "sha256": _sha256(_normalized_text_bytes(root, relative_path)),
        }
        for relative_path in input_paths
    ]
    counts: dict[str, int] = {}
    for component in components:
        ecosystem = str(component["ecosystem"])
        counts[ecosystem] = counts.get(ecosystem, 0) + 1
    return {
        "component_counts": counts,
        "components": components,
        "inputs": inputs,
        "registries": {
            "cargo": CARGO_REGISTRY,
            "npm": NPM_REGISTRY,
        },
        "schema": MANIFEST_SCHEMA,
    }


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def verify_manifest(repo_root: Path) -> tuple[dict[str, Any], bytes]:
    manifest = build_manifest(repo_root)
    expected = canonical_json_bytes(manifest)
    path = _path(repo_root, MANIFEST_PATH)
    if not path.is_file() or path.is_symlink():
        raise ManifestError(f"{MANIFEST_PATH.as_posix()}: committed manifest is missing")
    actual = _normalized_text_bytes(repo_root, MANIFEST_PATH)
    if actual != expected:
        raise ManifestError(
            f"{MANIFEST_PATH.as_posix()}: manifest is stale or non-canonical"
        )
    return manifest, actual


def _git(
    repo_root: Path,
    arguments: Sequence[str],
) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *arguments],
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    if completed.returncode != 0:
        raise ManifestError("Git provenance check failed")
    return completed.stdout.strip()


def _git_bytes(repo_root: Path, arguments: Sequence[str]) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *arguments],
        capture_output=True,
        check=False,
        timeout=15,
    )
    if completed.returncode != 0:
        raise ManifestError("Git provenance check failed")
    return completed.stdout


def _git_file_evidence(
    repo_root: Path,
    revision: str,
    relative_path: str,
) -> tuple[str, str, bytes]:
    listing = _git(repo_root, ("ls-tree", revision, "--", relative_path))
    match = re.fullmatch(
        rf"(?P<mode>100644|100755) blob (?P<oid>[0-9a-f]{{40}})\t{re.escape(relative_path)}",
        listing,
    )
    if match is None:
        raise ManifestError("dependency evidence path is not a tracked regular file")
    payload = _git_bytes(repo_root, ("cat-file", "blob", match.group("oid")))
    return match.group("mode"), match.group("oid"), payload


def build_provenance(
    repo_root: Path,
    expected_revision: str,
    manifest: Mapping[str, Any],
    manifest_bytes: bytes,
) -> dict[str, Any]:
    if _FULL_SHA_PATTERN.fullmatch(expected_revision) is None:
        raise ManifestError("expected revision must be a lowercase full Git SHA")
    root = repo_root.resolve()
    actual_revision = _git(root, ("rev-parse", "--verify", "HEAD^{commit}"))
    if actual_revision != expected_revision:
        raise ManifestError("checked-out HEAD does not match the expected revision")
    tree = _git(root, ("rev-parse", "HEAD^{tree}"))
    if _FULL_SHA_PATTERN.fullmatch(tree) is None:
        raise ManifestError("checked-out Git tree identity is invalid")

    input_paths = [str(item["path"]) for item in manifest["inputs"]]
    evidence_paths = [*input_paths, MANIFEST_PATH.as_posix()]
    diff_result = subprocess.run(
        ["git", "-C", str(root), "diff", "--quiet", "HEAD", "--", *evidence_paths],
        capture_output=True,
        check=False,
        timeout=15,
    )
    if diff_result.returncode != 0:
        raise ManifestError("dependency evidence files do not match the expected revision")

    provenance_inputs: list[dict[str, Any]] = []
    for manifest_input in manifest["inputs"]:
        relative_path = str(manifest_input["path"])
        mode, blob_oid, blob = _git_file_evidence(root, actual_revision, relative_path)
        if _sha256(_normalize_utf8_text(blob, relative_path)) != manifest_input["sha256"]:
            raise ManifestError("dependency input digest does not match the expected revision")
        provenance_inputs.append(
            {
                **manifest_input,
                "git_blob": blob_oid,
                "git_mode": mode,
                "raw_blob_sha256": _sha256(blob),
            }
        )
    manifest_mode, manifest_blob_oid, manifest_blob = _git_file_evidence(
        root,
        actual_revision,
        MANIFEST_PATH.as_posix(),
    )
    if _normalize_utf8_text(manifest_blob, MANIFEST_PATH.as_posix()) != manifest_bytes:
        raise ManifestError("committed manifest bytes do not match the expected revision")
    return {
        "inputs": provenance_inputs,
        "manifest": {
            "git_blob": manifest_blob_oid,
            "git_mode": manifest_mode,
            "path": MANIFEST_PATH.as_posix(),
            "raw_blob_sha256": _sha256(manifest_blob),
            "sha256": _sha256(manifest_bytes),
        },
        "revision": actual_revision,
        "schema": PROVENANCE_SCHEMA,
        "tree": tree,
    }


def _write_provenance(repo_root: Path, output: str, payload: bytes) -> None:
    if output == "-":
        print(payload.decode("utf-8"), end="")
        return
    output_path = Path(output).resolve()
    if output_path.is_relative_to(repo_root.resolve()):
        raise ManifestError("provenance output must be outside the repository")
    if not output_path.parent.is_dir():
        raise ManifestError("provenance output directory does not exist")
    try:
        with output_path.open("xb") as stream:
            stream.write(payload)
    except FileExistsError as exc:
        raise ManifestError("provenance output already exists") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build or verify the deterministic lock-derived dependency manifest."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--expected-revision")
    parser.add_argument("--emit-provenance")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    try:
        if args.write:
            if args.expected_revision is not None or args.emit_provenance is not None:
                raise ManifestError("--write cannot emit revision provenance")
            manifest = build_manifest(repo_root)
            payload = canonical_json_bytes(manifest)
            output = _path(repo_root, MANIFEST_PATH)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(payload)
            print(
                f"Dependency lock manifest written: {MANIFEST_PATH.as_posix()} "
                f"sha256={_sha256(payload)}"
            )
            return 0

        manifest, manifest_bytes = verify_manifest(repo_root)
        manifest_sha = _sha256(manifest_bytes)
        if args.emit_provenance is not None and args.expected_revision is None:
            raise ManifestError("--emit-provenance requires --expected-revision")
        if args.expected_revision is not None:
            provenance = build_provenance(
                repo_root,
                args.expected_revision,
                manifest,
                manifest_bytes,
            )
            if args.emit_provenance is not None:
                _write_provenance(
                    repo_root,
                    args.emit_provenance,
                    canonical_json_bytes(provenance),
                )
        print(f"Dependency lock manifest verified: sha256={manifest_sha}")
        return 0
    except ManifestError as exc:
        print(f"::error::{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
