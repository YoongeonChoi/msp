# Codex Security Diff Scan: release_freshness_20260630140851

## Summary

- Result: no reportable findings.
- Scan profile: `security_diff_scan`.
- Scope: local patch against `HEAD`.
- Worklist rows: 3.
- Candidate findings: 0.
- Validation receipts: 0.
- Attack-path receipts: 0.

## Reviewed Surfaces

- `apps/worker/app/tools/verify_worker_release_freshness.py`
- `apps/worker/app/tools/collect_live_readiness_evidence_bundle.py`
- `apps/worker/app/tools/verify_live_readiness_evidence_bundle.py`

## Security Review Notes

The new worker release freshness verifier is read-only. It fetches only the
latest `worker_heartbeats` row from hosted Supabase and compares
`details.release_sha` with the expected Git commit. The service-role key is used
only in request headers and is not included in success or failure output.

The CLI output is intentionally bounded to a single `FINAL` line. It exposes
only short commit prefixes, heartbeat age, max age, and fixed reason codes. Full
commit hashes, Supabase URLs, response bodies, headers, and secret values are
not printed.

The bundle integration adds `worker_release_freshness` as a required local
check. The collector requires the check command to exit `0`, extracts one exact
`FINAL=PASS worker_release_freshness` line, and rejects side-channel output.
The bundle verifier also rejects missing metrics, duplicate or unknown metrics,
non-12-hex short SHAs, observed/expected SHA mismatch, nonpositive max age, and
heartbeat age greater than max age.

The change does not add broker calls, desktop broker/order API calls, live order
execution, or any bypass around `RiskService` or `ExecutionService`.

## Phase Receipts

- Threat model receipt: complete.
- Finding discovery receipt: complete.
- Deep review completion receipts: 3 of 3 worklist rows.
- No technically plausible candidate findings were promoted, so validation and
  attack-path counts are both zero by closure.

## Conclusion

No security regression was identified in the diff-scoped review. The new gate is
fail-closed for missing, stale, mismatched, invalid, or future hosted worker
heartbeat data, and it improves release evidence by preventing a running but
stale Render worker from being treated as current deployment proof.
