from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
MIGRATIONS_DIR = ROOT / "supabase" / "migrations"
CANONICAL_MIGRATION_DOC = ROOT / "supabase" / "README.md"
SETUP_DOC = ROOT / "docs" / "SUPABASE_SETUP.md"
RUNBOOK = ROOT / "docs" / "RUNBOOK.md"


def _migration_filenames() -> list[str]:
    filenames = sorted(path.name for path in MIGRATIONS_DIR.glob("*.sql"))
    assert filenames, "Supabase migration directory must not be empty"
    return filenames


def _assert_migrations_appear_in_order(path: Path, filenames: list[str]) -> None:
    text = path.read_text(encoding="utf-8")
    positions: list[int] = []
    for filename in filenames:
        position = text.find(filename)
        assert position >= 0, f"{path.relative_to(ROOT)} is missing {filename}"
        positions.append(position)

    assert positions == sorted(positions), (
        f"{path.relative_to(ROOT)} must list Supabase migrations in filename order"
    )


def test_canonical_readme_lists_every_migration_in_order() -> None:
    _assert_migrations_appear_in_order(CANONICAL_MIGRATION_DOC, _migration_filenames())


def test_setup_doc_lists_every_migration_in_order() -> None:
    _assert_migrations_appear_in_order(SETUP_DOC, _migration_filenames())


def test_runbook_references_canonical_list_and_latest_migration() -> None:
    latest_migration = _migration_filenames()[-1]
    runbook = RUNBOOK.read_text(encoding="utf-8")

    assert "supabase/README.md" in runbook
    assert latest_migration in runbook
