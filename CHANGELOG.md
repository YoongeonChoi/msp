# Changelog

This file records notable user, operator, and safety-boundary changes. Versioning follows
[Release Process](docs/RELEASE_PROCESS.md); `v0.x.y` denotes the Paper qualification
phase and never grants Production Live authority.

## [0.1.0] - 2026-07-26

### Added

- Safety-first Worker, Supabase control plane, and Tauri operations cockpit baseline.
- Paper V2 execution, balanced accounting, reconciliation, incident, and outbox flows.
- PIT candle/calendar evidence and strict provider/persistence boundaries.
- Sealed DB-clock durable scheduler with lease/fencing, bounded retry, dead-letter,
  reason-bound replay, heartbeat, and restart-safe lifecycle.
- Evidence-bound 67-ID QA checklist and exact-source verification receipts.

### Changed

- Replaced the legacy per-stage in-memory operations loop with the durable scheduler.
- Bound scheduler effects to exact runtime provenance, release, fencing, and deadlines.
- Published the current engineering QA baseline of `92.90 / 100` after reviewed
  scorecard integration; Production Live remains `NO-GO`.

### Safety

- No live broker order-write network path is included.
- Render automatic deployment and V2 runtime activation remain disabled.
- Deployment-pause evidence and bounded fairness remain activation blockers.

See [detailed v0.1.0 release notes](docs/releases/v0.1.0.md) for migrations, verification,
rollback, security posture, and known limitations.

[0.1.0]: https://github.com/YoongeonChoi/msp/releases/tag/v0.1.0
