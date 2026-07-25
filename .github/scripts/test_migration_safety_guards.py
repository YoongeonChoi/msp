from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT_DIR = Path(__file__).resolve().parent


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


migration_history_guard = _load_script("migration_history_guard")
repository_safety = _load_script("repository_safety")


class GitFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.git("init", "-q")
        self.git("config", "user.name", "Migration Guard Test")
        self.git("config", "user.email", "migration-guard@example.invalid")
        self.git("config", "core.autocrlf", "false")

    def git(self, *args: str, input_text: str | None = None) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.root), *args],
            input=input_text,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise AssertionError(f"git {' '.join(args)} failed: {detail}")
        return result.stdout.strip()

    def write(self, relative_path: str, text: str = "select 1;\n") -> None:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def commit(self, message: str, *paths: str) -> str:
        if paths:
            self.git("add", "--", *paths)
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def root_commit(self) -> str:
        self.write("README.md", "fixture\n")
        return self.commit("root", "README.md")

    def timestamp_base(self) -> tuple[str, str]:
        root = self.root_commit()
        path = "supabase/migrations/20260719080000_base.sql"
        self.write(path)
        return root, self.commit("timestamp base", path)

    def legacy_base(self) -> tuple[str, str]:
        root = self.root_commit()
        path = "supabase/migrations/0001_base.sql"
        self.write(path)
        return root, self.commit("legacy base", path)

    def guard(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            result = migration_history_guard.main(
                ["--repo-root", str(self.root), *arguments]
            )
        return result, output.getvalue()

    def remote_guard(self, base: str, head: str | None = None) -> tuple[int, str]:
        return self.guard(
            "--base",
            base,
            "--head",
            head or self.git("rev-parse", "HEAD"),
        )


class MigrationHistoryGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.fixture = GitFixture(Path(self.temporary_directory.name))

    def test_remote_accepts_timestamp_strictly_after_base_tail(self) -> None:
        _root, base = self.fixture.timestamp_base()
        path = "supabase/migrations/20260719090000_next.sql"
        self.fixture.write(path)
        head = self.fixture.commit("valid tail", path)

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 0, output)
        self.assertIn("new migrations: 1", output)

    def test_new_ref_zero_base_rejects_exact_existing_tip(self) -> None:
        _root, head = self.fixture.timestamp_base()

        result, output = self.fixture.remote_guard("0" * 40, head)

        self.assertEqual(result, 2, output)
        self.assertIn("all-zero --base is not a trusted migration boundary", output)

    def test_new_ref_zero_base_rejects_descendant_append(self) -> None:
        _root, _base = self.fixture.timestamp_base()
        path = "supabase/migrations/20260719090000_next.sql"
        self.fixture.write(path)
        head = self.fixture.commit("new ref descendant", path)

        result, output = self.fixture.remote_guard("0" * 40, head)

        self.assertEqual(result, 2, output)
        self.assertIn("protected long-lived refs must not be created", output)

    def test_remote_rejects_backdated_timestamp(self) -> None:
        _root, base = self.fixture.timestamp_base()
        path = "supabase/migrations/20260701000000_backdated.sql"
        self.fixture.write(path)
        head = self.fixture.commit("backdated", path)

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 1, output)
        self.assertIn("must be newer than base tail 20260719080000", output)

    def test_remote_rejects_duplicate_timestamp_version(self) -> None:
        _root, base = self.fixture.timestamp_base()
        path = "supabase/migrations/20260719080000_duplicate.sql"
        self.fixture.write(path)
        head = self.fixture.commit("duplicate version", path)

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 1, output)
        self.assertIn("already exists in base", output)

    def test_remote_rejects_duplicate_new_timestamp_versions(self) -> None:
        _root, base = self.fixture.timestamp_base()
        first = "supabase/migrations/20260719090000_first.sql"
        second = "supabase/migrations/20260719090000_second.sql"
        self.fixture.write(first)
        self.fixture.write(second)
        head = self.fixture.commit("duplicate new versions", first, second)

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 1, output)
        self.assertIn("duplicate new migration version 20260719090000", output)

    def test_worktree_rejects_backdated_and_duplicate_versions(self) -> None:
        _root, _base = self.fixture.timestamp_base()
        self.fixture.write("supabase/migrations/20260701000000_backdated.sql")
        self.fixture.write("supabase/migrations/20260719080000_duplicate.sql")

        result, output = self.fixture.guard("--worktree")

        self.assertEqual(result, 1, output)
        self.assertIn("must be newer than base tail", output)
        self.assertIn("already exists in base", output)

    def test_worktree_rejects_duplicate_new_timestamp_versions(self) -> None:
        _root, _base = self.fixture.timestamp_base()
        self.fixture.write("supabase/migrations/20260719090000_first.sql")
        self.fixture.write("supabase/migrations/20260719090000_second.sql")

        result, output = self.fixture.guard("--worktree")

        self.assertEqual(result, 1, output)
        self.assertIn("duplicate new migration version 20260719090000", output)

    def test_remote_requires_each_commit_to_append_after_parent_tail(self) -> None:
        _root, base = self.fixture.timestamp_base()
        later = "supabase/migrations/20260719100000_later.sql"
        earlier = "supabase/migrations/20260719090000_earlier.sql"
        self.fixture.write(later)
        self.fixture.commit("later first", later)
        self.fixture.write(earlier)
        head = self.fixture.commit("earlier second", earlier)

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 1, output)
        self.assertIn("first-parent append", output)
        self.assertIn("must be newer than base tail 20260719100000", output)

    def test_remote_rejects_modify_of_candidate_migration_after_add_commit(self) -> None:
        _root, base = self.fixture.timestamp_base()
        path = "supabase/migrations/20260719090000_candidate.sql"
        self.fixture.write(path, "select 1;\n")
        self.fixture.commit("add candidate migration", path)
        self.fixture.write(path, "select 2;\n")
        head = self.fixture.commit("modify candidate migration", path)

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 1, output)
        self.assertRegex(output, r"commit [0-9a-f]{12} M: .*20260719090000_candidate\.sql")

    def test_remote_rejects_delete_of_candidate_migration_after_add_commit(self) -> None:
        _root, base = self.fixture.timestamp_base()
        path = "supabase/migrations/20260719090000_candidate.sql"
        self.fixture.write(path)
        self.fixture.commit("add candidate migration", path)
        self.fixture.git("rm", "-q", "--", path)
        head = self.fixture.commit("delete candidate migration")

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 1, output)
        self.assertRegex(output, r"commit [0-9a-f]{12} D: .*20260719090000_candidate\.sql")

    def test_remote_rejects_rename_of_candidate_migration_after_add_commit(self) -> None:
        _root, base = self.fixture.timestamp_base()
        source = "supabase/migrations/20260719090000_candidate.sql"
        target = "supabase/migrations/20260719100000_renamed.sql"
        self.fixture.write(source)
        self.fixture.commit("add candidate migration", source)
        self.fixture.git("mv", "--", source, target)
        head = self.fixture.commit("rename candidate migration")

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 1, output)
        self.assertIn(source, output)
        self.assertIn(target, output)

    def test_remote_rejects_merge_time_modify_of_second_parent_migration(self) -> None:
        base, path = self._begin_second_parent_candidate_merge()
        self.fixture.write(path, "select 2;\n")
        head = self.fixture.commit("merge modified candidate migration", path)

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 1, output)
        self.assertRegex(output, r"commit [0-9a-f]{12} parent [0-9a-f]{12} M: .*candidate\.sql")

    def test_remote_rejects_merge_time_delete_of_second_parent_migration(self) -> None:
        base, path = self._begin_second_parent_candidate_merge()
        self.fixture.git("rm", "-q", "-f", "--", path)
        head = self.fixture.commit("merge deleted candidate migration")

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 1, output)
        self.assertRegex(output, r"commit [0-9a-f]{12} parent [0-9a-f]{12} D: .*candidate\.sql")

    def test_remote_rejects_merge_time_rename_of_second_parent_migration(self) -> None:
        base, source = self._begin_second_parent_candidate_merge()
        target = "supabase/migrations/20260719100000_renamed.sql"
        self.fixture.git("mv", "--", source, target)
        head = self.fixture.commit("merge renamed candidate migration")

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 1, output)
        self.assertIn(source, output)
        self.assertIn(target, output)

    def test_remote_accepts_unchanged_base_only_migration_from_first_parent(self) -> None:
        base, _base_only_path = self._begin_base_advanced_candidate_merge()
        head = self.fixture.commit("merge candidate after base advanced")

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 0, output)
        self.assertIn("new migrations: 1", output)

    def test_remote_rejects_merge_time_modify_of_base_only_migration(self) -> None:
        base, base_only_path = self._begin_base_advanced_candidate_merge()
        self.fixture.write(base_only_path, "select 2;\n")
        head = self.fixture.commit("merge modified base-only migration", base_only_path)

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 1, output)
        self.assertIn(f"final tree changed committed migration: {base_only_path}", output)

    def test_remote_rejects_head_delete_even_when_merge_restores_first_parent(self) -> None:
        _root, base = self.fixture.timestamp_base()
        protected_path = "supabase/migrations/20260719080000_base.sql"
        self.fixture.git("switch", "-q", "-c", "topic", base)
        self.fixture.git("rm", "-q", "--", protected_path)
        self.fixture.commit("delete protected migration on topic")

        self.fixture.git("switch", "-q", "-c", "integration", base)
        self.fixture.git("merge", "--no-ff", "--no-commit", "topic")
        self.fixture.write(protected_path)
        head = self.fixture.commit("restore protected migration in merge", protected_path)

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 1, output)
        self.assertRegex(
            output,
            r"commit [0-9a-f]{12} D: .*20260719080000_base\.sql",
        )

    def _begin_second_parent_candidate_merge(self) -> tuple[str, str]:
        _root, base = self.fixture.timestamp_base()
        path = "supabase/migrations/20260719090000_candidate.sql"
        self.fixture.git("switch", "-q", "-c", "topic", base)
        self.fixture.write(path)
        self.fixture.commit("add topic candidate migration", path)
        self.fixture.git("switch", "-q", "-c", "integration", base)
        self.fixture.write("integration.txt", "integration\n")
        self.fixture.commit("advance integration branch", "integration.txt")
        self.fixture.git("merge", "--no-ff", "--no-commit", "topic")
        return base, path

    def _begin_base_advanced_candidate_merge(self) -> tuple[str, str]:
        _root, fork = self.fixture.timestamp_base()
        candidate_path = "supabase/migrations/20260719100000_candidate.sql"
        self.fixture.git("switch", "-q", "-c", "topic", fork)
        self.fixture.write(candidate_path)
        self.fixture.commit("add topic candidate migration", candidate_path)

        base_only_path = "supabase/migrations/20260719090000_base_only.sql"
        self.fixture.git("switch", "-q", "-c", "integration", fork)
        self.fixture.write(base_only_path)
        base = self.fixture.commit("advance base migrations", base_only_path)
        self.fixture.git("merge", "--no-ff", "--no-commit", "topic")
        return base, base_only_path

    def test_remote_rejects_modify_then_restore_of_protected_migration(self) -> None:
        _root, base = self.fixture.timestamp_base()
        path = "supabase/migrations/20260719080000_base.sql"
        self.fixture.write(path, "select 2;\n")
        self.fixture.commit("modify protected migration", path)
        self.fixture.write(path, "select 1;\n")
        head = self.fixture.commit("restore protected migration", path)

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 1, output)
        self.assertRegex(output, r"commit [0-9a-f]{12} M: .*20260719080000_base\.sql")

    def test_remote_rejects_deleted_protected_migration(self) -> None:
        _root, base = self.fixture.timestamp_base()
        path = "supabase/migrations/20260719080000_base.sql"
        self.fixture.git("rm", "-q", "--", path)
        head = self.fixture.commit("delete protected migration")

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 1, output)
        self.assertIn(f"final tree changed committed migration: {path}", output)

    def test_remote_rejects_renamed_protected_migration(self) -> None:
        _root, base = self.fixture.timestamp_base()
        source = "supabase/migrations/20260719080000_base.sql"
        target = "supabase/migrations/20260719090000_renamed.sql"
        self.fixture.git("mv", "--", source, target)
        head = self.fixture.commit("rename protected migration")

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 1, output)
        self.assertIn(source, output)
        self.assertIn(target, output)

    def test_worktree_must_append_after_index_tail(self) -> None:
        _root, _base = self.fixture.timestamp_base()
        staged = "supabase/migrations/20260719100000_staged.sql"
        unstaged = "supabase/migrations/20260719090000_unstaged.sql"
        self.fixture.write(staged)
        self.fixture.git("add", "--", staged)
        self.fixture.write(unstaged)

        result, output = self.fixture.guard("--worktree")

        self.assertEqual(result, 1, output)
        self.assertIn("index..worktree", output)
        self.assertIn("must be newer than base tail 20260719100000", output)

    def test_worktree_rejects_staged_edit_hidden_by_unstaged_restore(self) -> None:
        _root, _base = self.fixture.timestamp_base()
        path = "supabase/migrations/20260719080000_base.sql"
        self.fixture.write(path, "select 2;\n")
        self.fixture.git("add", "--", path)
        self.fixture.write(path, "select 1;\n")

        result, output = self.fixture.guard("--worktree")

        self.assertEqual(result, 1, output)
        self.assertIn(f"HEAD..index M: {path}", output)
        self.assertIn(f"index..worktree M: {path}", output)

    def test_legacy_sequence_can_continue_then_transition_to_timestamp(self) -> None:
        _root, base = self.fixture.legacy_base()
        legacy = "supabase/migrations/0002_continue.sql"
        timestamp = "supabase/migrations/20260719090000_transition.sql"
        self.fixture.write(legacy)
        self.fixture.write(timestamp)
        head = self.fixture.commit("valid transition", legacy, timestamp)

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 0, output)
        self.assertIn("new migrations: 2", output)

    def test_legacy_sequence_rejects_gap(self) -> None:
        _root, base = self.fixture.legacy_base()
        path = "supabase/migrations/0003_gap.sql"
        self.fixture.write(path)
        head = self.fixture.commit("legacy gap", path)

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 1, output)
        self.assertIn("must append as 0002, not 0003", output)

    def test_timestamp_transition_must_sort_after_legacy_filename(self) -> None:
        _root, base = self.fixture.legacy_base()
        path = "supabase/migrations/00010000000000_bad_transition.sql"
        self.fixture.write(path)
        head = self.fixture.commit("bad transition", path)

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 1, output)
        self.assertIn("does not sort after legacy tail 0001", output)

    def test_timestamp_phase_rejects_new_legacy_migration(self) -> None:
        _root, base = self.fixture.timestamp_base()
        path = "supabase/migrations/0001_late_legacy.sql"
        self.fixture.write(path)
        head = self.fixture.commit("late legacy", path)

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 1, output)
        self.assertIn("cannot be added after the timestamp phase began", output)

    def test_merge_first_parent_add_of_protected_path_is_rejected(self) -> None:
        root, base = self.fixture.timestamp_base()
        self.fixture.git("switch", "-q", "--detach", root)
        self.fixture.write("side.txt", "side\n")
        self.fixture.commit("side", "side.txt")
        self.fixture.git("merge", "--no-ff", "-m", "merge base", base)
        head = self.fixture.git("rev-parse", "HEAD")

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 1, output)
        self.assertRegex(
            output,
            r"commit [0-9a-f]{12} parent [0-9a-f]{12} A: .*20260719080000_base\.sql",
        )

    def test_remote_accepts_tree_identical_long_lived_sync_merge(self) -> None:
        root, base = self.fixture.timestamp_base()
        self.fixture.git("switch", "-q", "--detach", root)
        self.fixture.git("merge", "--no-ff", "-m", "merge reviewed base", base)
        head = self.fixture.git("rev-parse", "HEAD")

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 0, output)
        self.assertIn("new migrations: 0", output)

    def test_remote_rejects_non_noop_long_lived_sync_merge(self) -> None:
        root, base = self.fixture.timestamp_base()
        self.fixture.git("switch", "-q", "--detach", root)
        self.fixture.git("merge", "--no-ff", "--no-commit", base)
        self.fixture.write("README.md", "changed during merge\n")
        head = self.fixture.commit("mutate reviewed base merge", "README.md")

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 1, output)
        self.assertRegex(
            output,
            r"commit [0-9a-f]{12} parent [0-9a-f]{12} A: .*20260719080000_base\.sql",
        )

    def test_remote_rejects_sync_merge_with_modified_protected_migration(self) -> None:
        _root, first_parent = self.fixture.timestamp_base()
        path = "supabase/migrations/20260719080000_base.sql"
        self.fixture.write(path, "select 2;\n")
        base = self.fixture.commit("unsafe trusted modification", path)
        self.fixture.git("switch", "-q", "--detach", first_parent)
        self.fixture.git("merge", "--no-ff", "-m", "sync unsafe modification", base)
        head = self.fixture.git("rev-parse", "HEAD")

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 1, output)
        self.assertRegex(
            output,
            r"commit [0-9a-f]{12} parent [0-9a-f]{12} M: .*20260719080000_base\.sql",
        )

    def test_remote_rejects_sync_merge_with_deleted_protected_migration(self) -> None:
        _root, first_parent = self.fixture.timestamp_base()
        path = "supabase/migrations/20260719080000_base.sql"
        self.fixture.git("rm", "-q", "--", path)
        base = self.fixture.commit("unsafe trusted deletion")
        self.fixture.git("switch", "-q", "--detach", first_parent)
        self.fixture.git("merge", "--no-ff", "-m", "sync unsafe deletion", base)
        head = self.fixture.git("rev-parse", "HEAD")

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 1, output)
        self.assertRegex(
            output,
            r"commit [0-9a-f]{12} parent [0-9a-f]{12} D: .*20260719080000_base\.sql",
        )

    def test_remote_rejects_sync_merge_with_renamed_protected_migration(self) -> None:
        _root, first_parent = self.fixture.timestamp_base()
        source = "supabase/migrations/20260719080000_base.sql"
        target = "supabase/migrations/20260719090000_renamed.sql"
        self.fixture.git("mv", "--", source, target)
        base = self.fixture.commit("unsafe trusted rename")
        self.fixture.git("switch", "-q", "--detach", first_parent)
        self.fixture.git("merge", "--no-ff", "-m", "sync unsafe rename", base)
        head = self.fixture.git("rev-parse", "HEAD")

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 1, output)
        self.assertIn(source, output)
        self.assertIn(target, output)

    def test_remote_rejects_tree_identical_sync_from_divergent_first_parent(self) -> None:
        root, base = self.fixture.timestamp_base()
        self.fixture.git("switch", "-q", "--detach", root)
        self.fixture.write("README.md", "divergent\n")
        self.fixture.commit("diverge first parent", "README.md")
        self.fixture.git("merge", "--no-ff", "--no-commit", base)
        self.fixture.write("README.md", "fixture\n")
        head = self.fixture.commit("tree-identical divergent merge", "README.md")

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 1, output)
        self.assertRegex(
            output,
            r"commit [0-9a-f]{12} parent [0-9a-f]{12} A: .*20260719080000_base\.sql",
        )

    def _stage_mode(self, mode: str, object_id: str, path: str) -> None:
        self.fixture.git("update-index", "--add", "--cacheinfo", mode, object_id, path)

    def test_remote_rejects_symlink_git_entry(self) -> None:
        _root, base = self.fixture.timestamp_base()
        path = "supabase/migrations/20260719090000_symlink.sql"
        object_id = self.fixture.git(
            "hash-object", "-w", "--stdin", input_text="target"
        )
        self._stage_mode("120000", object_id, path)
        head = self.fixture.commit("symlink")

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 1, output)
        self.assertIn("got 120000 blob", output)

    def test_remote_rejects_gitlink_entry(self) -> None:
        _root, base = self.fixture.timestamp_base()
        path = "supabase/migrations/20260719090000_gitlink.sql"
        self._stage_mode("160000", base, path)
        head = self.fixture.commit("gitlink")

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 1, output)
        self.assertIn("got 160000 commit", output)

    def test_remote_rejects_executable_blob(self) -> None:
        _root, base = self.fixture.timestamp_base()
        path = "supabase/migrations/20260719090000_executable.sql"
        self.fixture.write(path)
        self.fixture.git("add", "--", path)
        self.fixture.git("update-index", "--chmod=+x", "--", path)
        head = self.fixture.commit("executable")

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 1, output)
        self.assertIn("got 100755 blob", output)

    def test_remote_rejects_type_change_of_protected_migration(self) -> None:
        _root, base = self.fixture.timestamp_base()
        path = "supabase/migrations/20260719080000_base.sql"
        object_id = self.fixture.git(
            "hash-object", "-w", "--stdin", input_text="target"
        )
        self._stage_mode("120000", object_id, path)
        head = self.fixture.commit("type-change protected migration")

        result, output = self.fixture.remote_guard(base, head)

        self.assertEqual(result, 1, output)
        self.assertIn(f"final tree changed committed migration: {path}", output)

    def test_worktree_rejects_non_regular_index_entry(self) -> None:
        _root, _base = self.fixture.timestamp_base()
        path = "supabase/migrations/20260719090000_symlink.sql"
        object_id = self.fixture.git(
            "hash-object", "-w", "--stdin", input_text="target"
        )
        self._stage_mode("120000", object_id, path)

        result, output = self.fixture.guard("--worktree")

        self.assertEqual(result, 1, output)
        self.assertIn("got 120000 blob", output)

    def test_worktree_rejects_directory_at_migration_path(self) -> None:
        _root, _base = self.fixture.timestamp_base()
        path = self.fixture.root / "supabase/migrations/20260719090000_directory.sql"
        path.mkdir()

        result, output = self.fixture.guard("--worktree")

        self.assertEqual(result, 1, output)
        self.assertIn("must be a regular file", output)

    def test_worktree_rejects_symlink_when_platform_supports_it(self) -> None:
        _root, _base = self.fixture.timestamp_base()
        link = self.fixture.root / "supabase/migrations/20260719090000_symlink.sql"
        try:
            os.symlink("20260719080000_base.sql", link)
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")

        result, output = self.fixture.guard("--worktree")

        self.assertEqual(result, 1, output)
        self.assertIn("must be a regular file", output)


class LongLivedBranchRulesetTests(unittest.TestCase):
    def test_ruleset_blocks_ref_recreation_deletion_and_force_push(self) -> None:
        ruleset_path = (
            SCRIPT_DIR.parent / "rulesets" / "long-lived-branch-ancestry.json"
        )
        ruleset = json.loads(ruleset_path.read_text(encoding="utf-8"))

        self.assertEqual(ruleset["name"], "protect-long-lived-branch-ancestry")
        self.assertEqual(ruleset["target"], "branch")
        self.assertEqual(ruleset["enforcement"], "active")
        self.assertEqual(ruleset["bypass_actors"], [])
        self.assertEqual(
            ruleset["conditions"]["ref_name"],
            {
                "include": ["refs/heads/main", "refs/heads/develop"],
                "exclude": [],
            },
        )
        self.assertEqual(
            {rule["type"] for rule in ruleset["rules"]},
            {"creation", "deletion", "non_fast_forward"},
        )


class MigrationChecksumTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.migration_dir = self.root / "supabase/migrations"
        self.migration_dir.mkdir(parents=True)
        self.migration = self.migration_dir / "0001_base.sql"
        self.migration.write_text("select 1;\n", encoding="utf-8", newline="\n")
        self.manifest_path = self.root / "supabase/migration-checksums.v1.json"
        self.write_manifest({self.migration.name: self.digest("select 1;\n")})

    @staticmethod
    def digest(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def write_manifest(self, migrations: dict[str, str]) -> None:
        self.manifest_path.write_text(
            json.dumps(
                {
                    "algorithm": "sha256",
                    "canonicalization": "utf-8-lf",
                    "migrations": migrations,
                }
            ),
            encoding="utf-8",
            newline="\n",
        )

    def check(self, paths: list[Path] | None = None) -> list[str]:
        return repository_safety._check_migration_checksums(
            self.root,
            paths if paths is not None else [self.migration],
        )

    def test_valid_manifest_passes(self) -> None:
        self.assertEqual(self.check(), [])

    def test_missing_manifest_entry_is_rejected(self) -> None:
        self.write_manifest({})
        self.assertEqual(
            self.check(),
            ["migration checksum missing: 0001_base.sql"],
        )

    def test_manifest_entry_for_missing_file_is_rejected(self) -> None:
        self.write_manifest(
            {
                self.migration.name: self.digest("select 1;\n"),
                "0002_missing.sql": self.digest("select 2;\n"),
            }
        )
        self.assertEqual(
            self.check(),
            ["migration checksum references missing file: 0002_missing.sql"],
        )

    def test_checksum_drift_is_rejected(self) -> None:
        self.migration.write_text("select 2;\n", encoding="utf-8", newline="\n")
        self.assertEqual(
            self.check(),
            ["historical migration checksum changed: 0001_base.sql"],
        )

    def test_migration_utf8_bom_is_rejected(self) -> None:
        text = "\ufeffselect 1;\n"
        self.migration.write_text(text, encoding="utf-8", newline="\n")
        self.write_manifest({self.migration.name: self.digest(text)})
        self.assertEqual(
            self.check(),
            ["migration contains UTF-8 BOM: 0001_base.sql"],
        )

    def test_migration_symlink_is_rejected_when_platform_supports_it(self) -> None:
        target = self.root / "target.sql"
        target.write_text("select 1;\n", encoding="utf-8", newline="\n")
        self.migration.unlink()
        try:
            os.symlink(target, self.migration)
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")

        self.assertEqual(
            self.check(),
            ["migration must be a regular file: 0001_base.sql"],
        )

    def test_migration_symlink_branch_is_deterministically_rejected(self) -> None:
        class SymlinkPath:
            name = "0001_base.sql"

            @staticmethod
            def is_symlink() -> bool:
                return True

            @staticmethod
            def is_file() -> bool:
                return True

        self.assertEqual(
            self.check([SymlinkPath()]),  # type: ignore[list-item]
            ["migration must be a regular file: 0001_base.sql"],
        )

    def test_manifest_symlink_is_rejected_when_platform_supports_it(self) -> None:
        target = self.root / "manifest.json"
        target.write_text(
            self.manifest_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        self.manifest_path.unlink()
        try:
            os.symlink(target, self.manifest_path)
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")

        self.assertEqual(
            self.check(),
            ["supabase/migration-checksums.v1.json: regular file required"],
        )

    def test_invalid_json_is_rejected(self) -> None:
        self.manifest_path.write_text("{not-json", encoding="utf-8", newline="\n")
        self.assertEqual(
            self.check(),
            ["supabase/migration-checksums.v1.json: invalid UTF-8 JSON"],
        )

    def test_manifest_utf8_bom_is_rejected_as_invalid_json(self) -> None:
        valid_json = self.manifest_path.read_text(encoding="utf-8")
        self.manifest_path.write_text(
            "\ufeff" + valid_json,
            encoding="utf-8",
            newline="\n",
        )
        self.assertEqual(
            self.check(),
            ["supabase/migration-checksums.v1.json: invalid UTF-8 JSON"],
        )


if __name__ == "__main__":
    unittest.main()
