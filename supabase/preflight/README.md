# PostgreSQL 17 pgcrypto replay preflight

`pgcrypto_replay_preflight.sql` is an out-of-band, transactional compatibility
boundary. Run it with an approved migration role immediately before the
migration runner. For an empty or pre-convergence replay, the session's first
effective non-system `search_path` schema must be `public`. The preflight is not
a migration, does not create `pgcrypto`, and does not edit
`supabase_migrations.schema_migrations`.

The preflight supports four explicit states:

- an empty PG17 database without `pgcrypto`: no-op; historical `0001` installs it;
- a pre-convergence database with `pgcrypto` in `public`: ACL, owner, version,
  member, and caller checks followed by a verified no-op;
- a pre-convergence database with `pgcrypto` in `extensions`: fail-closed caller,
  owner, ACL, collision, OID, and metadata checks, then temporary relocation to
  `public` for immutable historical migration replay;
- a database whose `20260718165749` convergence is recorded: final
  `extensions` owner/ACL/member boundary verification and no-op.

Any missing extension on a retained database, unknown owner/schema/version,
unsupported member class, split extension/member ownership that the migration
role cannot exercise, non-PG17 server, incomplete migration ledger, unsafe
catalog caller, untrusted direct or inherited `CREATE` grant, or catalog
identity change aborts the transaction. Function-level and catalog-persisted
role/database `search_path` settings are inspected. The `public` schema owner
and direct, inherited, and `SET ROLE`-reachable `CREATE` privileges are checked
even when `pgcrypto` is absent. That closes the installer path before the
empty-database no-op is allowed. Before hardening its own local `search_path`,
the preflight captures that preflight connection's effective schemas. Until
convergence is recorded it requires `public` to be first, preventing an earlier
schema from shadowing unqualified replay calls.
It also requires database and `public` object-creation privileges, verifies the
PostgreSQL 17 trusted-extension install contract on the empty path, and checks
the existing or future `extensions` owner, owner-role access, `CREATE` ACL
closure, and all reserved pgcrypto member-name conflicts before any
relocation. Caller matching normalizes SQL comments, so
comments inserted between a schema, routine name, and `(` do not hide a call.
For SQL and PL/pgSQL routines a small lexer reads `pg_proc.prosrc`: it preserves
static code and quoted identifiers, replaces single-quoted and dollar-quoted
string contents, and removes line and nested block comments. A literal such as
`'--'` therefore cannot erase a real static caller later on the same line, while
ordinary source comments do not create a false positive. Other procedural
languages retain the conservative raw-source scan. Because stored source does
not prove which historical `standard_conforming_strings` mode parsed an
ordinary backslash, both standard and legacy escape interpretations are scanned
and either unsafe result rejects the routine. SQL Unicode escape identifiers
(`U&"..."`) in executable SQL are rejected for manual review rather than
decoded incompletely; the same text inside a literal, dollar-quoted body, or
comment is ignored by the lexer.

Before convergence, only the exact lowercase `public.digest` token in
`private`, `api`, or `worker_api` is accepted because that is the precise scope
rewritten by migration `20260718165749`. Public-qualified members elsewhere,
other member names, noncanonical/comment-obfuscated spellings, and ambiguous
unqualified calls fail closed. PostgreSQL 17's unqualified core
`pg_catalog.gen_random_uuid()` is the sole name-collision exception; an
explicit `public.gen_random_uuid()` remains rejected.

After convergence, the retained `extensions` boundary is checked across every
non-system, non-extension-owned function and procedure schema, not only the
three schemas rewritten by the convergence migration. Exact lowercase
`extensions.<member>` calls take a catalog-scan fast path across all 36 stock
pgcrypto members; any remaining member token, Unicode escape identifier,
alternate spelling, qualification, or comment boundary is normalized and
reviewed before the final state is accepted. Public-qualified pgcrypto members
and unqualified non-core members are rejected; unqualified PostgreSQL 17 core
`pg_catalog.gen_random_uuid()` remains the sole exception.

The catalog caller scan also runs before the empty-database no-op, so a stored
routine cannot become an unreviewed pgcrypto caller after historical `0001`
installs the extension. In both reserved target schemas, `public` and
`extensions`, every stock pgcrypto member name is exclusive: any routine with a
matching name that is not owned by the pgcrypto extension fails closed,
regardless of signature. This intentionally conservative rule covers default,
variadic, polymorphic, domain, implicit-cast, and unknown-literal overload
resolution without attempting to reproduce PostgreSQL's resolver. Explicitly
qualified routines in other custom schemas remain outside that name reservation.

The supported extension oracle is deliberately narrow: stock PostgreSQL 17
`pgcrypto` version `1.3`, with exactly its 36 `pg_proc` members. The preflight
compares every member identity, input/output signature, routine kind, language,
volatility, parallel safety, security-definer/leakproof/strict/set-returning
flags, argument modes/names/default count, function config, binary/source
symbol, planner-support/variadic/transform/default-expression state, cost, and
row estimate with a fixed clean-install contract. Namespace,
OID, routine ACL, and owner are installation-specific: namespace and owner are
constrained independently, while OIDs and ACLs must remain identical across a
relocation. Extension and member owners are limited to `postgres` and
`supabase_admin`. The default `public` schema may be owned by PostgreSQL's
virtual `pg_database_owner` role; `extensions` remains limited to `postgres` and
`supabase_admin`. A vendor-patched catalog or future
extension version fails closed and requires a reviewed oracle update backed by
a clean PostgreSQL 17 extraction. A matching before/after relocation digest
alone is never treated as proof of authenticity.

Do not bypass a failure or edit historical migration SQL. Stop application and
Worker traffic before execution because session-local state beyond the checked
preflight-session `search_path`, external dynamic SQL, and prepared statements
are not discoverable from the catalog scan.

The transaction advisory lock serializes concurrent preflight transactions
only. It is released at preflight commit and cannot protect the subsequent
migration replay. The operator must hold one external, single-deployment mutex
across the uninterrupted preflight-and-migration-runner sequence; no concurrent
runner may start until that sequence finishes.

`psql -f` and `supabase db push` open different database sessions. Therefore the
preflight-session `current_schemas(false)` check cannot prove the later runner's
session state. Both commands must use the same approved role and immutable
connection profile, that role/database's persistent default must be
`public`-first, and client/session overrides such as `PGOPTIONS`, URI `options`,
or a runner-side `SET search_path` are forbidden. Retain a runner-connection
`current_user`/`current_schemas(false)` receipt as corroborating evidence; it is
not a same-session proof. If the deployment wrapper cannot enforce those
conditions, stop rather than treating this preflight as a replay guarantee.

The approved sequence and evidence requirements are documented in
`docs/SUPABASE_SETUP.md`. Hosted Staging execution requires separate user
approval and dedicated credentials. Never put a connection string, token, JWT,
or SQL body containing secrets in the retained receipt.
