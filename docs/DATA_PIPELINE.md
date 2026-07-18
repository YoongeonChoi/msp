# Data Pipeline

Toss: account, position, quote, candle, order contracts are represented as ports.
The candle path exposes an explicit, bounded single-page read through
`DataCollectionService`; it does not persist observations, follow pagination,
schedule collection, certify a completed bar, calculate a feature, or authorize
an order.

The Toss KR market-calendar adapter can map one explicitly requested date into
strict point-in-time session evidence. It preserves a closed day without
current regular hours, requires the next business day's KST regular session,
binds the evidence to a pinned OpenAPI artifact hash, and rejects inconsistent
date ordering or timestamps. The pure adapter does not persist the result; an
explicit Worker-only persistence adapter can submit one canonical calendar
observation together with its timing evidence. Neither path by itself certifies
a completed candle or makes data feature-ready.

A pure domain timing gate can combine one daily candle with one open-session
calendar revision only when the candle event's KST date matches that session
date and both source observations were made at or after the next business
day's regular start. The resulting PIT evidence records the later source
observation as its availability time and binds both independently pinned
provider contracts. The domain gate itself remains pure and is not wired to
features. Persistence of an explicitly submitted result does not claim provider
finality, collection completeness, corporate-action safety, or overall DQ
approval.

The pure daily-candle as-of selector keeps that timing evidence attached to
the selected candle. It admits a candidate only when
`evidence_available_at <= as_of`, while ordering eligible price corrections by
the candle observation clock. This separation prevents a late calendar proof
for an older candle from rolling back a newer correction. Every source is
revalidated and exact-bound before filtering; same-clock price conflicts,
historical hash recurrence, and multiple candle identities for one daily key
fail closed. The selector is deterministic across input order and equivalent
timezone representations.

This selector operates only on one exact built-in list or tuple supplied by the
caller, and "selected" means latest only within that canonical candidate
snapshot. Its result type cannot be directly constructed to bypass the
snapshot-wide ambiguity checks. The selector does not prove that the snapshot
is complete, authentic, provider-final, corporate-action safe,
DQ-approved, feature-ready, or authorized for research promotion or order
execution. It is not yet backed by a durable as-of query source. SHA-256 values
are integrity and lineage checks, not provider signatures.

Append-only candle revision semantics are defined behind a dedicated storage
port. The in-memory reference adapter and the explicit Supabase Worker adapter
implement the same receipt contract. The Supabase path uses one service-role-
only RPC to serialize each logical candle, recompute the Python canonical
identity and observation hashes in PostgreSQL, and persist immutable revisions.
A repeated latest hash is an idempotent replay; a new hash requires a strictly
later observation time and creates the next database-assigned revision.
Reappearance of an older historical hash, a same-clock price conflict, or an
observation-time regression returns a committed quarantine receipt before the
Worker adapter raises a fail-closed domain error. Quarantine rows and accepted
revisions are append-only, while direct table access is denied to runtime roles.

The daily-candle timing store adds an append-only calendar content ledger and
separate append-only candle/calendar observation-occurrence ledgers. Content is
deduplicated without discarding the fact that unchanged content was observed at
a later time. Each timing revision has composite foreign keys to the exact
immutable content revisions and exact occurrence rows used to derive it. One
service-role-only RPC recomputes calendar identity/content hashes, timing
identity/content hashes, and `evidence_available_at` from those stored source
occurrences. It never accepts a caller-provided hash or availability clock as
authority. Request retries return the original durable receipt; source-clock
regression, same-clock conflict, historical hash recurrence, request-key reuse,
a missing exact source occurrence, or a forged binding produces a durable
quarantine receipt and no accepted timing row. Runtime roles have no direct
table access.

Calendar evidence and timing evidence have separate acceptance semantics. A
valid calendar correction may be committed before a later timing-stream guard
quarantines the timing candidate. The current Worker adapter still raises a
fail-closed timing error and does not expose that partial receipt as structured
telemetry; database receipts remain the audit source until runtime wiring adds
that observability.

The explicit candle and timing Supabase adapters are not wired into collection,
the runtime container, a durable as-of reader, or feature calculation. The
stores prove only the behavior of observations submitted to their RPCs. A later
exact re-observation of the latest unchanged content now creates an immutable
occurrence without creating a duplicate content revision, and timing binds that
exact occurrence rather than a mutable stream-head clock. A pre-migration
database can recover each content revision's original occurrence and the latest
candle head observation, but intermediate unchanged observations that were
never stored cannot be reconstructed. A request key that already has a durable
quarantine receipt keeps replaying that receipt; callers must use a new request
key for a newly observed event. These contracts do not prove collection
completeness, provider authenticity or finality, corporate-action safety, DQ
approval, or feature/research/order readiness. An unresolved quarantine is
evidence of an ambiguity, not an automated resolution or a promotion decision.

KRX: market calendar/listing/statistics are adapter placeholders and mock data in local mode.

OpenDART: financial statement ingestion requires corp code and account-name mapping verification. Canonical fields are defined in `domain/fundamentals/value_objects.py`.

Naver: news search stores title, source, published time, compact summary/classification, and hashes for deduplication. Full article scraping is out of MVP scope.

OpenAI: structured outputs classify news/disclosures and propose monthly strategy candidates. Inputs must be sanitized and data-minimized.

Feature storage uses typed columns for queryable fields and JSONB only for snapshots.

## Outcome Tracking

`python -m app.tools.update_outcomes_once` builds paper-trading outcomes from stored database facts only:

- `decision_snapshots` provides `symbol`, `action`, decision time, and `feature_snapshot.price_at_decision`.
- `features_daily` provides verified cached future close prices by trading date.
- `orders` provides linked paper order amount, price, and quantity when available.
- `outcomes` is upserted by `decision_id`, so reruns update the same row instead of creating duplicates.

The command does not call Toss order APIs, does not create orders, does not create decision snapshots, and does not send account data to OpenAI. Non-trading days are handled by using the next available `features_daily.trade_date` rows rather than calendar-day interpolation.

Outcome fields:

- `return_1d`, `return_5d`, `return_20d`
- `max_drawdown_20d`
- `hit_target`, `hit_stop`
- `realized_pnl_krw`
- `outcome_status`: `pending`, `partial`, `complete`, or `skipped`

`return_pct` and `pnl_krw` remain populated from the 20-day outcome for compatibility with monthly research summaries.
