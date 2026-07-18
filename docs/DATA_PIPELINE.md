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
date ordering or timestamps. This evidence is not persisted and does not by
itself certify a completed candle or make data feature-ready.

A pure domain timing gate can combine one daily candle with one open-session
calendar revision only when the candle event's KST date matches that session
date and both source observations were made at or after the next business
day's regular start. The resulting PIT evidence records the later source
observation as its availability time and binds both independently pinned
provider contracts. It is not wired to persistence or features and does not
claim provider finality, immutability, completeness, corporate-action safety,
or overall DQ approval.

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
is complete, durable, authentic, provider-final, corporate-action safe,
DQ-approved, feature-ready, or authorized for research promotion or order
execution. It also does not certify calendar-revision finality or quarantine a
calendar A-to-B-to-A recurrence. SHA-256 values are integrity and lineage
checks, not provider signatures.

Append-only candle revision semantics are defined behind a dedicated storage
port and an in-memory reference adapter. This adapter is not durable and is not
wired into collection or feature calculation. A repeated latest hash is an
idempotent replay; a new hash requires a strictly later observation time and
creates a revision. Reappearance of an older historical hash is rejected as
ambiguous rather than guessed to be a replay or provider reversion; no durable
quarantine exists yet.

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
