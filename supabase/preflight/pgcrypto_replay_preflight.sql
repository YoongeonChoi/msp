-- Run this transaction before the repository migration runner on PostgreSQL 17.
-- It preserves immutable historical migrations by staging a trusted, relocatable
-- pgcrypto extension in public only while pre-convergence migrations replay.
-- It does not create pgcrypto and it never repairs migration history.

begin;

select pg_catalog.set_config(
  'msp.pgcrypto_replay_preflight_schemas',
  pg_catalog.current_schemas(false)::text,
  true
);

set local search_path = pg_catalog, pg_temp;
set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $pgcrypto_replay_preflight$
declare
  expected_convergence_version constant text := '20260718165749';
  server_version integer := current_setting('server_version_num')::integer;
  migration_history regclass := to_regclass(
    'supabase_migrations.schema_migrations'
  );
  migration_count bigint := 0;
  history_versions text[] := array[]::text[];
  history_names text[] := array[]::text[];
  expected_versions constant text[] := array[
    '0001','0002','0003','0004','0005','0006','0007','0008','0009','0010',
    '0011','0012','0013','0014','0015','0016','0017','0018','0019','0020',
    '0021','0022','0023','0024','20260714154520','20260714155117',
    '20260714155744','20260714160105','20260714161511','20260714165910',
    '20260715020752','20260715041903','20260715041909','20260715041912',
    '20260715041915','20260718165749','20260719001947','20260719010000',
    '20260719020000','20260719030000','20260719040000','20260719050000',
    '20260719060000','20260719070000','20260719080000','20260719090000',
    '20260723162000','20260724210000','20260724234500','20260725090000'
  ];
  expected_names constant text[] := array[
    'schema','rls','realtime','retention','schema_alignment','outcome_tracking',
    'backtest_runs','backtest_runs_rls','live_operations_hardening',
    'security_definer_hardening','data_api_grants','runtime_safety_invariants',
    'worker_deployment_lock','desktop_audit_summary',
    'paper_order_execution_details','private_foundation',
    'execution_accounting_truth','control_plane_api','rpc_access_contract',
    'canonical_operations_contract','reconciliation_and_cutover',
    'operational_workflows','operational_safety_closure',
    'operational_upgrade_convergence','control_qualification_workflow',
    'paper_execution_source','cash_settlement_maturity',
    'operations_runtime_scheduler','unknown_execution_resolution_v2',
    'unknown_resolution_desktop_projection','kst_trading_date_convergence',
    'paper_bar_participation_guard','operation_claim_fencing',
    'sell_cost_basis_checkpoint_guard',
    'paper_evidence_and_sell_reservation_guards','pgcrypto_schema_convergence',
    'pit_candle_revision_store','pit_daily_candle_timing_store',
    'pit_source_observation_occurrence_store','pit_daily_candle_as_of_reader',
    'pit_calendar_observation_store','pit_calendar_as_of_reader',
    'kr_calendar_collection_job_store',
    'kr_calendar_collection_job_conflict_boundary',
    'kr_calendar_collection_job_inspection',
    'pit_daily_candle_collection_job_store',
    'desktop_operations_sensitive_projection_gate',
    'durable_operations_scheduler','durable_scheduler_conflict_target',
    'durable_scheduler_budget_policy'
  ];
  trusted_role_names constant text[] := array[
    'postgres', 'supabase_admin', 'pg_database_owner'
  ];
  trusted_pgcrypto_owner_names constant text[] := array[
    'postgres', 'supabase_admin'
  ];
  preflight_effective_schemas name[] := pg_catalog.current_setting(
    'msp.pgcrypto_replay_preflight_schemas'
  )::name[];
  expected_pgcrypto_member_signatures constant text[] := array[
    'armor(bytea)',
    'armor(bytea,text[],text[])',
    'crypt(text,text)',
    'dearmor(text)',
    'decrypt(bytea,bytea,text)',
    'decrypt_iv(bytea,bytea,bytea,text)',
    'digest(bytea,text)',
    'digest(text,text)',
    'encrypt(bytea,bytea,text)',
    'encrypt_iv(bytea,bytea,bytea,text)',
    'gen_random_bytes(integer)',
    'gen_random_uuid()',
    'gen_salt(text)',
    'gen_salt(text,integer)',
    'hmac(bytea,bytea,text)',
    'hmac(text,text,text)',
    'pgp_armor_headers(text)',
    'pgp_key_id(bytea)',
    'pgp_pub_decrypt(bytea,bytea)',
    'pgp_pub_decrypt(bytea,bytea,text)',
    'pgp_pub_decrypt(bytea,bytea,text,text)',
    'pgp_pub_decrypt_bytea(bytea,bytea)',
    'pgp_pub_decrypt_bytea(bytea,bytea,text)',
    'pgp_pub_decrypt_bytea(bytea,bytea,text,text)',
    'pgp_pub_encrypt(text,bytea)',
    'pgp_pub_encrypt(text,bytea,text)',
    'pgp_pub_encrypt_bytea(bytea,bytea)',
    'pgp_pub_encrypt_bytea(bytea,bytea,text)',
    'pgp_sym_decrypt(bytea,text)',
    'pgp_sym_decrypt(bytea,text,text)',
    'pgp_sym_decrypt_bytea(bytea,text)',
    'pgp_sym_decrypt_bytea(bytea,text,text)',
    'pgp_sym_encrypt(text,text)',
    'pgp_sym_encrypt(text,text,text)',
    'pgp_sym_encrypt_bytea(bytea,text)',
    'pgp_sym_encrypt_bytea(bytea,text,text)'
  ];
  -- Oracle extracted from a clean PostgreSQL 17 `pgcrypto` 1.3 installation.
  -- Namespace, owner, OID and ACL are intentionally checked separately because
  -- they are installation-specific. Every other listed pg_proc property is an
  -- immutable part of the supported replay contract.
  expected_pgcrypto_member_contract constant text[] := array[
    'armor(bytea)->text|kind=f|lang=c|vol=i|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=1|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pg_armor|cost=1|rows=0',
    'armor(bytea, text[], text[])->text|kind=f|lang=c|vol=i|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=3|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pg_armor|cost=1|rows=0',
    'crypt(text, text)->text|kind=f|lang=c|vol=i|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=2|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pg_crypt|cost=1|rows=0',
    'dearmor(text)->bytea|kind=f|lang=c|vol=i|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=1|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pg_dearmor|cost=1|rows=0',
    'decrypt(bytea, bytea, text)->bytea|kind=f|lang=c|vol=i|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=3|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pg_decrypt|cost=1|rows=0',
    'decrypt_iv(bytea, bytea, bytea, text)->bytea|kind=f|lang=c|vol=i|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=4|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pg_decrypt_iv|cost=1|rows=0',
    'digest(bytea, text)->bytea|kind=f|lang=c|vol=i|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=2|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pg_digest|cost=1|rows=0',
    'digest(text, text)->bytea|kind=f|lang=c|vol=i|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=2|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pg_digest|cost=1|rows=0',
    'encrypt(bytea, bytea, text)->bytea|kind=f|lang=c|vol=i|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=3|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pg_encrypt|cost=1|rows=0',
    'encrypt_iv(bytea, bytea, bytea, text)->bytea|kind=f|lang=c|vol=i|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=4|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pg_encrypt_iv|cost=1|rows=0',
    'gen_random_bytes(integer)->bytea|kind=f|lang=c|vol=v|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=1|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pg_random_bytes|cost=1|rows=0',
    'gen_random_uuid()->uuid|kind=f|lang=c|vol=v|parallel=s|secdef=f|leak=f|strict=f|retset=f|nargs=0|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pg_random_uuid|cost=1|rows=0',
    'gen_salt(text)->text|kind=f|lang=c|vol=v|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=1|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pg_gen_salt|cost=1|rows=0',
    'gen_salt(text, integer)->text|kind=f|lang=c|vol=v|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=2|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pg_gen_salt_rounds|cost=1|rows=0',
    'hmac(bytea, bytea, text)->bytea|kind=f|lang=c|vol=i|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=3|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pg_hmac|cost=1|rows=0',
    'hmac(text, text, text)->bytea|kind=f|lang=c|vol=i|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=3|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pg_hmac|cost=1|rows=0',
    'pgp_armor_headers(text, OUT key text, OUT value text)->SETOF record|kind=f|lang=c|vol=i|parallel=s|secdef=f|leak=f|strict=t|retset=t|nargs=1|ndef=0|argmodes={i,o,o}|argnames={"",key,value}|config={}|bin=$libdir/pgcrypto|src=pgp_armor_headers|cost=1|rows=1000',
    'pgp_key_id(bytea)->text|kind=f|lang=c|vol=i|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=1|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pgp_key_id_w|cost=1|rows=0',
    'pgp_pub_decrypt(bytea, bytea)->text|kind=f|lang=c|vol=i|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=2|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pgp_pub_decrypt_text|cost=1|rows=0',
    'pgp_pub_decrypt(bytea, bytea, text)->text|kind=f|lang=c|vol=i|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=3|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pgp_pub_decrypt_text|cost=1|rows=0',
    'pgp_pub_decrypt(bytea, bytea, text, text)->text|kind=f|lang=c|vol=i|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=4|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pgp_pub_decrypt_text|cost=1|rows=0',
    'pgp_pub_decrypt_bytea(bytea, bytea)->bytea|kind=f|lang=c|vol=i|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=2|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pgp_pub_decrypt_bytea|cost=1|rows=0',
    'pgp_pub_decrypt_bytea(bytea, bytea, text)->bytea|kind=f|lang=c|vol=i|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=3|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pgp_pub_decrypt_bytea|cost=1|rows=0',
    'pgp_pub_decrypt_bytea(bytea, bytea, text, text)->bytea|kind=f|lang=c|vol=i|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=4|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pgp_pub_decrypt_bytea|cost=1|rows=0',
    'pgp_pub_encrypt(text, bytea)->bytea|kind=f|lang=c|vol=v|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=2|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pgp_pub_encrypt_text|cost=1|rows=0',
    'pgp_pub_encrypt(text, bytea, text)->bytea|kind=f|lang=c|vol=v|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=3|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pgp_pub_encrypt_text|cost=1|rows=0',
    'pgp_pub_encrypt_bytea(bytea, bytea)->bytea|kind=f|lang=c|vol=v|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=2|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pgp_pub_encrypt_bytea|cost=1|rows=0',
    'pgp_pub_encrypt_bytea(bytea, bytea, text)->bytea|kind=f|lang=c|vol=v|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=3|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pgp_pub_encrypt_bytea|cost=1|rows=0',
    'pgp_sym_decrypt(bytea, text)->text|kind=f|lang=c|vol=i|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=2|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pgp_sym_decrypt_text|cost=1|rows=0',
    'pgp_sym_decrypt(bytea, text, text)->text|kind=f|lang=c|vol=i|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=3|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pgp_sym_decrypt_text|cost=1|rows=0',
    'pgp_sym_decrypt_bytea(bytea, text)->bytea|kind=f|lang=c|vol=i|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=2|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pgp_sym_decrypt_bytea|cost=1|rows=0',
    'pgp_sym_decrypt_bytea(bytea, text, text)->bytea|kind=f|lang=c|vol=i|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=3|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pgp_sym_decrypt_bytea|cost=1|rows=0',
    'pgp_sym_encrypt(text, text)->bytea|kind=f|lang=c|vol=v|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=2|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pgp_sym_encrypt_text|cost=1|rows=0',
    'pgp_sym_encrypt(text, text, text)->bytea|kind=f|lang=c|vol=v|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=3|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pgp_sym_encrypt_text|cost=1|rows=0',
    'pgp_sym_encrypt_bytea(bytea, text)->bytea|kind=f|lang=c|vol=v|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=2|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pgp_sym_encrypt_bytea|cost=1|rows=0',
    'pgp_sym_encrypt_bytea(bytea, text, text)->bytea|kind=f|lang=c|vol=v|parallel=s|secdef=f|leak=f|strict=t|retset=f|nargs=3|ndef=0|argmodes={}|argnames={}|config={}|bin=$libdir/pgcrypto|src=pgp_sym_encrypt_bytea|cost=1|rows=0'
  ];
  convergence_applied boolean := false;
  later_migration_applied boolean := false;
  repository_sentinel_exists boolean;
  extension_oid oid;
  extension_schema text;
  extension_owner_oid oid;
  extension_owner_name text;
  extension_relocatable boolean;
  extension_version text;
  available_default_version text;
  available_requires_superuser boolean;
  available_trusted boolean;
  digest_bytea_oid oid;
  digest_text_oid oid;
  digest_metadata_before text;
  digest_metadata_after text;
  member_routine_metadata_before text;
  member_routine_metadata_after text;
  member_oids_before text[];
  member_oids_after text[];
  pgcrypto_member_contract text[];
  pgcrypto_member_names text[];
  pgcrypto_member_name_pattern text;
  pgcrypto_noncore_member_name_pattern text;
  pgcrypto_member_token_pattern text;
  pgcrypto_noncore_member_token_pattern text;
  pgcrypto_canonical_member_pattern text;
  public_acl_before text;
  public_acl_after text;
  extensions_acl_before text;
  extensions_acl_after text;
  routine record;
  collision record;
  routine_definition text;
  routine_definition_normalized text;
  routine_definition_normalized_standard text;
  routine_definition_normalized_legacy text;
  routine_definition_without_canonical text;
  scan_mode integer;
  scan_mode_count integer;
  source_position integer;
  source_length integer;
  code_start integer;
  comment_depth integer;
  closing_offset integer;
  opening_offset integer;
  backslash_offset integer;
  line_feed_offset integer;
  carriage_return_offset integer;
  dollar_quote_tag text;
  escape_string boolean;
  routine_tail text;
  current_role_oid oid;
  current_role_superuser boolean;
  target_schema text;
  target_schema_owner_oid oid;
  target_schema_owner_name text;
  has_function_search_path boolean;
  has_extensions_search_path boolean;
  has_default_extensions_search_path boolean;
  unsafe_caller boolean;
begin
  if server_version < 170000 or server_version >= 180000 then
    raise exception
      'pgcrypto replay preflight requires PostgreSQL 17, found %',
      current_setting('server_version');
  end if;

  perform pg_advisory_xact_lock(684736450187356112::bigint);

  if migration_history is not null then
    execute format(
      'select'
      ' coalesce(array_agg(version order by version), array[]::text[]),'
      ' coalesce(array_agg(name order by version), array[]::text[]),'
      ' count(*),'
      ' coalesce(bool_or(version = %L), false),'
      ' coalesce(bool_or('
      '   version ~ %L and version > %L'
      ' ), false)'
      ' from supabase_migrations.schema_migrations',
      expected_convergence_version,
      '^[0-9]{14}$',
      expected_convergence_version
    )
    into
      history_versions,
      history_names,
      migration_count,
      convergence_applied,
      later_migration_applied;
  end if;

  if migration_count > cardinality(expected_versions)
    or history_versions is distinct from
      expected_versions[1:migration_count::integer]
    or history_names is distinct from
      expected_names[1:migration_count::integer] then
    raise exception
      'migration history is not an exact repository prefix';
  end if;

  select exists (
    select 1
    from pg_class as relation
    join pg_namespace as namespace on namespace.oid = relation.relnamespace
    where (namespace.nspname, relation.relname) in (
      ('public', 'user_roles'),
      ('private', 'trading_accounts')
    )
  ) or exists (
    select 1
    from pg_proc as procedure
    join pg_namespace as namespace on namespace.oid = procedure.pronamespace
    where (namespace.nspname, procedure.proname) in (
      ('api', 'get_desktop_operations_snapshot_v1'),
      ('worker_api', 'acquire_worker_lease')
    )
  ) into repository_sentinel_exists;

  if migration_count = 0 and repository_sentinel_exists then
    raise exception
      'repository objects exist without migration history; repair is required';
  end if;
  if later_migration_applied and not convergence_applied then
    raise exception
      'migration history continues past pgcrypto convergence without its version';
  end if;
  if not convergence_applied
    and preflight_effective_schemas[1] is distinct from 'public'::name then
    raise exception
      'preflight search_path must begin with public before pgcrypto relocation';
  end if;

  select
    array_agg(member_name order by member_name collate "C"),
    '(' || string_agg(
      member_name,
      '|' order by member_name collate "C"
    ) || ')',
    '(' || string_agg(
      member_name,
      '|' order by member_name collate "C"
    ) filter (where member_name <> 'gen_random_uuid') || ')'
  into
    pgcrypto_member_names,
    pgcrypto_member_name_pattern,
    pgcrypto_noncore_member_name_pattern
  from (
    select distinct split_part(signature.value, '(', 1) as member_name
    from unnest(expected_pgcrypto_member_signatures) as signature(value)
  ) as member_names;
  pgcrypto_member_token_pattern :=
    '(^|[^a-zA-Z0-9_])"?' || pgcrypto_member_name_pattern
    || '"?([^a-zA-Z0-9_]|$)';
  pgcrypto_noncore_member_token_pattern :=
    '(^|[^a-zA-Z0-9_])"?' || pgcrypto_noncore_member_name_pattern
    || '"?([^a-zA-Z0-9_]|$)';
  pgcrypto_canonical_member_pattern :=
    'extensions\.' || pgcrypto_member_name_pattern;

  -- The empty-database path still lets historical 0001 install pgcrypto in
  -- public. Validate that boundary before inspecting (or returning for) the
  -- extension so an absent extension cannot bypass schema privilege checks.
  select role.oid, role.rolsuper
  into current_role_oid, current_role_superuser
  from pg_roles as role
  where role.rolname = current_user;
  if not convergence_applied and (
    not has_database_privilege(
      current_user,
      current_database(),
      'CREATE'
    )
    or not has_schema_privilege(current_user, 'public', 'CREATE')
  ) then
    raise exception 'preflight role cannot create required replay objects';
  end if;
  target_schema := 'public';
  select namespace.nspowner, pg_get_userbyid(namespace.nspowner)
  into target_schema_owner_oid, target_schema_owner_name
  from pg_namespace as namespace
  where namespace.nspname = target_schema;
  if target_schema_owner_oid is null then
    raise exception 'required pgcrypto target schema % is missing', target_schema;
  end if;
  if not (target_schema_owner_name = any(trusted_role_names)) then
    raise exception
      'pgcrypto target schema % has an untrusted owner %',
      target_schema,
      target_schema_owner_name;
  end if;
  if exists (
    select 1
    from pg_namespace as namespace
    cross join lateral aclexplode(
      coalesce(namespace.nspacl, acldefault('n', namespace.nspowner))
    ) as acl
    left join pg_roles as grantee on grantee.oid = acl.grantee
    where namespace.nspname = target_schema
      and lower(acl.privilege_type) = 'create'
      and not (
        acl.grantee in (target_schema_owner_oid, current_role_oid)
        or coalesce(grantee.rolname, '') = any(trusted_role_names)
      )
  ) or exists (
    -- has_schema_privilege covers direct and inherited CREATE. The explicit
    -- SET closure also covers NOINHERIT memberships and a path that terminates
    -- at an otherwise trusted creator role.
    select 1
    from pg_roles as login_role
    where login_role.rolcanlogin
      and login_role.oid <> current_role_oid
      and not (login_role.rolname = any(trusted_role_names))
      and exists (
        select 1
        from pg_roles as reachable_role
        where pg_has_role(login_role.oid, reachable_role.oid, 'SET')
          and has_schema_privilege(
            reachable_role.oid,
            target_schema,
            'CREATE'
          )
      )
  ) then
    raise exception 'untrusted role can CREATE in schema %', target_schema;
  end if;

  target_schema := 'extensions';
  select namespace.nspowner, pg_get_userbyid(namespace.nspowner)
  into target_schema_owner_oid, target_schema_owner_name
  from pg_namespace as namespace
  where namespace.nspname = target_schema;
  if target_schema_owner_oid is null then
    if convergence_applied then
      raise exception 'required pgcrypto target schema extensions is missing';
    end if;
    if not (current_user = any(trusted_pgcrypto_owner_names)) then
      raise exception
        'preflight role would create extensions with an untrusted owner';
    end if;
  else
    if not (
      target_schema_owner_name = any(trusted_pgcrypto_owner_names)
    ) then
      raise exception
        'pgcrypto target schema % has an untrusted owner %',
        target_schema,
        target_schema_owner_name;
    end if;
    if not pg_has_role(current_user, target_schema_owner_oid, 'USAGE') then
      raise exception
        'current migration role cannot act as extensions schema owner';
    end if;
    if not has_schema_privilege(current_user, target_schema, 'CREATE') then
      raise exception 'preflight role cannot create required replay objects';
    end if;
    if exists (
      select 1
      from pg_namespace as namespace
      cross join lateral aclexplode(
        coalesce(namespace.nspacl, acldefault('n', namespace.nspowner))
      ) as acl
      left join pg_roles as grantee on grantee.oid = acl.grantee
      where namespace.nspname = target_schema
        and lower(acl.privilege_type) = 'create'
        and not (
          acl.grantee in (target_schema_owner_oid, current_role_oid)
          or coalesce(grantee.rolname, '') = any(trusted_role_names)
        )
    ) or exists (
      select 1
      from pg_roles as login_role
      where login_role.rolcanlogin
        and login_role.oid <> current_role_oid
        and not (login_role.rolname = any(trusted_role_names))
        and exists (
          select 1
          from pg_roles as reachable_role
          where pg_has_role(login_role.oid, reachable_role.oid, 'SET')
            and has_schema_privilege(
              reachable_role.oid,
              target_schema,
              'CREATE'
            )
        )
    ) then
      raise exception 'untrusted role can CREATE in schema %', target_schema;
    end if;
  end if;

  select
    extension.oid,
    namespace.nspname,
    extension.extowner,
    pg_get_userbyid(extension.extowner),
    extension.extrelocatable,
    extension.extversion,
    to_regprocedure(format('%I.digest(bytea,text)', namespace.nspname))::oid,
    to_regprocedure(format('%I.digest(text,text)', namespace.nspname))::oid
  into
    extension_oid,
    extension_schema,
    extension_owner_oid,
    extension_owner_name,
    extension_relocatable,
    extension_version,
    digest_bytea_oid,
    digest_text_oid
  from pg_extension as extension
  join pg_namespace as namespace on namespace.oid = extension.extnamespace
  where extension.extname = 'pgcrypto';

  if extension_oid is null
    and (migration_count > 0 or repository_sentinel_exists) then
    raise exception
      'pgcrypto is missing from a retained repository database';
  end if;

  select
    target_namespace.nspname,
    target_procedure.oid::regprocedure::text as identity
  into collision
  from pg_proc as target_procedure
  join pg_namespace as target_namespace
    on target_namespace.oid = target_procedure.pronamespace
  where target_namespace.nspname in ('public', 'extensions')
    and target_procedure.proname = any(pgcrypto_member_names)
    and not exists (
      select 1
      from pg_depend as dependency
      where dependency.classid = 'pg_proc'::regclass
        and dependency.objid = target_procedure.oid
        and dependency.refclassid = 'pg_extension'::regclass
        and dependency.refobjid = extension_oid
        and dependency.deptype = 'e'
    )
  order by target_namespace.nspname, target_procedure.oid
  limit 1;
  if found then
    raise exception
      'pgcrypto target member name conflicts with %',
      collision.identity;
  end if;

  if extension_oid is null then
    select
      available.default_version,
      version_info.superuser,
      version_info.trusted
    into
      available_default_version,
      available_requires_superuser,
      available_trusted
    from pg_available_extensions as available
    join pg_available_extension_versions as version_info
      on version_info.name = available.name
     and version_info.version = available.default_version
    where available.name = 'pgcrypto';
    if available_default_version is distinct from '1.3' then
      raise exception
        'historical pgcrypto installer requires available version 1.3';
    end if;
    if coalesce(available_requires_superuser, true)
      and not (
        coalesce(available_trusted, false)
        or current_role_superuser
      ) then
      raise exception
        'preflight role cannot install the available pgcrypto extension';
    end if;
  else

  if extension_schema not in ('public', 'extensions') then
    raise exception
      'pgcrypto is installed in unexpected schema %', extension_schema;
  end if;
  if not (extension_owner_name = any(trusted_pgcrypto_owner_names)) then
    raise exception 'pgcrypto requires a trusted owner';
  end if;
  if not pg_has_role(current_user, extension_owner_oid, 'USAGE') then
    raise exception 'current migration role cannot act as pgcrypto owner';
  end if;
  if not extension_relocatable then
    raise exception 'pgcrypto must remain relocatable for historical replay';
  end if;
  if extension_version <> '1.3' then
    raise exception 'unsupported pgcrypto extension version %', extension_version;
  end if;
  if digest_bytea_oid is null or digest_text_oid is null then
    raise exception 'pgcrypto digest overloads are incomplete';
  end if;
  if exists (
    select 1
    from (values (digest_bytea_oid), (digest_text_oid)) as digest(oid)
    where not exists (
      select 1
      from pg_depend as dependency
      where dependency.classid = 'pg_proc'::regclass
        and dependency.objid = digest.oid
        and dependency.refclassid = 'pg_extension'::regclass
        and dependency.refobjid = extension_oid
        and dependency.deptype = 'e'
    )
  ) then
    raise exception 'digest overload is not owned by pgcrypto';
  end if;

  if exists (
    select 1
    from pg_depend as dependency
    where dependency.refclassid = 'pg_extension'::regclass
      and dependency.refobjid = extension_oid
      and dependency.deptype = 'e'
      and dependency.classid <> 'pg_proc'::regclass
  ) then
    raise exception 'pgcrypto exposes an unsupported extension member class';
  end if;
  if exists (
    select 1
    from pg_depend as dependency
    join pg_proc as procedure
      on dependency.classid = 'pg_proc'::regclass
     and dependency.objid = procedure.oid
    where dependency.refclassid = 'pg_extension'::regclass
      and dependency.refobjid = extension_oid
      and dependency.deptype = 'e'
      and not (
        pg_get_userbyid(procedure.proowner) = any(
          trusted_pgcrypto_owner_names
        )
      )
  ) then
    raise exception 'pgcrypto member has an untrusted owner';
  end if;
  if exists (
    select 1
    from pg_depend as dependency
    join pg_proc as procedure
      on dependency.classid = 'pg_proc'::regclass
     and dependency.objid = procedure.oid
    where dependency.refclassid = 'pg_extension'::regclass
      and dependency.refobjid = extension_oid
      and dependency.deptype = 'e'
      and not pg_has_role(current_user, procedure.proowner, 'USAGE')
  ) then
    raise exception
      'current migration role cannot act as every pgcrypto member owner';
  end if;

  select array_agg(
    format(
      '%s(%s)->%s|kind=%s|lang=%s|vol=%s|parallel=%s|secdef=%s|leak=%s|strict=%s|retset=%s|nargs=%s|ndef=%s|argmodes=%s|argnames=%s|config=%s|bin=%s|src=%s|cost=%s|rows=%s',
      procedure.proname,
      pg_get_function_identity_arguments(procedure.oid),
      pg_get_function_result(procedure.oid),
      procedure.prokind,
      language.lanname,
      procedure.provolatile,
      procedure.proparallel,
      procedure.prosecdef,
      procedure.proleakproof,
      procedure.proisstrict,
      procedure.proretset,
      procedure.pronargs,
      procedure.pronargdefaults,
      coalesce(procedure.proargmodes::text, '{}'),
      coalesce(procedure.proargnames::text, '{}'),
      coalesce(procedure.proconfig::text, '{}'),
      coalesce(procedure.probin, ''),
      procedure.prosrc,
      procedure.procost,
      procedure.prorows
    )
    order by
      procedure.proname collate "C",
      pg_get_function_identity_arguments(procedure.oid) collate "C"
  )
  into pgcrypto_member_contract
  from pg_depend as dependency
  join pg_proc as procedure
    on dependency.classid = 'pg_proc'::regclass
   and dependency.objid = procedure.oid
  join pg_language as language on language.oid = procedure.prolang
  where dependency.refclassid = 'pg_extension'::regclass
    and dependency.refobjid = extension_oid
    and dependency.deptype = 'e';

  if pgcrypto_member_contract is distinct from
      expected_pgcrypto_member_contract then
    raise exception
      'pgcrypto 1.3 member catalog does not match the PostgreSQL 17 contract';
  end if;
  if exists (
    select 1
    from pg_depend as dependency
    join pg_proc as procedure
      on dependency.classid = 'pg_proc'::regclass
     and dependency.objid = procedure.oid
    where dependency.refclassid = 'pg_extension'::regclass
      and dependency.refobjid = extension_oid
      and dependency.deptype = 'e'
      and (
        procedure.prosupport <> 0
        or procedure.provariadic <> 0
        or procedure.prosqlbody is not null
        or procedure.proargdefaults is not null
        or procedure.protrftypes is not null
      )
  ) then
    raise exception
      'pgcrypto 1.3 member catalog does not match the PostgreSQL 17 contract';
  end if;

  target_schema := case
    when convergence_applied then 'extensions'
    else 'public'
  end;
  select namespace.nspowner, pg_get_userbyid(namespace.nspowner)
  into target_schema_owner_oid, target_schema_owner_name
  from pg_namespace as namespace
  where namespace.nspname = target_schema;
  if target_schema_owner_oid is null then
    raise exception 'required pgcrypto target schema % is missing', target_schema;
  end if;
  if not (
    (
      target_schema = 'public'
      and target_schema_owner_name = any(trusted_role_names)
    )
    or (
      target_schema = 'extensions'
      and target_schema_owner_name = any(trusted_pgcrypto_owner_names)
    )
  ) then
    raise exception
      'pgcrypto target schema % has an untrusted owner %',
      target_schema,
      target_schema_owner_name;
  end if;
  if exists (
    select 1
    from pg_namespace as namespace
    cross join lateral aclexplode(
      coalesce(namespace.nspacl, acldefault('n', namespace.nspowner))
    ) as acl
    left join pg_roles as grantee on grantee.oid = acl.grantee
    where namespace.nspname = target_schema
      and lower(acl.privilege_type) = 'create'
      and not (
        acl.grantee in (
          target_schema_owner_oid,
          extension_owner_oid,
          current_role_oid
        )
        or coalesce(grantee.rolname, '') = any(trusted_role_names)
      )
  ) or exists (
    select 1
    from pg_roles as login_role
    where login_role.rolcanlogin
      and login_role.oid <> current_role_oid
      and not (login_role.rolname = any(trusted_role_names))
      and exists (
        select 1
        from pg_roles as reachable_role
        where pg_has_role(login_role.oid, reachable_role.oid, 'SET')
          and has_schema_privilege(
            reachable_role.oid,
            target_schema,
            'CREATE'
          )
      )
  ) then
    raise exception 'untrusted role can CREATE in schema %', target_schema;
  end if;

  if convergence_applied then
    if extension_schema <> 'extensions'
      or exists (
        select 1
        from pg_proc as procedure
        join pg_namespace as namespace
          on namespace.oid = procedure.pronamespace
        where namespace.nspname = 'public'
          and procedure.proname = 'digest'
          and procedure.pronargs = 2
          and (
            (
              procedure.proargtypes[0] = 'bytea'::regtype
              and procedure.proargtypes[1] = 'text'::regtype
            )
            or (
              procedure.proargtypes[0] = 'text'::regtype
              and procedure.proargtypes[1] = 'text'::regtype
            )
          )
      ) then
      raise exception
        'applied pgcrypto convergence does not match the final schema boundary';
    end if;
    for routine in
      select
        procedure.oid,
        procedure.oid::regprocedure::text as identity,
        procedure.prosrc as source,
        language.lanname as language_name
      from pg_proc as procedure
      join pg_namespace as namespace
        on namespace.oid = procedure.pronamespace
      join pg_language as language on language.oid = procedure.prolang
      where procedure.prokind in ('f', 'p')
        and namespace.nspname <> 'information_schema'
        and namespace.nspname !~ '^pg_'
        and (
          regexp_replace(
            procedure.prosrc,
            pgcrypto_canonical_member_pattern,
            '',
            'g'
          ) ~* pgcrypto_member_token_pattern
          or (
            language.lanname in ('sql', 'plpgsql')
            and procedure.prosrc ~* '(^|[^a-zA-Z0-9_$])u&"'
          )
        )
        and not exists (
          select 1
          from pg_depend as dependency
          where dependency.classid = 'pg_proc'::regclass
            and dependency.objid = procedure.oid
            and dependency.refclassid = 'pg_extension'::regclass
            and dependency.deptype = 'e'
        )
      order by namespace.nspname, procedure.proname, procedure.oid
    loop
      routine_definition := routine.source;
      routine_definition_normalized_standard := routine_definition;
      routine_definition_normalized_legacy := routine_definition;
      if routine.language_name in ('sql', 'plpgsql') then
        scan_mode_count := case
          when strpos(routine_definition, chr(92)) > 0 then 2
          else 1
        end;
        for scan_mode in 1..scan_mode_count loop
          routine_definition_normalized := '';
        source_position := 1;
        source_length := length(routine_definition);
        code_start := 1;
        while source_position <= source_length loop
          if substring(routine_definition from source_position for 2) = '--' then
            routine_definition_normalized :=
              routine_definition_normalized
              || substring(
                routine_definition from code_start
                for source_position - code_start
              ) || ' ';
            source_position := source_position + 2;
            routine_tail := substring(
              routine_definition from source_position
            );
            line_feed_offset := strpos(routine_tail, chr(10));
            carriage_return_offset := strpos(routine_tail, chr(13));
            closing_offset := case
              when line_feed_offset = 0 then carriage_return_offset
              when carriage_return_offset = 0 then line_feed_offset
              else least(line_feed_offset, carriage_return_offset)
            end;
            if closing_offset = 0 then
              source_position := source_length + 1;
            else
              source_position := source_position + closing_offset - 1;
            end if;
            code_start := source_position;
          elsif substring(routine_definition from source_position for 2) =
              '/*' then
            routine_definition_normalized :=
              routine_definition_normalized
              || substring(
                routine_definition from code_start
                for source_position - code_start
              ) || ' ';
            comment_depth := 1;
            source_position := source_position + 2;
            while source_position <= source_length
              and comment_depth > 0 loop
              routine_tail := substring(
                routine_definition from source_position
              );
              opening_offset := strpos(routine_tail, '/*');
              closing_offset := strpos(routine_tail, '*/');
              if closing_offset = 0 then
                source_position := source_length + 1;
                comment_depth := 0;
              elsif opening_offset > 0
                and opening_offset < closing_offset then
                comment_depth := comment_depth + 1;
                source_position := source_position + opening_offset + 1;
              else
                comment_depth := comment_depth - 1;
                source_position := source_position + closing_offset + 1;
              end if;
            end loop;
            code_start := source_position;
          elsif substring(routine_definition from source_position for 1) =
              chr(39) then
            routine_definition_normalized :=
              routine_definition_normalized
              || substring(
                routine_definition from code_start
                for source_position - code_start
              ) || ' ';
            escape_string := scan_mode = 2 or (
              source_position >= 2
              and lower(substring(
                routine_definition from source_position - 1 for 1
              )) = 'e'
              and (
                source_position = 2
                or substring(
                  routine_definition from source_position - 2 for 1
                ) !~ '[a-zA-Z0-9_$]'
              )
            ) or (
              source_position >= 3
              and lower(substring(
                routine_definition from source_position - 2 for 2
              )) = 'u&'
              and (
                source_position = 3
                or substring(
                  routine_definition from source_position - 3 for 1
                ) !~ '[a-zA-Z0-9_$]'
              )
            );
            source_position := source_position + 1;
            while source_position <= source_length loop
              routine_tail := substring(
                routine_definition from source_position
              );
              closing_offset := strpos(routine_tail, chr(39));
              backslash_offset := case
                when escape_string then strpos(routine_tail, chr(92))
                else 0
              end;
              if backslash_offset > 0
                and (
                  closing_offset = 0
                  or backslash_offset < closing_offset
                ) then
                source_position := source_position + backslash_offset + 1;
              elsif closing_offset = 0 then
                source_position := source_length + 1;
                exit;
              else
                source_position := source_position + closing_offset - 1;
                if substring(
                  routine_definition from source_position for 2
                ) = chr(39) || chr(39) then
                source_position := source_position + 2;
                else
                  source_position := source_position + 1;
                  exit;
                end if;
              end if;
            end loop;
            code_start := source_position;
          elsif substring(routine_definition from source_position for 1) =
            chr(34) then
            source_position := source_position + 1;
            while source_position <= source_length loop
              routine_tail := substring(
                routine_definition from source_position
              );
              closing_offset := strpos(routine_tail, chr(34));
              if closing_offset = 0 then
                source_position := source_length + 1;
                exit;
              end if;
              source_position := source_position + closing_offset - 1;
              if substring(
                routine_definition from source_position for 2
              ) = chr(34) || chr(34) then
                source_position := source_position + 2;
              else
                source_position := source_position + 1;
                exit;
              end if;
            end loop;
          elsif substring(routine_definition from source_position for 1) =
              '$' then
            dollar_quote_tag := case
              when substring(
                routine_definition from source_position for 2
              ) = '$$' then '$$'
              else substring(
                substring(routine_definition from source_position)
                from $tag_pattern$^\$[[:alpha:]_][[:alnum:]_]*\$$tag_pattern$
              )
            end;
            if dollar_quote_tag is null then
              source_position := source_position + 1;
            else
              routine_definition_normalized :=
                routine_definition_normalized
                || substring(
                  routine_definition from code_start
                  for source_position - code_start
                ) || ' ';
              source_position := source_position + length(dollar_quote_tag);
              closing_offset := strpos(
                substring(routine_definition from source_position),
                dollar_quote_tag
              );
              if closing_offset = 0 then
                source_position := source_length + 1;
              else
                source_position := source_position + closing_offset - 1
                  + length(dollar_quote_tag);
              end if;
              code_start := source_position;
            end if;
          else
            source_position := source_position + 1;
          end if;
        end loop;
        routine_definition_normalized := routine_definition_normalized
          || substring(routine_definition from code_start);
          if scan_mode = 1 then
            routine_definition_normalized_standard :=
              routine_definition_normalized;
          else
            routine_definition_normalized_legacy :=
              routine_definition_normalized;
          end if;
        end loop;
        if scan_mode_count = 1 then
          routine_definition_normalized_legacy :=
            routine_definition_normalized_standard;
        end if;
      end if;
      if (
          routine.language_name in ('sql', 'plpgsql')
          and (
            routine_definition_normalized_standard
              ~* '(^|[^a-zA-Z0-9_$])u&"'
            or routine_definition_normalized_legacy
              ~* '(^|[^a-zA-Z0-9_$])u&"'
          )
        )
        or routine_definition_normalized_standard ~* (
          '"?public"?[[:space:]]*\.[[:space:]]*"?'
          || pgcrypto_member_name_pattern
          || '"?[[:space:]]*\('
        )
        or routine_definition_normalized_standard ~* (
          '(^|[^a-zA-Z0-9_."])"?'
          || pgcrypto_noncore_member_name_pattern
          || '"?[[:space:]]*\('
        )
        or routine_definition_normalized_legacy ~* (
          '"?public"?[[:space:]]*\.[[:space:]]*"?'
          || pgcrypto_member_name_pattern
          || '"?[[:space:]]*\('
        )
        or routine_definition_normalized_legacy ~* (
          '(^|[^a-zA-Z0-9_."])"?'
          || pgcrypto_noncore_member_name_pattern
          || '"?[[:space:]]*\('
        ) then
        raise exception
          'final application pgcrypto boundary is inconsistent at %',
          routine.identity;
      end if;
    end loop;
    if encode(extensions.digest('abc', 'sha256'), 'hex') <>
      'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad' then
      raise exception 'pgcrypto digest verification vector failed';
    end if;
    raise notice 'pgcrypto convergence already applied; final boundary verified';
    return;
  end if;

  if not has_schema_privilege(current_user, 'public', 'CREATE') then
    raise exception 'current migration role cannot stage pgcrypto in public';
  end if;
  end if;

  for routine in
    select
      procedure.oid,
      procedure.oid::regprocedure::text as identity,
      procedure.pronamespace as namespace_oid,
      procedure.prosrc as source,
      namespace.nspname as namespace_name,
      language.lanname as language_name,
      procedure.prosecdef,
      procedure.proconfig
    from pg_proc as procedure
    join pg_namespace as namespace
      on namespace.oid = procedure.pronamespace
    join pg_language as language on language.oid = procedure.prolang
    where procedure.prokind in ('f', 'p')
      and procedure.prosqlbody is null
      and namespace.nspname <> 'information_schema'
      and namespace.nspname !~ '^pg_'
      and (
        procedure.prosrc ~* pgcrypto_noncore_member_token_pattern
        or procedure.prosrc ~* '(^|[^a-zA-Z0-9_$])u&"'
        or (
          procedure.prosrc ~* '(^|[^a-zA-Z0-9_])"?gen_random_uuid"?([^a-zA-Z0-9_]|$)'
          and procedure.prosrc
            ~* '(^|[^a-zA-Z0-9_])"?(public|extensions)"?([^a-zA-Z0-9_]|$)'
        )
      )
      and not exists (
        select 1
        from pg_depend as dependency
        where dependency.classid = 'pg_proc'::regclass
          and dependency.objid = procedure.oid
          and dependency.refclassid = 'pg_extension'::regclass
          and dependency.refobjid = extension_oid
          and dependency.deptype = 'e'
      )
    order by namespace.nspname, procedure.proname, procedure.oid
  loop
    routine_definition := routine.source;
    routine_definition_normalized_standard := routine_definition;
    routine_definition_normalized_legacy := routine_definition;
    if routine.language_name in ('sql', 'plpgsql') then
      scan_mode_count := case
        when strpos(routine_definition, chr(92)) > 0 then 2
        else 1
      end;
      for scan_mode in 1..scan_mode_count loop
        routine_definition_normalized := '';
      source_position := 1;
      source_length := length(routine_definition);
      code_start := 1;
      while source_position <= source_length loop
        if substring(routine_definition from source_position for 2) = '--' then
          routine_definition_normalized := routine_definition_normalized
            || substring(
              routine_definition from code_start
              for source_position - code_start
            ) || ' ';
          source_position := source_position + 2;
          routine_tail := substring(routine_definition from source_position);
          line_feed_offset := strpos(routine_tail, chr(10));
          carriage_return_offset := strpos(routine_tail, chr(13));
          closing_offset := case
            when line_feed_offset = 0 then carriage_return_offset
            when carriage_return_offset = 0 then line_feed_offset
            else least(line_feed_offset, carriage_return_offset)
          end;
          if closing_offset = 0 then
            source_position := source_length + 1;
          else
            source_position := source_position + closing_offset - 1;
          end if;
          code_start := source_position;
        elsif substring(routine_definition from source_position for 2) =
            '/*' then
          routine_definition_normalized := routine_definition_normalized
            || substring(
              routine_definition from code_start
              for source_position - code_start
            ) || ' ';
          comment_depth := 1;
          source_position := source_position + 2;
          while source_position <= source_length and comment_depth > 0 loop
            routine_tail := substring(routine_definition from source_position);
            opening_offset := strpos(routine_tail, '/*');
            closing_offset := strpos(routine_tail, '*/');
            if closing_offset = 0 then
              source_position := source_length + 1;
              comment_depth := 0;
            elsif opening_offset > 0 and opening_offset < closing_offset then
              comment_depth := comment_depth + 1;
              source_position := source_position + opening_offset + 1;
            else
              comment_depth := comment_depth - 1;
              source_position := source_position + closing_offset + 1;
            end if;
          end loop;
          code_start := source_position;
        elsif substring(routine_definition from source_position for 1) =
            chr(39) then
          routine_definition_normalized := routine_definition_normalized
            || substring(
              routine_definition from code_start
              for source_position - code_start
            ) || ' ';
          escape_string := scan_mode = 2 or (
            source_position >= 2
            and lower(substring(
              routine_definition from source_position - 1 for 1
            )) = 'e'
            and (
              source_position = 2
              or substring(
                routine_definition from source_position - 2 for 1
              ) !~ '[a-zA-Z0-9_$]'
            )
          ) or (
            source_position >= 3
            and lower(substring(
              routine_definition from source_position - 2 for 2
            )) = 'u&'
            and (
              source_position = 3
              or substring(
                routine_definition from source_position - 3 for 1
              ) !~ '[a-zA-Z0-9_$]'
            )
          );
          source_position := source_position + 1;
          while source_position <= source_length loop
            routine_tail := substring(routine_definition from source_position);
            closing_offset := strpos(routine_tail, chr(39));
            backslash_offset := case
              when escape_string then strpos(routine_tail, chr(92))
              else 0
            end;
            if backslash_offset > 0
              and (
                closing_offset = 0
                or backslash_offset < closing_offset
              ) then
              source_position := source_position + backslash_offset + 1;
            elsif closing_offset = 0 then
              source_position := source_length + 1;
              exit;
            else
              source_position := source_position + closing_offset - 1;
              if substring(
                routine_definition from source_position for 2
              ) = chr(39) || chr(39) then
              source_position := source_position + 2;
              else
                source_position := source_position + 1;
                exit;
              end if;
            end if;
          end loop;
          code_start := source_position;
        elsif substring(routine_definition from source_position for 1) =
            chr(34) then
          source_position := source_position + 1;
          while source_position <= source_length loop
            routine_tail := substring(routine_definition from source_position);
            closing_offset := strpos(routine_tail, chr(34));
            if closing_offset = 0 then
              source_position := source_length + 1;
              exit;
            end if;
            source_position := source_position + closing_offset - 1;
            if substring(
              routine_definition from source_position for 2
            ) = chr(34) || chr(34) then
              source_position := source_position + 2;
            else
              source_position := source_position + 1;
              exit;
            end if;
          end loop;
        elsif substring(routine_definition from source_position for 1) = '$' then
          dollar_quote_tag := case
            when substring(
              routine_definition from source_position for 2
            ) = '$$' then '$$'
            else substring(
              substring(routine_definition from source_position)
              from $tag_pattern$^\$[[:alpha:]_][[:alnum:]_]*\$$tag_pattern$
            )
          end;
          if dollar_quote_tag is null then
            source_position := source_position + 1;
          else
            routine_definition_normalized := routine_definition_normalized
              || substring(
                routine_definition from code_start
                for source_position - code_start
              ) || ' ';
            source_position := source_position + length(dollar_quote_tag);
            closing_offset := strpos(
              substring(routine_definition from source_position),
              dollar_quote_tag
            );
            if closing_offset = 0 then
              source_position := source_length + 1;
            else
              source_position := source_position + closing_offset - 1
                + length(dollar_quote_tag);
            end if;
            code_start := source_position;
          end if;
        else
          source_position := source_position + 1;
        end if;
      end loop;
      routine_definition_normalized := routine_definition_normalized
        || substring(routine_definition from code_start);
        if scan_mode = 1 then
          routine_definition_normalized_standard :=
            routine_definition_normalized;
        else
          routine_definition_normalized_legacy :=
            routine_definition_normalized;
        end if;
      end loop;
      if scan_mode_count = 1 then
        routine_definition_normalized_legacy :=
          routine_definition_normalized_standard;
      end if;
    end if;
    select
      exists (
        select 1
        from unnest(coalesce(routine.proconfig, array[]::text[])) as setting
        where lower(btrim(split_part(setting, '=', 1))) = 'search_path'
      ),
      exists (
        select 1
        from unnest(coalesce(routine.proconfig, array[]::text[])) as setting
        cross join lateral regexp_split_to_table(
          substring(setting from position('=' in setting) + 1),
          ','
        ) as entry(value)
        where lower(btrim(split_part(setting, '=', 1))) = 'search_path'
          and lower(btrim(entry.value, E' \t"')) = 'extensions'
      )
    into has_function_search_path, has_extensions_search_path;

    has_default_extensions_search_path := false;
    if not has_function_search_path then
      select exists (
        select 1
        from pg_db_role_setting as role_setting
        cross join lateral unnest(role_setting.setconfig) as setting
        cross join lateral regexp_split_to_table(
          substring(setting from position('=' in setting) + 1),
          ','
        ) as entry(value)
        where role_setting.setdatabase in (
            0,
            (select database.oid
             from pg_database as database
             where database.datname = current_database())
          )
          and lower(btrim(split_part(setting, '=', 1))) = 'search_path'
          and lower(btrim(entry.value, E' \t"')) = 'extensions'
          and (
            (
              role_setting.setrole = 0
              and exists (
                select 1
                from pg_roles as login_role
                where login_role.rolcanlogin
                  and has_schema_privilege(
                    login_role.oid,
                    routine.namespace_oid,
                    'USAGE'
                  )
                  and has_function_privilege(
                    login_role.oid,
                    routine.oid,
                    'EXECUTE'
                  )
              )
            )
            or (
              role_setting.setrole <> 0
              and has_schema_privilege(
                role_setting.setrole,
                routine.namespace_oid,
                'USAGE'
              )
              and has_function_privilege(
                role_setting.setrole,
                routine.oid,
                'EXECUTE'
              )
            )
          )
      ) into has_default_extensions_search_path;
      has_extensions_search_path := has_default_extensions_search_path;
    end if;

    for scan_mode in 1..scan_mode_count loop
      routine_definition_normalized := case
        when scan_mode = 1 then routine_definition_normalized_standard
        else routine_definition_normalized_legacy
      end;
      if routine.language_name in ('sql', 'plpgsql')
        and routine_definition_normalized
          ~* '(^|[^a-zA-Z0-9_$])u&"' then
        raise exception
          'unsafe pgcrypto caller % (language %, security_definer %, config %, catalog_default %) requires review',
          routine.identity,
          routine.language_name,
          routine.prosecdef,
          coalesce(routine.proconfig::text, '{}'),
          has_default_extensions_search_path;
      end if;
      unsafe_caller := false;
      if extension_schema is null then
        if routine_definition_normalized ~* (
          '"?public"?[[:space:]]*\.[[:space:]]*"?'
          || pgcrypto_member_name_pattern
          || '"?[[:space:]]*\('
        ) or routine_definition_normalized ~* (
          '(^|[^a-zA-Z0-9_."])"?'
          || pgcrypto_noncore_member_name_pattern
          || '"?[[:space:]]*\('
        ) then
          unsafe_caller := true;
        end if;
      elsif extension_schema = 'public' then
        -- The convergence migration only rewrites the exact lowercase
        -- `public.digest` token in these three repository-owned schemas.
        -- Everything else would survive the move and break afterwards.
        if routine_definition_normalized ~* (
          '"?public"?[[:space:]]*\.[[:space:]]*"?'
          || pgcrypto_member_name_pattern
          || '"?[[:space:]]*\('
        ) then
          unsafe_caller := routine.namespace_name not in (
            'private', 'api', 'worker_api'
          );
          routine_definition_without_canonical := replace(
            routine_definition_normalized,
            'public.digest',
            ''
          );
          if routine_definition_without_canonical
              ~* (
                '"?public"?[[:space:]]*\.[[:space:]]*"?'
                || pgcrypto_member_name_pattern
                || '"?[[:space:]]*\('
              ) then
            unsafe_caller := true;
          end if;
        end if;
        -- PostgreSQL 17 resolves unqualified gen_random_uuid() to the core
        -- pg_catalog routine, not pgcrypto's compatibility wrapper. Every
        -- other unqualified member name is ambiguous and fails closed.
        if routine_definition_normalized ~* (
          '(^|[^a-zA-Z0-9_."])"?'
          || pgcrypto_noncore_member_name_pattern
          || '"?[[:space:]]*\('
        ) then
          unsafe_caller := true;
        end if;
      end if;
      if routine_definition_normalized ~* (
        '"?extensions"?[[:space:]]*\.[[:space:]]*"?'
        || pgcrypto_member_name_pattern
        || '"?[[:space:]]*\('
      ) or (
        has_extensions_search_path
        and routine_definition_normalized ~* (
          '(^|[^a-zA-Z0-9_."])"?'
          || pgcrypto_member_name_pattern
          || '"?[[:space:]]*\('
        )
      ) then
        unsafe_caller := true;
      end if;
      if unsafe_caller then
        raise exception
          'unsafe pgcrypto caller % (language %, security_definer %, config %, catalog_default %) requires review',
          routine.identity,
          routine.language_name,
          routine.prosecdef,
          coalesce(routine.proconfig::text, '{}'),
          has_default_extensions_search_path;
      end if;
    end loop;
  end loop;

  if extension_oid is null then
    raise notice
      'pgcrypto absent on empty PG17 database; historical 0001 remains installer';
    return;
  end if;

  if extension_schema = 'public' then
    if encode(public.digest('abc', 'sha256'), 'hex') <>
      'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad' then
      raise exception 'pgcrypto digest verification vector failed';
    end if;
    raise notice 'pgcrypto already staged in public for historical replay';
    return;
  end if;

  select array_agg(
    format('%s:%s:%s', dependency.classid, dependency.objid, dependency.objsubid)
    order by dependency.classid, dependency.objid, dependency.objsubid
  )
  into member_oids_before
  from pg_depend as dependency
  where dependency.refclassid = 'pg_extension'::regclass
    and dependency.refobjid = extension_oid
    and dependency.deptype = 'e';

  select md5(string_agg(
    (to_jsonb(procedure) - 'pronamespace')::text,
    E'\n' order by procedure.oid
  ))
  into digest_metadata_before
  from pg_proc as procedure
  where procedure.oid in (digest_bytea_oid, digest_text_oid);

  select md5(coalesce(string_agg(
    (to_jsonb(procedure) - 'pronamespace')::text,
    E'\n' order by procedure.oid
  ), ''))
  into member_routine_metadata_before
  from pg_proc as procedure
  where exists (
    select 1
    from pg_depend as dependency
    where dependency.classid = 'pg_proc'::regclass
      and dependency.objid = procedure.oid
      and dependency.refclassid = 'pg_extension'::regclass
      and dependency.refobjid = extension_oid
      and dependency.deptype = 'e'
  );

  select coalesce(namespace.nspacl::text, '<default>')
  into public_acl_before
  from pg_namespace as namespace
  where namespace.nspname = 'public';
  select coalesce(namespace.nspacl::text, '<default>')
  into extensions_acl_before
  from pg_namespace as namespace
  where namespace.nspname = 'extensions';

  alter extension pgcrypto set schema public;

  select array_agg(
    format('%s:%s:%s', dependency.classid, dependency.objid, dependency.objsubid)
    order by dependency.classid, dependency.objid, dependency.objsubid
  )
  into member_oids_after
  from pg_depend as dependency
  where dependency.refclassid = 'pg_extension'::regclass
    and dependency.refobjid = extension_oid
    and dependency.deptype = 'e';

  select md5(string_agg(
    (to_jsonb(procedure) - 'pronamespace')::text,
    E'\n' order by procedure.oid
  ))
  into digest_metadata_after
  from pg_proc as procedure
  where procedure.oid in (digest_bytea_oid, digest_text_oid);

  select md5(coalesce(string_agg(
    (to_jsonb(procedure) - 'pronamespace')::text,
    E'\n' order by procedure.oid
  ), ''))
  into member_routine_metadata_after
  from pg_proc as procedure
  where exists (
    select 1
    from pg_depend as dependency
    where dependency.classid = 'pg_proc'::regclass
      and dependency.objid = procedure.oid
      and dependency.refclassid = 'pg_extension'::regclass
      and dependency.refobjid = extension_oid
      and dependency.deptype = 'e'
  );

  select coalesce(namespace.nspacl::text, '<default>')
  into public_acl_after
  from pg_namespace as namespace
  where namespace.nspname = 'public';
  select coalesce(namespace.nspacl::text, '<default>')
  into extensions_acl_after
  from pg_namespace as namespace
  where namespace.nspname = 'extensions';

  if (select extension.extnamespace from pg_extension as extension
      where extension.oid = extension_oid) <> to_regnamespace('public')
    or (select extension.extowner from pg_extension as extension
        where extension.oid = extension_oid) is distinct from extension_owner_oid
    or (select extension.extrelocatable from pg_extension as extension
        where extension.oid = extension_oid)
      is distinct from extension_relocatable
    or (select extension.extversion from pg_extension as extension
        where extension.oid = extension_oid) <> extension_version
    or to_regprocedure('public.digest(bytea,text)')::oid
      is distinct from digest_bytea_oid
    or to_regprocedure('public.digest(text,text)')::oid
      is distinct from digest_text_oid
    or to_regprocedure('extensions.digest(bytea,text)') is not null
    or to_regprocedure('extensions.digest(text,text)') is not null
    or member_oids_after is distinct from member_oids_before
    or digest_metadata_after is distinct from digest_metadata_before
    or member_routine_metadata_after is distinct from
      member_routine_metadata_before
    or public_acl_after is distinct from public_acl_before
    or extensions_acl_after is distinct from extensions_acl_before then
    raise exception 'pgcrypto catalog identity changed during replay staging';
  end if;
  if encode(public.digest('abc', 'sha256'), 'hex') <>
    'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad' then
    raise exception 'pgcrypto digest verification vector failed';
  end if;

  raise notice
    'pgcrypto staged from extensions to public for historical migration replay';
end;
$pgcrypto_replay_preflight$;

commit;
