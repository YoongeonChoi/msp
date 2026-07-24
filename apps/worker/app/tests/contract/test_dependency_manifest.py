from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[5]
SCRIPT = ROOT / ".github" / "scripts" / "dependency_manifest.py"
MANIFEST = ROOT / ".github" / "dependency-lock-manifest.v1.json"
SECURITY_WORKFLOW = ROOT / ".github" / "workflows" / "security.yml"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("dependency_manifest", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _fixture_repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    script = root / ".github" / "scripts" / "dependency_manifest.py"
    script.parent.mkdir(parents=True)
    script.write_bytes(SCRIPT.read_bytes())

    _write_json(
        root / "package.json",
        {
            "name": "fixture-root",
            "version": "1.0.0",
            "private": True,
            "workspaces": ["apps/web"],
        },
    )
    _write_json(
        root / "apps" / "web" / "package.json",
        {"name": "@fixture/web", "version": "1.0.0", "private": True},
    )
    integrity = "sha512-" + base64.b64encode(b"\x01" * 64).decode("ascii")
    registry_entry = {
        "version": "1.3.0",
        "resolved": "https://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz",
        "integrity": integrity,
    }
    _write_json(
        root / "package-lock.json",
        {
            "name": "fixture-root",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "requires": True,
            "packages": {
                "": {"name": "fixture-root", "version": "1.0.0"},
                "apps/web": {"name": "@fixture/web", "version": "1.0.0"},
                "node_modules/@fixture/web": {"resolved": "apps/web", "link": True},
                "node_modules/left-pad": registry_entry,
                "node_modules/parent/node_modules/left-pad": registry_entry,
            },
        },
    )

    worker = root / "apps" / "worker"
    worker.mkdir(parents=True)
    (worker / "pyproject.toml").write_text(
        """[project]
name = "fixture-worker"
version = "1.0.0"
dependencies = [
  "httpx>=1.0",
  "tzdata>=1.0; sys_platform == 'win32'",
]
""",
        encoding="utf-8",
    )
    (worker / "requirements.txt").write_text(
        "httpx>=1.0\n"
        'tzdata>=1.0; sys_platform == "win32"\n',
        encoding="utf-8",
    )
    (worker / "requirements.lock").write_text(
        """httpx==1.0.0 \\
    --hash=sha256:1111111111111111111111111111111111111111111111111111111111111111 \\
    --hash=sha256:2222222222222222222222222222222222222222222222222222222222222222
tzdata==1.0.0 ; sys_platform == "win32" \\
    --hash=sha256:3333333333333333333333333333333333333333333333333333333333333333
""",
        encoding="utf-8",
    )
    security_lock = root / ".github" / "security-tools.lock"
    security_lock.write_text(
        "bandit==1.0.0 "
        "--hash=sha256:4444444444444444444444444444444444444444444444444444444444444444\n",
        encoding="utf-8",
    )

    cargo = root / "apps" / "desktop" / "src-tauri"
    cargo.mkdir(parents=True)
    (cargo / "Cargo.toml").write_text(
        """[package]
name = "fixture_desktop"
version = "1.0.0"
edition = "2021"

[dependencies]
serde = "1"
""",
        encoding="utf-8",
    )
    (cargo / "Cargo.lock").write_text(
        """version = 4

[[package]]
name = "fixture_desktop"
version = "1.0.0"
dependencies = ["serde"]

[[package]]
name = "serde"
version = "1.0.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "5555555555555555555555555555555555555555555555555555555555555555"
""",
        encoding="utf-8",
    )
    return root


def _write_manifest(module: ModuleType, root: Path) -> bytes:
    payload = module.canonical_json_bytes(module.build_manifest(root))
    assert isinstance(payload, bytes)
    path = root / ".github" / "dependency-lock-manifest.v1.json"
    path.write_bytes(payload)
    return payload


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        check=True,
        text=True,
        timeout=15,
    )
    return completed.stdout.strip()


def _workflow_job_block(text: str, job_name: str) -> str:
    marker = f"  {job_name}:\n"
    start = text.index(marker)
    next_job = text.find("\n  ", start + len(marker))
    while next_job != -1:
        next_line = text[next_job + 1 :].splitlines()[0]
        if next_line.startswith("  ") and not next_line.startswith("    "):
            break
        next_job = text.find("\n  ", next_job + 1)
    return text[start:] if next_job == -1 else text[start:next_job]


def _workflow_step_blocks(job: str) -> list[str]:
    lines = job.splitlines()
    starts = [index for index, line in enumerate(lines) if line.startswith("      - ")]
    return [
        "\n".join(
            lines[
                start : starts[position + 1]
                if position + 1 < len(starts)
                else len(lines)
            ]
        )
        for position, start in enumerate(starts)
    ]


def _yaml_keys_at_indent(block: str, spaces: int) -> set[str]:
    prefix = " " * spaces
    keys: set[str] = set()
    for line in block.splitlines():
        if not line.startswith(prefix) or line.startswith(prefix + " "):
            continue
        field = line[len(prefix) :]
        if ":" in field:
            keys.add(field.split(":", 1)[0].strip(" -'\""))
    return keys


def _folded_run_command(step: str) -> str:
    lines = step.splitlines()
    run_index = lines.index("        run: >-")
    command_lines: list[str] = []
    for line in lines[run_index + 1 :]:
        if not line.startswith("          "):
            break
        command_lines.append(line.strip())
    return " ".join(command_lines)


def _literal_run_body(step: str) -> str:
    lines = step.splitlines()
    run_index = lines.index("        run: |")
    body_lines: list[str] = []
    for line in lines[run_index + 1 :]:
        if not line.startswith("          "):
            break
        body_lines.append(line[10:])
    return "\n".join(body_lines)


def test_committed_manifest_matches_current_repository() -> None:
    module = _module()

    manifest, payload = module.verify_manifest(ROOT)

    assert manifest["schema"] == module.MANIFEST_SCHEMA
    assert payload == module.canonical_json_bytes(manifest)
    assert manifest["component_counts"] == {
        "cargo": 418,
        "npm": 393,
        "pypi": 50,
    }
    assert "apps/worker/requirements.txt" in {
        item["path"] for item in manifest["inputs"]
    }


def test_manifest_generation_is_byte_deterministic(tmp_path: Path) -> None:
    module = _module()
    root = _fixture_repository(tmp_path)

    first = module.canonical_json_bytes(module.build_manifest(root))
    second = module.canonical_json_bytes(module.build_manifest(root))

    assert first == second
    assert first.endswith(b"\n")
    assert not first.endswith(b"\n\n")
    assert b"timestamp" not in first
    assert str(root).encode() not in first


def test_npm_lock_paths_and_python_marker_are_preserved(tmp_path: Path) -> None:
    module = _module()
    root = _fixture_repository(tmp_path)

    manifest = module.build_manifest(root)
    left_pad_paths = {
        component["lock_path"]
        for component in manifest["components"]
        if component["ecosystem"] == "npm" and component["name"] == "left-pad"
    }
    tzdata = next(
        component
        for component in manifest["components"]
        if component.get("scope") == "worker-runtime"
        and component["name"] == "tzdata"
    )
    workspace_link = next(
        component
        for component in manifest["components"]
        if component.get("kind") == "workspace-link"
    )

    assert left_pad_paths == {
        "node_modules/left-pad",
        "node_modules/parent/node_modules/left-pad",
    }
    assert workspace_link == {
        "ecosystem": "npm",
        "kind": "workspace-link",
        "lock_path": "node_modules/@fixture/web",
        "name": "@fixture/web",
        "resolved": "apps/web",
        "version": "1.0.0",
        "workspace": "apps/web",
    }
    assert tzdata["marker"] == 'sys_platform == "win32"'


@pytest.mark.parametrize(
    ("relative_path", "old", "new"),
    (
        ("package-lock.json", "sha512-AQE", "sha512-AgE"),
        ("apps/worker/requirements.lock", "sha256:111", "sha256:211"),
        ("apps/desktop/src-tauri/Cargo.lock", 'checksum = "555', 'checksum = "655'),
    ),
)
def test_parseable_lock_tampering_makes_manifest_stale(
    tmp_path: Path,
    relative_path: str,
    old: str,
    new: str,
) -> None:
    module = _module()
    root = _fixture_repository(tmp_path)
    _write_manifest(module, root)
    path = root / relative_path
    original = path.read_text(encoding="utf-8")
    assert old in original
    path.write_text(original.replace(old, new, 1), encoding="utf-8")

    with pytest.raises(module.ManifestError, match="manifest is stale"):
        module.verify_manifest(root)


def test_npm_credential_source_is_rejected_without_echoing_value(tmp_path: Path) -> None:
    module = _module()
    root = _fixture_repository(tmp_path)
    path = root / "package-lock.json"
    credential_prefix = "redacted-user@"
    text = path.read_text(encoding="utf-8").replace(
        "https://registry.npmjs.org/left-pad/",
        f"https://{credential_prefix}registry.npmjs.org/left-pad/",
    )
    path.write_text(text, encoding="utf-8")

    with pytest.raises(module.ManifestError) as error:
        module.build_manifest(root)

    assert "registry artifact identity mismatch" in str(error.value)
    assert credential_prefix not in str(error.value)


@pytest.mark.parametrize(
    "value",
    (
        "a//b",
        "a/./b",
        "C:/escape",
        "a:b",
        "a\x00b",
        "a\\b",
        "../outside",
        "node_modules/con.txt",
        "node_modules/package.",
    ),
)
def test_noncanonical_repository_paths_are_rejected(value: str) -> None:
    module = _module()

    with pytest.raises(module.ManifestError, match="safe relative path"):
        module._safe_relative_path(value)


@pytest.mark.parametrize(
    "bad_locator",
    (
        "../node_modules/left-pad",
        "other/node_modules/left-pad",
        "node_modules/parent/../node_modules/left-pad",
        "node_modules/parent//node_modules/left-pad",
        "C:/node_modules/left-pad",
        "node_modules/@scope",
    ),
)
def test_npm_noncanonical_or_unanchored_locators_are_rejected(
    tmp_path: Path,
    bad_locator: str,
) -> None:
    module = _module()
    root = _fixture_repository(tmp_path)
    lock_path = root / "package-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    entry = lock["packages"].pop("node_modules/parent/node_modules/left-pad")
    lock["packages"][bad_locator] = entry
    _write_json(lock_path, lock)

    with pytest.raises(module.ManifestError):
        module.build_manifest(root)


def test_npm_declaration_maps_are_exactly_bound(tmp_path: Path) -> None:
    module = _module()
    root = _fixture_repository(tmp_path)
    package_path = root / "package.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    package["dependencies"] = {"ghost-package": "1.0.0"}
    _write_json(package_path, package)

    with pytest.raises(module.ManifestError, match="dependency declarations differ"):
        module.build_manifest(root)


def test_npm_lock_only_declaration_is_rejected(tmp_path: Path) -> None:
    module = _module()
    root = _fixture_repository(tmp_path)
    lock_path = root / "package-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["packages"][""]["dependencies"] = {"ghost-package": "1.0.0"}
    _write_json(lock_path, lock)

    with pytest.raises(module.ManifestError, match="dependency declarations differ"):
        module.build_manifest(root)


def test_npm_name_and_semver_boundaries_are_explicit() -> None:
    module = _module()

    assert module._valid_npm_name("@scope/_package")
    assert module._valid_npm_name("a" * 214)
    assert not module._valid_npm_name("a" * 215)
    assert module._valid_npm_version("1.2.3-0")
    assert module._valid_npm_version("1.2.3-rc.1+build.01")
    assert not module._valid_npm_version("1.2.3-01")


def test_npm_registry_artifact_name_and_version_are_exact(tmp_path: Path) -> None:
    module = _module()
    root = _fixture_repository(tmp_path)
    lock_path = root / "package-lock.json"
    text = lock_path.read_text(encoding="utf-8").replace(
        "left-pad/-/left-pad-1.3.0.tgz",
        "different/-/different-9.9.9.tgz",
    )
    lock_path.write_text(text, encoding="utf-8")

    with pytest.raises(module.ManifestError, match="artifact identity mismatch"):
        module.build_manifest(root)


def test_npm_scoped_alias_preserves_registry_identity(tmp_path: Path) -> None:
    module = _module()
    root = _fixture_repository(tmp_path)
    package_path = root / "package.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    package["dependencies"] = {
        "babel-core-legacy": "npm:@babel/core@>=7.0.0 <8.0.0"
    }
    _write_json(package_path, package)

    lock_path = root / "package-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["packages"][""]["dependencies"] = package["dependencies"]
    lock["packages"]["node_modules/babel-core-legacy"] = {
        "name": "@babel/core",
        "version": "7.28.6",
        "resolved": "https://registry.npmjs.org/@babel/core/-/core-7.28.6.tgz",
        "integrity": "sha512-"
        + base64.b64encode(b"\x02" * 64).decode("ascii"),
    }
    _write_json(lock_path, lock)

    manifest = module.build_manifest(root)
    alias = next(
        component
        for component in manifest["components"]
        if component.get("alias") == "babel-core-legacy"
    )

    assert alias["name"] == "@babel/core"
    assert alias["version"] == "7.28.6"
    assert alias["lock_path"] == "node_modules/babel-core-legacy"


def test_npm_alias_name_requires_matching_declaration(tmp_path: Path) -> None:
    module = _module()
    root = _fixture_repository(tmp_path)
    lock_path = root / "package-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    entry = lock["packages"]["node_modules/left-pad"]
    entry["name"] = "@scope/different"
    entry["resolved"] = (
        "https://registry.npmjs.org/@scope/different/-/different-1.3.0.tgz"
    )
    _write_json(lock_path, lock)

    with pytest.raises(module.ManifestError, match="unproven npm alias identity"):
        module.build_manifest(root)


def test_workspace_link_rejects_extra_descriptor_fields(tmp_path: Path) -> None:
    module = _module()
    root = _fixture_repository(tmp_path)
    lock_path = root / "package-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["packages"]["node_modules/@fixture/web"]["dev"] = True
    _write_json(lock_path, lock)

    with pytest.raises(module.ManifestError, match="link descriptor is malformed"):
        module.build_manifest(root)


def test_workspace_traversal_is_rejected(tmp_path: Path) -> None:
    module = _module()
    root = _fixture_repository(tmp_path)
    package_path = root / "package.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    package["workspaces"] = ["../outside"]
    _write_json(package_path, package)

    with pytest.raises(module.ManifestError, match="safe relative path"):
        module.build_manifest(root)


@pytest.mark.parametrize(
    "replacement",
    (
        "httpx==1.0.0",
        "httpx @ https://packages.invalid/httpx.whl "
        "--hash=sha256:1111111111111111111111111111111111111111111111111111111111111111",
        "httpx==1.0.0 ; python_version in unsafe "
        "--hash=sha256:1111111111111111111111111111111111111111111111111111111111111111",
        "httpx==1.0@packages.invalid "
        "--hash=sha256:1111111111111111111111111111111111111111111111111111111111111111",
        'httpx==1.0.0 ; made_up_variable == "allowed" '
        "--hash=sha256:1111111111111111111111111111111111111111111111111111111111111111",
        'httpx==1.0.0 ; platform_release == "Windows  11" '
        "--hash=sha256:1111111111111111111111111111111111111111111111111111111111111111",
    ),
)
def test_unsafe_python_requirement_forms_are_rejected(
    tmp_path: Path,
    replacement: str,
) -> None:
    module = _module()
    root = _fixture_repository(tmp_path)
    lock = root / "apps" / "worker" / "requirements.lock"
    text = lock.read_text(encoding="utf-8")
    first_record, remainder = text.split("tzdata==", 1)
    assert first_record.startswith("httpx==")
    lock.write_text(f"{replacement}\ntzdata=={remainder}", encoding="utf-8")

    with pytest.raises(module.ManifestError):
        module.build_manifest(root)


def test_worker_declaration_files_must_match(tmp_path: Path) -> None:
    module = _module()
    root = _fixture_repository(tmp_path)
    project_path = root / "apps" / "worker" / "pyproject.toml"
    project_path.write_text(
        project_path.read_text(encoding="utf-8").replace("httpx>=1.0", "httpx>=99.0"),
        encoding="utf-8",
    )

    with pytest.raises(module.ManifestError, match="declarations differ"):
        module.build_manifest(root)


def test_worker_lock_markers_must_match_direct_declarations(tmp_path: Path) -> None:
    module = _module()
    root = _fixture_repository(tmp_path)
    lock_path = root / "apps" / "worker" / "requirements.lock"
    lock_path.write_text(
        lock_path.read_text(encoding="utf-8").replace('"win32"', '"linux"'),
        encoding="utf-8",
    )

    with pytest.raises(module.ManifestError, match="marker differs"):
        module.build_manifest(root)


def test_cargo_registry_checksum_and_source_are_fail_closed(tmp_path: Path) -> None:
    module = _module()
    root = _fixture_repository(tmp_path)
    lock = root / "apps" / "desktop" / "src-tauri" / "Cargo.lock"
    original = lock.read_text(encoding="utf-8")
    lock.write_text(
        original.replace('checksum = "555', 'checksum = "xyz'),
        encoding="utf-8",
    )
    with pytest.raises(module.ManifestError, match="checksum is invalid"):
        module.build_manifest(root)

    lock.write_text(
        original.replace(
            "registry+https://github.com/rust-lang/crates.io-index",
            "git+https://example.invalid/repository#" + "a" * 40,
        ),
        encoding="utf-8",
    )
    with pytest.raises(module.ManifestError, match="unapproved package source"):
        module.build_manifest(root)


def test_cargo_root_dependency_names_are_exactly_bound(tmp_path: Path) -> None:
    module = _module()
    root = _fixture_repository(tmp_path)
    manifest_path = root / "apps" / "desktop" / "src-tauri" / "Cargo.toml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8") + 'ghost = "1"\n',
        encoding="utf-8",
    )

    with pytest.raises(module.ManifestError, match="root dependency names differ"):
        module.build_manifest(root)


def test_manifest_write_refuses_symlink_target(tmp_path: Path) -> None:
    module = _module()
    root = _fixture_repository(tmp_path)
    victim = root / "victim.txt"
    victim.write_text("keep-me\n", encoding="utf-8")
    output = root / ".github" / "dependency-lock-manifest.v1.json"
    try:
        output.symlink_to("../victim.txt")
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    result = module.main(["--write", "--repo-root", str(root)])

    assert result == 1
    assert victim.read_text(encoding="utf-8") == "keep-me\n"


def test_dependency_input_refuses_ancestor_symlink(tmp_path: Path) -> None:
    module = _module()
    root = _fixture_repository(tmp_path)
    worker = root / "apps" / "worker"
    real_worker = root / "apps" / "worker-real"
    worker.rename(real_worker)
    try:
        worker.symlink_to("worker-real", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")

    with pytest.raises(module.ManifestError, match="symbolic link or junction"):
        module.build_manifest(root)


def test_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    module = _module()
    root = _fixture_repository(tmp_path)
    (root / "package.json").write_text(
        '{"name":"fixture-root","name":"other","version":"1.0.0",'
        '"workspaces":["apps/web"]}\n',
        encoding="utf-8",
    )

    with pytest.raises(module.ManifestError, match="duplicate JSON key"):
        module.build_manifest(root)


def test_provenance_binds_manifest_and_inputs_to_exact_git_revision(
    tmp_path: Path,
) -> None:
    module = _module()
    root = _fixture_repository(tmp_path)
    payload = _write_manifest(module, root)
    _git(root, "init", "--quiet")
    _git(root, "add", "--", ".")
    _git(
        root,
        "-c",
        "user.name=QA Test",
        "-c",
        "user.email=qa@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "fixture",
    )
    revision = _git(root, "rev-parse", "HEAD")
    manifest, verified_payload = module.verify_manifest(root)

    provenance = module.build_provenance(root, revision, manifest, verified_payload)

    assert verified_payload == payload
    assert provenance["revision"] == revision
    assert len(provenance["tree"]) == 40
    assert provenance["manifest"]["sha256"] == module._sha256(payload)
    assert all(len(item["git_blob"]) == 40 for item in provenance["inputs"])

    with pytest.raises(module.ManifestError, match="lowercase full Git SHA"):
        module.build_provenance(root, revision.upper(), manifest, verified_payload)

    package_lock = root / "package-lock.json"
    package_lock.write_text(
        package_lock.read_text(encoding="utf-8").replace("left-pad", "rightpad", 1),
        encoding="utf-8",
    )
    with pytest.raises(module.ManifestError, match="do not match"):
        module.build_provenance(root, revision, manifest, verified_payload)


def test_security_workflow_runs_blocking_dependency_evidence_job() -> None:
    text = SECURITY_WORKFLOW.read_text(encoding="utf-8")
    job = _workflow_job_block(text, "dependency-evidence")
    job_keys = _yaml_keys_at_indent(job, 4)
    top_level_keys = _yaml_keys_at_indent(text, 0)
    steps = _workflow_step_blocks(job)
    verify_step = next(
        step
        for step in steps
        if "- name: Verify lock-derived dependency inventory" in step
    )
    publish_step = next(
        step for step in steps if "- name: Publish bounded provenance summary" in step
    )

    assert "\n    runs-on: ubuntu-24.04\n" in job
    assert 'python-version: "3.12.13"' in job
    assert "persist-credentials: false" in job
    assert "\n    permissions:\n      contents: read\n    steps:\n" in job
    assert job.count("\n    permissions:") == 1
    assert job_keys == {"name", "runs-on", "timeout-minutes", "permissions", "steps"}
    assert {"env", "defaults"}.isdisjoint(top_level_keys)
    assert len(steps) == 4
    assert steps[0].splitlines()[0] == (
        "      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5"
        " # v4"
    )
    assert _yaml_keys_at_indent(steps[0], 8) == {"with"}
    assert steps[1].splitlines()[0] == (
        "      - uses: actions/setup-python@"
        "a26af69be951a213d495a4c3e4e4022e16d87065 # v5"
    )
    assert _yaml_keys_at_indent(steps[1], 8) == {"with"}
    assert verify_step.splitlines()[0] == (
        "      - name: Verify lock-derived dependency inventory"
    )
    assert _yaml_keys_at_indent(verify_step, 8) == {"shell", "env", "run"}
    assert publish_step.splitlines()[0] == (
        "      - name: Publish bounded provenance summary"
    )
    assert _yaml_keys_at_indent(publish_step, 8) == {"shell", "run"}
    assert verify_step.count("\n        shell: bash\n") == 1
    assert publish_step.count("\n        shell: bash\n") == 1
    assert (
        "\n        env:\n"
        "          EXPECTED_REVISION: ${{ github.sha }}\n"
        "        run: >-\n"
    ) in verify_step
    assert _folded_run_command(verify_step) == (
        "python .github/scripts/dependency_manifest.py --check "
        '--expected-revision "$EXPECTED_REVISION" '
        '--emit-provenance "$RUNNER_TEMP/dependency-provenance.json"'
    )
    assert _literal_run_body(publish_step) == "\n".join(
        (
            "set -euo pipefail",
            "{",
            '  echo "## Lock-derived dependency evidence"',
            "  echo",
            '  echo "Unsigned CI receipt; not a standard SBOM, attestation, '
            'or release authorization."',
            "  echo '```json'",
            '  cat "$RUNNER_TEMP/dependency-provenance.json"',
            "  echo '```'",
            '} >> "$GITHUB_STEP_SUMMARY"',
        )
    )
    assert "pip install" not in job
    assert "npm install" not in job
    assert "cargo install" not in job
    assert "permissions:\n  contents: read" in text
    assert MANIFEST.as_posix().endswith(".github/dependency-lock-manifest.v1.json")
