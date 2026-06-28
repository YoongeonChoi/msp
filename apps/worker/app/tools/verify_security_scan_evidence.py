from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

from app.tools.verify_live_readiness_evidence_bundle import (
    BundleValidationError,
    SecurityScanEvidenceSummary,
    _join_errors,
    _security_source_binding_exclusions,
    _validate_security_scan_report_file_hash,
    _validate_security_scan_source_binding,
    verify_security_scan_evidence_parts,
)


class SecurityScanEvidenceValidationError(ValueError):
    pass


RemoteReportFetcher = Callable[[str, int], bytes]
REMOTE_REPORT_TIMEOUT_SECONDS = 10
MAX_REMOTE_REPORT_BYTES = 2_000_000


def verify_security_scan_evidence_file(
    path: Path,
    *,
    repo_root: Path | None = None,
    verify_remote_report_uri: bool = False,
    remote_fetcher: RemoteReportFetcher | None = None,
    remote_timeout_seconds: int = REMOTE_REPORT_TIMEOUT_SECONDS,
) -> SecurityScanEvidenceSummary:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SecurityScanEvidenceValidationError(
            "security_scan_evidence_unreadable"
        ) from exc
    except json.JSONDecodeError as exc:
        raise SecurityScanEvidenceValidationError(
            "security_scan_evidence_json_invalid"
        ) from exc
    if not isinstance(payload, Mapping):
        raise SecurityScanEvidenceValidationError("security_scan_evidence_must_be_object")
    evidence = cast(Mapping[str, object], payload)
    summary = verify_security_scan_evidence(evidence)
    verify_security_scan_report_file_hash(evidence, evidence_path=path)
    if repo_root is not None:
        verify_security_scan_report_uri_repo_artifact(evidence, repo_root=repo_root)
        verify_security_scan_source_binding(evidence, repo_root=repo_root, evidence_path=path)
    if verify_remote_report_uri:
        verify_security_scan_report_uri_remote_fetch(
            evidence,
            fetcher=remote_fetcher,
            timeout_seconds=remote_timeout_seconds,
        )
    return summary


def verify_security_scan_evidence(
    evidence: Mapping[str, object],
) -> SecurityScanEvidenceSummary:
    try:
        return verify_security_scan_evidence_parts(evidence)
    except BundleValidationError as exc:
        raise SecurityScanEvidenceValidationError(str(exc)) from exc


def verify_security_scan_report_file_hash(
    evidence: Mapping[str, object],
    *,
    evidence_path: Path | None = None,
) -> None:
    errors: list[str] = []
    _validate_security_scan_report_file_hash(
        evidence,
        base_dir=evidence_path.parent if evidence_path is not None else Path.cwd(),
        path="security_scan_evidence",
        errors=errors,
    )
    if errors:
        raise SecurityScanEvidenceValidationError(
            _join_errors(errors),
        )


def verify_security_scan_report_uri_repo_artifact(
    evidence: Mapping[str, object],
    *,
    repo_root: Path,
) -> None:
    errors: list[str] = []
    _validate_security_scan_report_uri_repo_artifact(
        evidence,
        repo_root=repo_root,
        path="security_scan_evidence",
        errors=errors,
    )
    if errors:
        raise SecurityScanEvidenceValidationError(_join_errors(errors))


def verify_security_scan_source_binding(
    evidence: Mapping[str, object],
    *,
    repo_root: Path,
    evidence_path: Path,
) -> None:
    errors: list[str] = []
    _validate_security_scan_source_binding(
        evidence,
        repo_root=repo_root,
        excluded_paths=_security_source_binding_exclusions(evidence_path, evidence),
        path="security_scan_evidence",
        errors=errors,
    )
    if errors:
        raise SecurityScanEvidenceValidationError(_join_errors(errors))


def verify_security_scan_report_uri_remote_fetch(
    evidence: Mapping[str, object],
    *,
    fetcher: RemoteReportFetcher | None = None,
    timeout_seconds: int = REMOTE_REPORT_TIMEOUT_SECONDS,
) -> None:
    errors: list[str] = []
    _validate_security_scan_report_uri_remote_fetch(
        evidence,
        path="security_scan_evidence",
        errors=errors,
        fetcher=fetcher or _default_remote_report_fetcher,
        timeout_seconds=timeout_seconds,
    )
    if errors:
        raise SecurityScanEvidenceValidationError(_join_errors(errors))


def _validate_security_scan_report_uri_repo_artifact(
    evidence: Mapping[str, object],
    *,
    repo_root: Path,
    path: str,
    errors: list[str],
) -> None:
    report_uri = evidence.get("report_uri")
    report_sha256 = evidence.get("report_sha256")
    if not isinstance(report_uri, str) or not isinstance(report_sha256, str):
        return

    parts = urlsplit(report_uri)
    host = parts.hostname.rstrip(".").casefold() if parts.hostname else ""
    if host not in {"github.com", "www.github.com"}:
        return

    repo_artifact_path = _github_report_uri_repo_path(
        parts.path,
        repo_root=repo_root,
        path=path,
        errors=errors,
    )
    if repo_artifact_path is None:
        return

    try:
        actual_sha256 = hashlib.sha256(repo_artifact_path.read_bytes()).hexdigest()
    except OSError:
        errors.append(f"{path}.report_uri_github_artifact_unreadable")
        return

    if actual_sha256 != report_sha256.lower():
        errors.append(f"{path}.report_uri_github_artifact_sha256_mismatch")


def _validate_security_scan_report_uri_remote_fetch(
    evidence: Mapping[str, object],
    *,
    path: str,
    errors: list[str],
    fetcher: RemoteReportFetcher,
    timeout_seconds: int,
) -> None:
    report_uri = evidence.get("report_uri")
    report_sha256 = evidence.get("report_sha256")
    if not isinstance(report_uri, str) or not isinstance(report_sha256, str):
        return

    if _github_report_uri_is_blob_page(report_uri):
        errors.append(f"{path}.report_uri_remote_must_reference_raw_report_bytes")
        return

    try:
        body = fetcher(report_uri, timeout_seconds)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        errors.append(f"{path}.report_uri_remote_fetch_failed")
        return

    if len(body) > MAX_REMOTE_REPORT_BYTES:
        errors.append(f"{path}.report_uri_remote_report_too_large")
        return

    actual_sha256 = hashlib.sha256(body).hexdigest()
    if actual_sha256 != report_sha256.lower():
        errors.append(f"{path}.report_uri_remote_sha256_mismatch")


def _github_report_uri_is_blob_page(report_uri: str) -> bool:
    parts = urlsplit(report_uri)
    host = parts.hostname.rstrip(".").casefold() if parts.hostname else ""
    if host not in {"github.com", "www.github.com"}:
        return False
    path_parts = [unquote(part) for part in parts.path.split("/") if part]
    return len(path_parts) >= 5 and path_parts[2] == "blob"


def _default_remote_report_fetcher(report_uri: str, timeout_seconds: int) -> bytes:
    request = Request(
        report_uri,
        headers={"User-Agent": "kr-auto-trading-lab-live-readiness-verifier"},
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        return cast(bytes, response.read(MAX_REMOTE_REPORT_BYTES + 1))


def _github_report_uri_repo_path(
    uri_path: str,
    *,
    repo_root: Path,
    path: str,
    errors: list[str],
) -> Path | None:
    parts = [unquote(part) for part in uri_path.split("/") if part]
    if len(parts) < 5 or parts[2] not in {"blob", "raw"}:
        errors.append(f"{path}.report_uri_github_artifact_must_reference_repo_blob")
        return None

    artifact_segments = parts[4:]
    if _has_unsafe_repo_artifact_segments(artifact_segments):
        errors.append(f"{path}.report_uri_github_artifact_must_reference_repo_file")
        return None

    relative_artifact = PurePosixPath(*artifact_segments)
    repo_base = repo_root.resolve(strict=False)
    repo_artifact = (repo_base / Path(*relative_artifact.parts)).resolve(strict=False)
    try:
        repo_artifact.relative_to(repo_base)
    except ValueError:
        errors.append(f"{path}.report_uri_github_artifact_must_stay_under_repo")
        return None

    if not repo_artifact.is_file():
        errors.append(f"{path}.report_uri_github_artifact_unreadable")
        return None
    return repo_artifact


def _has_unsafe_repo_artifact_segments(segments: Sequence[str]) -> bool:
    if not segments:
        return True
    return any(
        segment in {"", ".", ".."} or "/" in segment or "\\" in segment
        for segment in segments
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate retained independent Codex Security scan evidence."
    )
    parser.add_argument(
        "--evidence",
        required=True,
        type=Path,
        help="Path to retained security_scan_summary JSON.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Optional Git repository root used to verify source_head and source_diff_sha256.",
    )
    parser.add_argument(
        "--verify-remote-report-uri",
        action="store_true",
        help=(
            "Fetch report_uri over HTTPS and require the downloaded bytes to "
            "match report_sha256. Use this after publishing release evidence."
        ),
    )
    args = parser.parse_args(argv)

    try:
        summary = verify_security_scan_evidence_file(
            args.evidence,
            repo_root=args.repo_root,
            verify_remote_report_uri=args.verify_remote_report_uri,
        )
    except SecurityScanEvidenceValidationError as exc:
        print(f"FINAL=FAIL security_scan_evidence reason={_safe_reason(str(exc))}")
        return 1

    print(
        "FINAL=PASS security_scan_evidence "
        f"scan_id={summary.scan_id} "
        f"worklist_rows={summary.worklist_rows} "
        f"completion_receipts={summary.completion_receipts} "
        f"candidate_findings={summary.candidate_findings} "
        f"validation_receipts={summary.validation_receipts} "
        f"attack_path_receipts={summary.attack_path_receipts} "
        f"report_uri={summary.report_uri}"
    )
    return 0


def _safe_reason(reason: str) -> str:
    return reason.replace("\r", " ").replace("\n", " ")[:700]


if __name__ == "__main__":
    raise SystemExit(main())
