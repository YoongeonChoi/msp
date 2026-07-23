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
snapshot-wide ambiguity checks. A dedicated Supabase reader can now assemble
that candidate snapshot from retained immutable rows, but neither component
proves that the source history is complete, authentic, provider-final,
corporate-action safe, DQ-approved, feature-ready, or authorized for research
promotion or order execution. SHA-256 values are integrity and lineage checks,
not provider signatures.

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

The daily-candle collection job store adds a separate, default-unused fence
around one explicit candle request. Its immutable spec binds one UUIDv4 job,
provider, KR symbol, `1d` interval, adjusted flag, inclusive `before` clock,
pinned provider-contract SHA-256, `count=1`, no pagination, the `manual`
trigger, and no automatic retry. The intended future collector protocol calls
CAS begin before a provider read and fences the canonical candidate before an
append. The store durably records those transitions, atomically assigns the
fencing revision, and rejects stale job-state progress. It cannot yet enforce
the I/O order or stop an out-of-band provider/append call because the existing
ports carry no job or fence and no collector is wired. Every mutation after
begin rebinds the spec SHA-256, expected revision, attempt, holder, and fencing
revision.

Only the exact proven pre-append reason may release a job-store attempt into
`paused_retryable`. A candidate-fenced or `blocked_unknown` job has no
job-store TTL takeover or automatic retry transition. Completion does not trust the append
response's `inserted` flag as durable authority: the database joins the exact
candidate occurrence by identity, content hash, observation time, and payload,
then joins its immutable content revision to the returned revision and stored
observation clock. This permits a valid later unchanged occurrence to bind an
older content revision without confusing the two clocks. The completion owns
the database-confirmed occurrence and content-revision UUIDs.

The in-memory implementation proves only lock/CAS state semantics and loses all
state on restart. The Supabase implementation stores private RLS-protected job
state and an append-only attempt-event ledger behind service-role-only Worker
RPCs. Neither adapter is selected by the normal runtime, scheduler, Desktop,
features, backtests, strategies, or order paths. This store is a prerequisite
for a later single-candle collector; it does not itself call the provider or
append a candle.

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

`20260719040000_pit_calendar_observation_store.sql` adds a separate Worker-only
calendar observation RPC over the same append-only calendar content and
occurrence ledgers. It accepts one canonically validated KR session observation,
including both open and closed dates, and recomputes the calendar identity,
evidence hash, canonical timestamps, and KST session geometry in PostgreSQL.
The Worker adapter requires identity response encoding, rejects duplicate JSON
keys, and stops reading after 64 KiB before exposing any receipt.
An exact occurrence replay returns the existing receipt, a later unchanged
observation adds only an occurrence, and a strictly later correction adds one
content revision plus its occurrence. A regressed source clock, same-clock
content conflict, or historical content-hash recurrence is durably quarantined
and the Worker adapter fails closed. The RPC is service-role-only and direct
table access remains denied.

`CollectKrDailySessionObservation` is the explicit single-date application
boundary between a `KrDailySessionSourcePort` and that observation store. One
execution validates the exact date before I/O, performs one source fetch, binds
the source `observed_at` to the local read window, canonicalizes the evidence,
and performs one append. It then rebinds the durable receipt to the same
calendar identity, evidence hash, and observation time. Source or store failure
is returned as a fixed fail-closed error without an internal retry or upstream
payload details. Its structured `write_outcome` is `not_attempted` only before
the observation-store boundary and `unknown` after append is entered or its
receipt cannot be trusted. The optional evidence-returning method owns canonical
copies of both the session and receipt. Once append has been attempted, callers
must inspect durable receipts/evidence before an explicit rerun and must not
blind-retry the operation.

`RunKrCalendarDateRangeCollectionJob` defines a bounded manual range-job
contract over that single-date collector. A job covers one exact provider, the
`KR` market, and 1..366 inclusive dates. Execution is disabled by default and
requires the exact `manual` trigger embedded in the canonical job spec. One
explicit invocation records an attempt fence before collection and advances at
most one date. Every transition is bound to the job spec SHA, expected revision,
opaque UUIDv4 attempt and holder IDs, and exact target date. Confirmed checkpoints
form a continuous prefix from the requested start date, own the canonical source
evidence and receipt, retain the attempt fence/times, and produce a terminal
SHA-256 manifest only after the entire range is confirmed.

The `manual` trigger is an application scheduling boundary, not proof of user
identity, MFA, maker/checker approval, or operational authorization.

Only a proven pre-write failure can move the date to `paused_retryable`, and a
new explicit manual invocation is still required. An unknown write outcome or
unexpected failure moves the attempt to `blocked_unknown` when that transition
can be confirmed. Cancellation, process loss, or a lost transition response does
not itself authorize a retry: the next explicit invocation must follow the
reloaded job state. A committed confirm advances the durable checkpoint and
releases that fence; an uncommitted or still-unknown confirm leaves the attempt
active. Any active fence is never reclaimed by TTL or used for a blind retry.
The in-memory adapter validates these CAS and concurrency semantics but is test
reference state only: it is not restart-durable and is intentionally absent from
the runtime container. The explicit Supabase adapter persists the canonical job
snapshot and append-only attempt ledger through service-role-only RPCs. Database
CAS binds every mutation to the spec SHA, revision, attempt, holder, and target;
restart/reconnect, concurrent begin, stale-write, blocked-attempt, terminal
manifest, ACL/RLS, and zero-order-write behavior are exercised in a disposable
PostgreSQL verifier. It has no TTL takeover or automatic retry. The adapter is
selected only by a separate, default-disabled one-shot command. It is not
selected by the normal trading runtime or any scheduler.

`KrCalendarCollectionRecoveryAssessmentService` is a separate read-only
classification boundary over one `KrCalendarCollectionJobInspectorPort` read.
It rebinds the inspected snapshot to the caller's exact canonical spec and spec
SHA before classifying `missing`, `ready`, recognized `paused_retryable`,
`paused_unrecognized`, `collecting`, `blocked_unknown`, or `completed`. Only the
exact `collection_failed_before_write` pause reason is a retry candidate. Its
`recommended_operator_action` describes
only the next review question. Mutation, retry, manual recovery, manual
execution, and Production Live authorization remain false; an in-flight or
unknown write outcome is never converted into a retry. A service-role-only
Worker RPC and the Supabase job-store adapter implement the durable inspection
read. `app.tools.assess_kr_calendar_collection_job` exposes that classification
through a separate default-disabled, read-only command and emits exact execution
preconditions only for executable states. `app.tools.run_kr_calendar_collection_job_once`
reassesses before mutation. It is disabled unless the dedicated assessment and
manual settings plus command-line confirmation are present, and binds execution
to the reviewed spec SHA, state, state reason, revision, confirmed count, and
next date. A recognized `paused_retryable` state requires the exact reason and
an additional reviewed-retry confirmation. `paused_unrecognized`, `collecting`,
and `blocked_unknown` stop before provider collection, and a race after assessment
fails the exact revision fence instead of advancing a newly exposed date. One
process invocation can confirm at most one date and emits the durable attempt,
holder, fencing, observation, and receipt identities for the processed
checkpoint.

This command is not wired into the normal runtime container, scheduler,
automatic range backfill, Desktop, timing, features, backtests, strategy, or
orders. It does not reconcile an in-flight or unknown write outcome, and it
does not retry automatically. Preserving one open- or closed-day observation
is source evidence, not proof of a complete KRX calendar, provider authenticity
or finality, corporate-action safety, DQ approval, dataset certification, or
research/order authorization.

`20260719050000_pit_calendar_as_of_reader.sql` adds the independent Worker-only
`worker_api.list_pit_kr_daily_sessions_as_of_v1` read boundary for both open and
closed retained calendar evidence. One query is limited to one exact provider,
the `KR` market, an inclusive range of at most 366 calendar days, page size
`25..100`, and no more than 1,000 raw candidates. Eligibility is determined by
the source-semantic `occurrence.observed_at <= as_of` cutoff. `received_at`
remains immutable lineage and cannot be used to reconstruct which transaction
was committed or visible in the database at a past instant. The migration also
converges the shared calendar identity/evidence hash helpers to explicit
`YYYY-MM-DD` rendering so their bytes do not depend on session `DateStyle`.

The reader joins exact immutable occurrence/content-revision lineage and never
uses a mutable calendar stream head as its source. Its first page binds the
query and complete ordered candidate manifest to a PostgreSQL MVCC snapshot for
15 minutes. Every page is validated and buffered before any result is exposed;
the Worker requires identity response encoding and rejects a non-terminal short
page, excess continuation count, or an RPC body above 4 MiB before JSON decode. Any parser
failure is re-raised without upstream payload details in its exception chain.
Cursor expiry, manifest drift, corrupt hashes or lineage, or an ambiguity in the
full as-of-eligible retained timeline fails the whole read closed with no
partial result. The RPC writes no domain rows.

This bounded reader is not collection, runtime-container, scheduler, timing
backfill, DQ, completeness, provider-finality or authenticity,
corporate-action, dataset, research, feature, backtest, strategy, order,
Desktop, or Live authorization. Production Live remains not authorized.

`RetainedKrCalendarCoverageService` can consume one complete snapshot from the
official durable calendar reader and fails closed unless every calendar date
in the requested inclusive range appears exactly once and in order. It
rebinds the query, provider, `KR` market, `selected_as_of`, unique selected
revision/occurrence lineage, the reader snapshot clock, and one uniform
selected provider-contract hash. The service keeps an isolated canonical copy
of the caller scope instead of trusting the request object handed to the
reader, and detaches request/lineage clocks into fresh UTC values before they
enter the result. `candidate_count` may be greater than the selected date count
because retained corrections and re-observations remain raw candidates; it
may never be smaller.

For a `next_business_date` inside the retained range, the gate requires that
the target date is open, every intervening retained date is closed, and the
target regular-session hours match. A suffix whose next business session is
outside the range must make one internally consistent date/hour claim, but the
target itself cannot be checked from this snapshot. The result therefore fixes
`coverage_scope=retained_calendar_date_range_only`,
`retained_date_coverage_complete=true`, `full_calendar_certified=false`, and
`right_boundary_next_session_verified=false`.

The coverage spec and selected data/lineage manifest use stable canonical
SHA-256 fingerprints. Page size, query hash, snapshot token, and snapshot issue
time are acquisition metadata and do not change those fingerprints. The raw
snapshot manifest cannot be recomputed from selected items: the gate only
binds the value already verified by the official reader. A successful result
does not prove provider authenticity or finality, official exchange-calendar
completeness, historical database visibility, corporate-action or DQ safety,
dataset/research/feature/backtest readiness, or order authorization. The pure
read-only service is not wired into runtime, scheduler, Desktop, strategy, or
orders. Production Live remains not authorized.

The existing timing writer keeps its stricter monotonic source-stream guard.
An exact calendar-only occurrence can be replayed after a newer observation,
but a new timing request cannot bind that older occurrence once the calendar
stream head has advanced; it is quarantined fail closed. Exact retries of an
already recorded timing request still use the timing request ledger. Supporting
asynchronous historical timing backfill requires a separate reviewed contract
and is not part of this store.

Calendar evidence and timing evidence have separate acceptance semantics. A
valid calendar correction may be committed before a later timing-stream guard
quarantines the timing candidate. The current Worker adapter still raises a
fail-closed timing error and does not expose that partial receipt as structured
telemetry; database receipts remain the audit source until runtime wiring adds
that observability.

The explicit candle and timing Supabase adapters are not wired into collection,
the runtime container, or feature calculation. The stores prove only the
behavior of observations submitted to their RPCs. A later exact re-observation
of the latest unchanged content now creates an immutable occurrence without
creating a duplicate content revision, and timing binds that exact occurrence
rather than a mutable stream-head clock. A pre-migration database can recover
each content revision's original occurrence and the latest candle head
observation, but intermediate unchanged observations that were never stored
cannot be reconstructed. A request key that already has a durable quarantine
receipt keeps replaying that receipt; callers must use a new request key for a
newly observed event.

`20260719030000_pit_daily_candle_as_of_reader.sql` adds the Worker-only
`worker_api.list_pit_daily_candles_as_of_v1` RPC and a matching explicit Worker
adapter. One logical query is restricted to an exact provider, `KR` market,
six-digit symbol, `1d` interval, explicit `adjusted` value, an inclusive range
of at most 366 calendar days, page size `25..100`, and at most 1,000 eligible
raw candidates. Eligibility remains `evidence_available_at <= as_of` over the
durable source evidence retained when the query runs. It does not mean
`received_at <= as_of`, and it cannot reconstruct which transactions were
committed or visible in the database at that historical time. The received
timestamps remain lineage, not an alternate semantic cutoff.

The first page captures a PostgreSQL MVCC snapshot token with a 15-minute TTL
and a SHA-256 manifest over the complete ordered raw-candidate set. Every later
page is bound to the same query, snapshot, and manifest. The Worker adapter
validates and buffers every page before it pre-collapses legal same-availability
timing revisions and invokes the existing domain selector exactly once. It
never exposes a partial selection. An expired or drifting snapshot, unresolved
timeline ambiguity, malformed immutable payload, corrupt content/occurrence/
timing binding, or manifest mismatch fails the whole read closed. The read
writes zero domain rows.

The cursor is an opaque continuation inside the trusted Worker-to-`worker_api`
boundary, not an end-user bearer token. A direct `service_role` caller must
replay it verbatim. The official adapter pins the first-page metadata and
rejects any later cursor or envelope mutation; the database applies the
15-minute expiry to that unmodified server-issued cursor.

`DailyCandleResearchSliceService` can consume one complete snapshot from the
official durable reader and fails closed unless the selected open sessions
cover the requested inclusive first and last session boundaries, are strictly
increasing, and each retained calendar revision points to the next selected
business date with matching regular-session hours. It rebinds the query,
scope, `selected_as_of`, unique content/occurrence lineage, uniform candle and
calendar contract pins, and the existing as-of selection. The gated result
labels its coverage `retained_open_session_chain_only`, fixes
`full_research_certified=false`, and emits separate SHA-256 fingerprints for
the logical slice scope and the selected data/lineage manifest. Page size,
snapshot token, and snapshot issue time remain acquisition metadata and do not
change those stable fingerprints when the semantic scope and evidence are
otherwise identical.

Here, `contiguous` means only the chain asserted by the retained selected
calendar evidence; it is not proof that provider or KRX history is complete.
A research slice and manifest are not a persisted or certified dataset,
provider signature, historical database snapshot, corporate-action or DQ
approval, deterministic feature/backtest replay, strategy promotion, or order
authorization. The slice service is read-only and is not wired into the
runtime container, scheduler, features, backtests, strategy, or any order
path.

The read RPCs are granted only to the server-side `service_role`; they are not
exposed to Desktop, `public`, authenticated clients, or Realtime. The port and
adapter are not wired into collection, the runtime container, features, strategy,
backtests, or any order path. These contracts do not prove collection
completeness, provider authenticity or finality, corporate-action safety, DQ
approval, or feature/research/order readiness. An unresolved quarantine is
evidence of an ambiguity, not an automated resolution or a promotion decision.
Production Live remains not authorized.

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
