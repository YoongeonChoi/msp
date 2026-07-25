#!/usr/bin/env python3
"""Fail closed when a committed Supabase migration is rewritten.

New migrations must append after the immediately preceding version and remain
regular 100644 blobs. A migration becomes immutable as soon as a commit adds it,
including within the candidate history being reviewed. An all-zero push base is
rejected because an established long-lived ref must never be recreated without
an independently preserved migration boundary. An ancestry-only merge may advance
a long-lived ref when its trusted base is the second parent and the complete tree is
unchanged; this preserves merge ancestry without reclassifying trusted migrations as
new first-parent additions.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
import re
import stat
import subprocess


MIGRATION_PATH = "supabase/migrations"
_FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")
_ZERO_SHA = "0" * 40
_MIGRATION_FILE = re.compile(
    r"supabase/migrations/(?P<version>[0-9]{4}|[0-9]{14})_"
    r"[a-z0-9_]+\.sql"
)


class GuardError(RuntimeError):
    """Raised when the repository or comparison boundary is not trustworthy."""


@dataclass(frozen=True)
class Change:
    status: str
    paths: tuple[str, ...]


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown git error"
        raise GuardError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def _is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    detail = result.stderr.strip() or result.stdout.strip() or "unknown git error"
    raise GuardError(f"cannot establish commit ancestry: {detail}")


def _require_ancestor(repo_root: Path, base: str, head: str) -> None:
    if not _is_ancestor(repo_root, base, head):
        raise GuardError("--base is not an ancestor of --head")


def _repository_root(path: Path) -> Path:
    requested = path.resolve()
    discovered = Path(_git(requested, "rev-parse", "--show-toplevel").strip()).resolve()
    if requested != discovered:
        raise GuardError(
            f"--repo-root must be the Git repository root: expected {discovered}"
        )
    return discovered


def _commit(repo_root: Path, revision: str, label: str) -> str:
    if _FULL_SHA.fullmatch(revision) is None or revision == _ZERO_SHA:
        raise GuardError(f"{label} must be a non-zero full 40-character commit SHA")
    resolved = _git(
        repo_root,
        "rev-parse",
        "--verify",
        f"{revision}^{{commit}}",
    ).strip()
    if resolved.casefold() != revision.casefold():
        raise GuardError(f"{label} did not resolve to the requested commit")
    return resolved


def _parse_name_status(output: str) -> list[Change]:
    tokens = output.split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    changes: list[Change] = []
    index = 0
    while index < len(tokens):
        status = tokens[index]
        index += 1
        path_count = 2 if status[:1] in {"R", "C"} else 1
        if not status or index + path_count > len(tokens):
            raise GuardError("git returned malformed name-status output")
        paths = tuple(tokens[index : index + path_count])
        index += path_count
        changes.append(Change(status=status, paths=paths))
    return changes


def _changes(
    repo_root: Path,
    revisions: Sequence[str],
    *,
    cached: bool = False,
) -> list[Change]:
    cached_flag = ("--cached",) if cached else ()
    output = _git(
        repo_root,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--find-renames=50%",
        "--name-status",
        "-z",
        *cached_flag,
        *revisions,
        "--",
        MIGRATION_PATH,
    )
    return _parse_name_status(output)


def _tree_entries(repo_root: Path, revision: str) -> dict[str, tuple[str, str, str]]:
    output = _git(
        repo_root,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        revision,
        "--",
        MIGRATION_PATH,
    )
    entries: dict[str, tuple[str, str, str]] = {}
    for record in output.split("\0"):
        if not record:
            continue
        try:
            metadata, path = record.split("\t", 1)
            mode, object_type, object_id = metadata.split(" ", 2)
        except ValueError as error:
            raise GuardError("git returned malformed ls-tree output") from error
        entries[path] = (mode, object_type, object_id)
    return entries


def _index_entries(repo_root: Path) -> dict[str, tuple[str, str]]:
    output = _git(
        repo_root,
        "ls-files",
        "--stage",
        "-z",
        "--",
        MIGRATION_PATH,
    )
    entries: dict[str, tuple[str, str]] = {}
    for record in output.split("\0"):
        if not record:
            continue
        try:
            metadata, path = record.split("\t", 1)
            mode, object_id, stage = metadata.split(" ", 2)
        except ValueError as error:
            raise GuardError("git returned malformed index output") from error
        if stage != "0":
            raise GuardError("unmerged migration index entries are present")
        if path in entries:
            raise GuardError(f"git returned a duplicate migration index path: {path}")
        entries[path] = (mode, object_id)
    return entries


def _worktree_paths(repo_root: Path) -> set[str]:
    migration_dir = repo_root / MIGRATION_PATH
    try:
        children = list(migration_dir.iterdir())
    except OSError as error:
        raise GuardError(f"cannot inspect {MIGRATION_PATH}: {error}") from error
    return {
        f"{MIGRATION_PATH}/{child.name}"
        for child in children
    }


def _protected_violations(
    changes: Sequence[Change],
    protected_paths: set[str],
    *,
    boundary: str,
) -> list[str]:
    return [
        f"{boundary} {change.status}: {' -> '.join(change.paths)}"
        for change in changes
        if any(path in protected_paths for path in change.paths)
    ]


def _is_unchanged_trusted_first_parent_addition(
    change: Change,
    trusted_entries: dict[str, tuple[str, str, str]],
    first_parent_entries: dict[str, tuple[str, str, str]],
    commit_entries: dict[str, tuple[str, str, str]],
) -> bool:
    if change.status != "A" or len(change.paths) != 1:
        return False
    path = change.paths[0]
    trusted_entry = trusted_entries.get(path)
    return (
        trusted_entry is not None
        and first_parent_entries.get(path) == trusted_entry
        and commit_entries.get(path) == trusted_entry
    )


def _is_unchanged_trusted_base_addition(
    change: Change,
    trusted_entries: dict[str, tuple[str, str, str]],
    commit_entries: dict[str, tuple[str, str, str]],
) -> bool:
    if change.status != "A" or len(change.paths) != 1:
        return False
    path = change.paths[0]
    trusted_entry = trusted_entries.get(path)
    return trusted_entry is not None and commit_entries.get(path) == trusted_entry


def _is_trusted_noop_sync_merge(
    repo_root: Path,
    *,
    trusted_base: str,
    commit: str,
    parents: Sequence[str],
) -> bool:
    if len(parents) != 2 or parents[1] != trusted_base:
        return False
    first_parent = parents[0]
    if not _is_ancestor(repo_root, first_parent, trusted_base):
        return False
    trusted_tree = _git(repo_root, "rev-parse", f"{trusted_base}^{{tree}}").strip()
    commit_tree = _git(repo_root, "rev-parse", f"{commit}^{{tree}}").strip()
    return commit_tree == trusted_tree


def _is_noncanonical_trusted_noop_sync_merge(
    repo_root: Path,
    *,
    trusted_base: str,
    commit: str,
    parents: Sequence[str],
    canonical: bool,
) -> bool:
    if canonical or len(parents) < 2 or trusted_base not in parents:
        return False
    trusted_tree = _git(repo_root, "rev-parse", f"{trusted_base}^{{tree}}").strip()
    commit_tree = _git(repo_root, "rev-parse", f"{commit}^{{tree}}").strip()
    return commit_tree == trusted_tree


def _invalid_additions(paths: set[str]) -> list[str]:
    return [
        f"new path is not a canonical migration filename: {path}"
        for path in sorted(paths)
        if _MIGRATION_FILE.fullmatch(path) is None
    ]


def _version_paths(
    paths: set[str],
    *,
    trusted_boundary: str | None = None,
) -> dict[str, list[str]]:
    versions: dict[str, list[str]] = {}
    for path in sorted(paths):
        match = _MIGRATION_FILE.fullmatch(path)
        if match is None:
            if trusted_boundary is not None:
                raise GuardError(
                    f"{trusted_boundary} contains a non-canonical migration path: "
                    f"{path}"
                )
            continue
        versions.setdefault(match.group("version"), []).append(path)
    if trusted_boundary is not None:
        duplicates = {
            version: duplicate_paths
            for version, duplicate_paths in versions.items()
            if len(duplicate_paths) > 1
        }
        if duplicates:
            version, duplicate_paths = sorted(duplicates.items())[0]
            raise GuardError(
                f"{trusted_boundary} contains duplicate migration version {version}: "
                + ", ".join(duplicate_paths)
            )
    return versions


def _addition_violations(
    base_paths: set[str],
    new_paths: set[str],
    *,
    boundary: str,
) -> list[str]:
    violations = _invalid_additions(new_paths)
    base_versions = _version_paths(base_paths, trusted_boundary="comparison base")
    new_versions = _version_paths(new_paths)

    for version, paths in sorted(new_versions.items()):
        if len(paths) > 1:
            violations.append(
                f"{boundary}: duplicate new migration version {version}: "
                + ", ".join(paths)
            )
        if version in base_versions:
            violations.append(
                f"{boundary}: new migration version {version} already exists in base: "
                + ", ".join(paths)
            )

    base_legacy = sorted(int(version) for version in base_versions if len(version) == 4)
    base_timestamps = sorted(
        version for version in base_versions if len(version) == 14
    )
    new_legacy = sorted(
        (int(version), version, paths)
        for version, paths in new_versions.items()
        if len(version) == 4
    )
    new_timestamps = sorted(
        (version, paths)
        for version, paths in new_versions.items()
        if len(version) == 14
    )

    if base_legacy and base_timestamps:
        legacy_tail = base_legacy[-1]
        invalid_base_timestamp = next(
            (
                version
                for version in base_timestamps
                if int(version[:4]) <= legacy_tail
            ),
            None,
        )
        if invalid_base_timestamp is not None:
            raise GuardError(
                "comparison base timestamp phase does not sort after legacy tail "
                f"{legacy_tail:04d}: {invalid_base_timestamp}"
            )

    if base_timestamps:
        for _number, version, paths in new_legacy:
            violations.append(
                f"{boundary}: legacy migration {version} cannot be added after the "
                f"timestamp phase began: {', '.join(paths)}"
            )
    else:
        legacy_tail = base_legacy[-1] if base_legacy else 0
        for offset, (number, version, paths) in enumerate(new_legacy, start=1):
            expected = legacy_tail + offset
            if number != expected:
                violations.append(
                    f"{boundary}: legacy migration must append as {expected:04d}, "
                    f"not {version}: {', '.join(paths)}"
                )

    if base_timestamps:
        timestamp_tail = base_timestamps[-1]
        for version, paths in new_timestamps:
            if version <= timestamp_tail:
                violations.append(
                    f"{boundary}: timestamp migration {version} must be newer than "
                    f"base tail {timestamp_tail}: {', '.join(paths)}"
                )
    elif new_timestamps:
        legacy_numbers = list(base_legacy)
        legacy_numbers.extend(number for number, _version, _paths in new_legacy)
        if legacy_numbers:
            legacy_tail = max(legacy_numbers)
            for version, paths in new_timestamps:
                if int(version[:4]) <= legacy_tail:
                    violations.append(
                        f"{boundary}: timestamp migration {version} does not sort "
                        f"after legacy tail {legacy_tail:04d}: {', '.join(paths)}"
                    )
    return violations


def _regular_entry_violations(
    entries: dict[str, tuple[str, str, str]],
    new_paths: set[str],
    *,
    boundary: str,
) -> list[str]:
    violations: list[str] = []
    for path in sorted(new_paths):
        entry = entries.get(path)
        if entry is None:
            violations.append(
                f"{boundary}: new migration is missing from Git tree: {path}"
            )
            continue
        mode, object_type, _object_id = entry
        if mode != "100644" or object_type != "blob":
            violations.append(
                f"{boundary}: new migration must be a regular 100644 blob, "
                f"got {mode} {object_type}: {path}"
            )
    return violations


def _index_entry_violations(
    repo_root: Path,
    entries: dict[str, tuple[str, str]],
    new_paths: set[str],
    *,
    boundary: str,
) -> list[str]:
    violations: list[str] = []
    for path in sorted(new_paths):
        mode, object_id = entries[path]
        object_type = _git(repo_root, "cat-file", "-t", object_id).strip()
        if mode != "100644" or object_type != "blob":
            violations.append(
                f"{boundary}: new migration must be a regular 100644 blob, "
                f"got {mode} {object_type}: {path}"
            )
    return violations


def _worktree_entry_violations(
    repo_root: Path,
    new_paths: set[str],
    *,
    boundary: str,
) -> list[str]:
    violations: list[str] = []
    for path in sorted(new_paths):
        file_path = repo_root / Path(path)
        try:
            metadata = file_path.lstat()
        except OSError as error:
            violations.append(
                f"{boundary}: cannot inspect new migration {path}: {error}"
            )
            continue
        if not stat.S_ISREG(metadata.st_mode):
            violations.append(
                f"{boundary}: new migration must be a regular file: {path}"
            )
        elif metadata.st_mode & 0o111:
            violations.append(
                f"{boundary}: new migration must not have executable bits: {path}"
            )
    return violations


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Allow only additive changes to committed Supabase migrations."
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--worktree",
        action="store_true",
        help="compare the index and working tree with the current HEAD",
    )
    parser.add_argument("--base", help="exact base commit SHA")
    parser.add_argument("--head", help="exact head commit SHA")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        repo_root = _repository_root(args.repo_root)
        if args.worktree:
            if args.base is not None or args.head is not None:
                raise GuardError("--worktree cannot be combined with --base or --head")
            head = _commit(
                repo_root,
                _git(repo_root, "rev-parse", "HEAD").strip(),
                "HEAD",
            )
            head_entries = _tree_entries(repo_root, head)
            protected = set(head_entries)
            _version_paths(protected, trusted_boundary="HEAD")
            if _git(repo_root, "ls-files", "-u", "--", MIGRATION_PATH).strip():
                raise GuardError("unmerged migration index entries are present")
            cached_changes = _changes(repo_root, (head,), cached=True)
            worktree_changes = _changes(repo_root, ())
            violations = _protected_violations(
                cached_changes,
                protected,
                boundary="HEAD..index",
            )
            violations.extend(
                _protected_violations(
                    worktree_changes,
                    protected,
                    boundary="index..worktree",
                )
            )
            index_entries = _index_entries(repo_root)
            index_new_paths = set(index_entries) - protected
            violations.extend(
                _addition_violations(
                    protected,
                    index_new_paths,
                    boundary="HEAD..index",
                )
            )
            violations.extend(
                _index_entry_violations(
                    repo_root,
                    index_entries,
                    index_new_paths,
                    boundary="HEAD..index",
                )
            )

            worktree_paths = _worktree_paths(repo_root)
            worktree_new_paths = worktree_paths - protected
            violations.extend(
                _addition_violations(
                    protected,
                    worktree_new_paths,
                    boundary="HEAD..worktree",
                )
            )
            violations.extend(
                _worktree_entry_violations(
                    repo_root,
                    worktree_new_paths,
                    boundary="HEAD..worktree",
                )
            )
            index_paths = set(index_entries)
            index_versions = _version_paths(index_paths)
            if (
                not _invalid_additions(index_paths)
                and all(len(paths) == 1 for paths in index_versions.values())
            ):
                violations.extend(
                    _addition_violations(
                        index_paths,
                        worktree_paths - index_paths,
                        boundary="index..worktree",
                    )
                )
            new_paths = index_new_paths | worktree_new_paths
            boundary = "HEAD..worktree"
        else:
            if args.base is None or args.head is None:
                raise GuardError("provide --worktree or both --base and --head")
            if args.base == _ZERO_SHA:
                raise GuardError(
                    "all-zero --base is not a trusted migration boundary; "
                    "protected long-lived refs must not be created or recreated"
                )
            base = _commit(repo_root, args.base, "--base")
            head = _commit(repo_root, args.head, "--head")
            _require_ancestor(repo_root, base, head)
            base_entries = _tree_entries(repo_root, base)
            head_entries = _tree_entries(repo_root, head)
            protected = set(base_entries)
            _version_paths(protected, trusted_boundary="comparison base")
            violations = [
                f"final tree changed committed migration: {path}"
                for path, entry in sorted(base_entries.items())
                if head_entries.get(path) != entry
            ]
            new_paths = set(head_entries) - protected
            violations.extend(
                _addition_violations(
                    protected,
                    new_paths,
                    boundary="final tree",
                )
            )
            violations.extend(
                _regular_entry_violations(
                    head_entries,
                    new_paths,
                    boundary="final tree",
                )
            )
            commit_lines = _git(
                repo_root,
                "rev-list",
                "--reverse",
                "--topo-order",
                "--parents",
                head,
                "--not",
                base,
            ).splitlines()
            for commit_line in commit_lines:
                commit_and_parents = commit_line.split()
                if len(commit_and_parents) < 2:
                    raise GuardError("candidate history contains a parentless commit")
                commit, *parents = commit_and_parents
                first_parent = parents[0]
                commit_boundary = f"commit {commit[:12]}"
                parent_entries = {
                    parent: _tree_entries(repo_root, parent)
                    for parent in parents
                }
                first_parent_entries = parent_entries[first_parent]
                first_parent_paths = set(first_parent_entries)
                commit_entries = _tree_entries(repo_root, commit)
                trusted_noop_sync_merge = _is_trusted_noop_sync_merge(
                    repo_root,
                    trusted_base=base,
                    commit=commit,
                    parents=parents,
                )
                if _is_noncanonical_trusted_noop_sync_merge(
                    repo_root,
                    trusted_base=base,
                    commit=commit,
                    parents=parents,
                    canonical=trusted_noop_sync_merge,
                ):
                    violations.append(
                        f"{commit_boundary} ancestry-only sync merge must use exactly "
                        "two parents with the trusted base as its second parent"
                    )
                for parent, entries in parent_entries.items():
                    parent_boundary = (
                        commit_boundary
                        if len(parents) == 1
                        else f"{commit_boundary} parent {parent[:12]}"
                    )
                    parent_changes = _changes(repo_root, (parent, commit))
                    if parent != first_parent:
                        # A PR merge may carry a migration added only on its trusted
                        # first parent into an older feature parent. It is not a
                        # rewrite when the trusted, first-parent, and merge entries
                        # are byte-for-byte identical.
                        parent_changes = [
                            change
                            for change in parent_changes
                            if not _is_unchanged_trusted_first_parent_addition(
                                change,
                                base_entries,
                                first_parent_entries,
                                commit_entries,
                            )
                        ]
                    elif trusted_noop_sync_merge:
                        # GitHub records a reviewed develop-to-main integration with
                        # main as the first parent. Fast-forwarding develop to that
                        # exact, tree-identical merge commit must not make migrations
                        # already protected by develop look new relative to old main.
                        parent_changes = [
                            change
                            for change in parent_changes
                            if not _is_unchanged_trusted_base_addition(
                                change,
                                base_entries,
                                commit_entries,
                            )
                        ]
                    violations.extend(
                        _protected_violations(
                            parent_changes,
                            protected | set(entries),
                            boundary=parent_boundary,
                        )
                    )
                commit_new_paths = set(commit_entries) - protected
                violations.extend(
                    _addition_violations(
                        first_parent_paths,
                        set(commit_entries) - first_parent_paths,
                        boundary=f"{commit_boundary} first-parent append",
                    )
                )
                violations.extend(
                    _addition_violations(
                        protected,
                        commit_new_paths,
                        boundary=commit_boundary,
                    )
                )
                violations.extend(
                    _regular_entry_violations(
                        commit_entries,
                        commit_new_paths,
                        boundary=commit_boundary,
                    )
                )
            boundary = f"{base[:12]}..{head[:12]}"

        if violations:
            print(
                "::error::Committed migrations are immutable; "
                "add a new migration instead."
            )
            print("\n".join(violations))
            return 1
        print(
            "Migration history guard passed: "
            f"{boundary} (new migrations: {len(new_paths)})"
        )
        return 0
    except (GuardError, OSError) as error:
        print(
            "::error::Migration history guard could not establish a safe boundary: "
            f"{error}"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
