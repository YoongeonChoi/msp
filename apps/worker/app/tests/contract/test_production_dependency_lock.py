import re
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[5]
WORKER = ROOT / "apps" / "worker"
LOCK = WORKER / "requirements.lock"


def _locked_requirements() -> dict[str, tuple[Version, str]]:
    text = LOCK.read_text(encoding="utf-8")
    blocks = re.findall(
        r"(?ms)^([a-z0-9-]+)==([^\s;]+)(.*?)(?=^[a-z0-9-]+==|\Z)",
        text,
    )
    return {
        canonicalize_name(name): (Version(version), body)
        for name, version, body in blocks
    }


def test_production_lock_pins_and_hashes_every_resolved_requirement() -> None:
    locked = _locked_requirements()

    assert locked
    assert all("--hash=sha256:" in body for _, body in locked.values())
    assert "git+" not in LOCK.read_text(encoding="utf-8").lower()
    assert "http://" not in LOCK.read_text(encoding="utf-8").lower()


def test_direct_requirements_are_present_and_satisfied_by_lock() -> None:
    locked = _locked_requirements()
    direct = [
        Requirement(line)
        for line in (WORKER / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    for requirement in direct:
        name = canonicalize_name(requirement.name)
        assert name in locked
        version, _body = locked[name]
        assert version in requirement.specifier


def test_render_uses_hashed_lock_and_pinned_python_runtime() -> None:
    render = (ROOT / "render.yaml").read_text(encoding="utf-8")

    assert "--require-hashes --only-binary=:all: -r requirements.lock" in render
    assert re.search(r"key:\s*PYTHON_VERSION\s+value:\s*3\.12\.13", render)
