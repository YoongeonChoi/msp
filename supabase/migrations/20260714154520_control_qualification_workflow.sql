-- Operational maker/checker artifact provenance and qualification workflow.
--
-- This migration is deliberately local-first.  It creates no hosted resources,
-- does not resolve unknown executions, and does not add any order transport.

alter table private.control_evidence
  drop constraint if exists control_evidence_evidence_type_check;
alter table private.control_evidence
  add constraint control_evidence_evidence_type_check check (
    evidence_type in (
      'account_opening',
      'contract_test_contract',
      'risk_review',
      'release',
      'incident',
      'restore_drill',
      'reference_bundle',
      'g1_qualification',
      'g2_qualification',
      'contract_qualification'
    )
  );

alter table private.control_evidence
  drop constraint if exists control_evidence_artifact_uri_check;
alter table private.control_evidence
  add constraint control_evidence_artifact_uri_check check (
    (
      artifact_uri ~ '^https://[^/?#[:space:]]+/'
      and artifact_uri !~ '[?#]'
    )
    or artifact_uri = 'urn:sha256:' || artifact_sha256
  );

alter table private.account_snapshots
  add column checkpoint_schema_version smallint not null default 0,
  add column ledger_checkpoint_sha256 text;
alter table private.account_snapshots
  drop constraint account_snapshots_account_id_environment_source_type_source_key;
create unique index account_snapshot_legacy_source_unique
  on private.account_snapshots (account_id, environment, source_type, source_id)
  where checkpoint_schema_version = 0;
alter table private.account_snapshots
  add constraint account_snapshot_checkpoint_shape_check check (
    (
      checkpoint_schema_version = 0
      and ledger_checkpoint_sha256 is null
    )
    or (
      checkpoint_schema_version = 1
      and ledger_checkpoint_sha256 ~ '^[0-9a-f]{64}$'
      and source_type = 'ledger_projection'
    )
  );
create unique index account_snapshot_v1_ledger_state_unique
  on private.account_snapshots (
    account_id, environment, ledger_checkpoint_sha256
  )
  where checkpoint_schema_version = 1;

create table private.control_approval_requests (
  id uuid primary key,
  request_kind text not null check (
    request_kind in ('reference_bundle', 'qualification_finalization')
  ),
  account_id text not null references private.trading_accounts(account_id),
  environment text not null check (environment in ('paper', 'contract_test')),
  payload jsonb not null check (jsonb_typeof(payload) = 'object'),
  payload_sha256 text not null check (payload_sha256 ~ '^[0-9a-f]{64}$'),
  requester_user_id uuid not null references auth.users(id),
  requested_at timestamptz not null,
  expires_at timestamptz not null,
  idempotency_key text not null,
  created_at timestamptz not null default clock_timestamp(),
  constraint control_approval_request_window_check check (
    expires_at > requested_at
    and expires_at <= requested_at + interval '24 hours'
  ),
  constraint control_approval_request_identity_check check (
    idempotency_key ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
  ),
  unique (request_kind, idempotency_key),
  unique (id, payload_sha256)
);

create table private.control_approval_reviews (
  id uuid primary key,
  request_id uuid not null unique references private.control_approval_requests(id),
  decision text not null check (decision in ('approved', 'rejected')),
  payload jsonb not null check (jsonb_typeof(payload) = 'object'),
  payload_sha256 text not null check (payload_sha256 ~ '^[0-9a-f]{64}$'),
  expected_request_payload_sha256 text not null check (
    expected_request_payload_sha256 ~ '^[0-9a-f]{64}$'
  ),
  reviewer_user_id uuid not null references auth.users(id),
  reviewed_at timestamptz not null,
  created_at timestamptz not null default clock_timestamp(),
  unique (request_id, payload_sha256)
);

create table private.reference_bundle_materializations (
  request_id uuid primary key references private.control_approval_requests(id),
  review_id uuid not null unique references private.control_approval_reviews(id),
  evidence_id uuid not null references private.control_evidence(id),
  policy_id uuid not null references private.paper_execution_policies(id),
  cost_schedule_id uuid not null references private.execution_cost_schedules(id),
  calendar_id uuid not null references private.market_calendars(id),
  execution_model_id uuid not null references private.paper_execution_model_registry(id),
  provider_contract_id uuid references private.provider_contract_registry(id),
  execution_model_sha256 text not null check (
    execution_model_sha256 ~ '^[0-9a-f]{64}$'
  ),
  provider_contract_sha256 text check (
    provider_contract_sha256 is null
    or provider_contract_sha256 ~ '^[0-9a-f]{64}$'
  ),
  bundle_sha256 text not null unique check (bundle_sha256 ~ '^[0-9a-f]{64}$'),
  materialized_at timestamptz not null default clock_timestamp()
);

create table private.qualification_runs (
  id uuid primary key,
  run_kind text not null check (
    run_kind in ('g1', 'g2', 'contract_qualification')
  ),
  account_id text not null references private.trading_accounts(account_id),
  environment text not null check (environment in ('paper', 'contract_test')),
  reference_bundle_request_id uuid not null
    references private.reference_bundle_materializations(request_id),
  account_snapshot_id uuid not null references private.account_snapshots(id),
  account_snapshot_sequence bigint not null check (account_snapshot_sequence > 0),
  ledger_checkpoint_sha256 text not null check (
    ledger_checkpoint_sha256 ~ '^[0-9a-f]{64}$'
  ),
  release_sha text not null check (release_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
  suite_version text not null check (nullif(btrim(suite_version), '') is not null),
  result text not null check (result in ('pass', 'fail')),
  evidence_manifest jsonb not null check (jsonb_typeof(evidence_manifest) = 'object'),
  evidence_sha256 text not null check (evidence_sha256 ~ '^[0-9a-f]{64}$'),
  started_at timestamptz not null,
  completed_at timestamptz not null,
  worker_id uuid not null,
  created_at timestamptz not null default clock_timestamp(),
  constraint qualification_run_window_check check (completed_at >= started_at),
  constraint qualification_run_environment_check check (
    (run_kind <> 'contract_qualification')
    or environment = 'contract_test'
  ),
  unique (run_kind, account_id, evidence_sha256)
);

alter table private.qualifications
  add column account_id text references private.trading_accounts(account_id),
  add column checkpoint_schema_version smallint not null default 0,
  add column account_snapshot_id uuid references private.account_snapshots(id),
  add column account_snapshot_sequence bigint,
  add column ledger_checkpoint_sha256 text,
  add column reference_bundle_request_id uuid
    references private.reference_bundle_materializations(request_id),
  add column g1_run_id uuid references private.qualification_runs(id),
  add column g2_run_id uuid references private.qualification_runs(id),
  add column contract_run_id uuid references private.qualification_runs(id),
  add column approval_request_id uuid unique
    references private.control_approval_requests(id);

alter table private.qualifications
  add constraint qualification_v1_checkpoint_shape_check check (
    status <> 'qualified'
    or (
      checkpoint_schema_version = 1
      and account_id is not null
      and account_snapshot_id is not null
      and account_snapshot_sequence > 0
      and ledger_checkpoint_sha256 ~ '^[0-9a-f]{64}$'
      and reference_bundle_request_id is not null
      and g1_run_id is not null
      and g2_run_id is not null
      and approval_request_id is not null
      and ledger_checkpoint = 'snapshot-v1:'
        || account_snapshot_id::text || ':'
        || account_snapshot_sequence::text || ':'
        || ledger_checkpoint_sha256
      and (
        (environment = 'paper' and contract_run_id is null)
        or (environment = 'contract_test' and contract_run_id is not null)
      )
    )
  ) not valid;

create unique index qualification_v1_g1_run_unique
  on private.qualifications (g1_run_id) where checkpoint_schema_version = 1;
create unique index qualification_v1_g2_run_unique
  on private.qualifications (g2_run_id) where checkpoint_schema_version = 1;
create unique index qualification_v1_contract_run_unique
  on private.qualifications (contract_run_id)
  where checkpoint_schema_version = 1 and contract_run_id is not null;

create table private.qualification_finalizations (
  request_id uuid primary key references private.control_approval_requests(id),
  review_id uuid not null unique references private.control_approval_reviews(id),
  qualification_id uuid not null unique references private.qualifications(id),
  g1_evidence_id uuid not null references private.control_evidence(id),
  g2_evidence_id uuid not null references private.control_evidence(id),
  contract_evidence_id uuid references private.control_evidence(id),
  finalized_at timestamptz not null default clock_timestamp()
);

create or replace function private.guard_control_approval_review_v1()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  request_row private.control_approval_requests%rowtype;
begin
  select * into request_row
  from private.control_approval_requests
  where id = new.request_id;
  if not found then
    raise exception 'control_approval_request_not_found' using errcode = 'P0002';
  end if;
  if request_row.requester_user_id = new.reviewer_user_id then
    raise exception 'control_approval_self_review_forbidden' using errcode = '23514';
  end if;
  if new.expected_request_payload_sha256 <> request_row.payload_sha256 then
    raise exception 'control_approval_request_digest_mismatch' using errcode = '23514';
  end if;
  return new;
end;
$$;

create trigger guard_control_approval_review_v1
  before insert on private.control_approval_reviews
  for each row execute function private.guard_control_approval_review_v1();

create trigger reject_control_approval_request_mutation
  before update or delete on private.control_approval_requests
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_control_approval_review_mutation
  before update or delete on private.control_approval_reviews
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_reference_bundle_materialization_mutation
  before update or delete on private.reference_bundle_materializations
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_qualification_run_mutation
  before update or delete on private.qualification_runs
  for each row execute function private.reject_immutable_execution_mutation();
create trigger reject_qualification_finalization_mutation
  before update or delete on private.qualification_finalizations
  for each row execute function private.reject_immutable_execution_mutation();

alter table private.control_approval_requests enable row level security;
alter table private.control_approval_reviews enable row level security;
alter table private.reference_bundle_materializations enable row level security;
alter table private.qualification_runs enable row level security;
alter table private.qualification_finalizations enable row level security;

revoke all on table
  private.control_approval_requests,
  private.control_approval_reviews,
  private.reference_bundle_materializations,
  private.qualification_runs,
  private.qualification_finalizations
from public, anon, authenticated, service_role;

create or replace function private.sha256_jsonb_v1(p_payload jsonb)
returns text
language sql
immutable
security definer
set search_path = ''
as $$
  select pg_catalog.encode(
    public.digest(pg_catalog.convert_to(p_payload::text, 'UTF8'), 'sha256'),
    'hex'
  );
$$;

create or replace function private.canonical_numeric_v1(p_value numeric)
returns text
language sql
immutable
security definer
set search_path = ''
as $$
  select case
    when p_value = 0 then '0'
    else pg_catalog.trim_scale(p_value)::text
  end;
$$;

create or replace function private.reference_component_sha256_v1(
  p_component_kind text,
  p_component jsonb
)
returns text
language plpgsql
immutable
security definer
set search_path = ''
as $$
begin
  if jsonb_typeof(p_component) <> 'object' then
    raise exception 'reference_component_object_required' using errcode = '22023';
  end if;
  if p_component_kind not in (
    'policy', 'cost_schedule', 'market_calendar',
    'market_calendar_session', 'execution_model', 'provider_contract'
  ) then
    raise exception 'reference_component_kind_invalid' using errcode = '22023';
  end if;
  return private.sha256_jsonb_v1(
    case p_component_kind
      when 'policy' then p_component - 'policy_sha256'
      when 'cost_schedule' then p_component - 'schedule_sha256'
      when 'market_calendar' then p_component - 'calendar_sha256'
      when 'market_calendar_session' then p_component - 'session_sha256'
      when 'execution_model' then p_component - 'model_sha256'
      when 'provider_contract' then p_component - 'contract_sha256'
    end
  );
end;
$$;

create or replace function private.account_ledger_payload_v1(
  p_account_id text,
  p_environment text
)
returns jsonb
language sql
stable
security definer
set search_path = ''
as $$
  select jsonb_build_object(
    'schema_version', 1,
    'account_id', p_account_id,
    'environment', p_environment,
    'cash', coalesce((
      select jsonb_build_object(
        'settled_cash_krw', private.canonical_numeric_v1(cash.settled_cash_krw),
        'reserved_cash_krw', private.canonical_numeric_v1(cash.reserved_cash_krw),
        'pending_debit_cash_krw', private.canonical_numeric_v1(cash.pending_debit_cash_krw),
        'projection_version', cash.projection_version,
        'last_journal_entry_id', cash.last_journal_entry_id
      )
      from private.cash_balance_projection as cash
      where cash.account_id = p_account_id
    ), 'null'::jsonb),
    'positions', coalesce((
      select jsonb_agg(jsonb_build_object(
        'symbol', position.symbol,
        'quantity', position.quantity,
        'reserved_quantity', position.reserved_quantity,
        'pending_sell_quantity', position.pending_sell_quantity,
        'average_cost_krw', private.canonical_numeric_v1(position.average_cost_krw),
        'projection_version', position.projection_version
      ) order by position.symbol)
      from private.position_projection as position
      where position.account_id = p_account_id
    ), '[]'::jsonb),
    'journal', coalesce((
      select jsonb_agg(jsonb_build_object(
        'id', transaction.id,
        'source_type', transaction.source_type,
        'source_id', transaction.source_id,
        'correlation_id', transaction.correlation_id,
        'occurred_at', private.utc_iso8601(transaction.occurred_at),
        'posted_at', private.utc_iso8601(transaction.posted_at),
        'release_sha', transaction.release_sha,
        'postings', coalesce((
          select jsonb_agg(jsonb_build_object(
            'id', posting.id,
            'ledger_code', ledger.ledger_code,
            'side', posting.side,
            'amount_krw', private.canonical_numeric_v1(posting.amount_krw)
          ) order by ledger.ledger_code, posting.side, posting.id)
          from private.accounting_postings as posting
          join private.ledger_accounts as ledger on ledger.id = posting.ledger_account_id
          where posting.journal_entry_id = transaction.id
        ), '[]'::jsonb)
      ) order by transaction.occurred_at, transaction.id)
      from private.accounting_transactions as transaction
      where transaction.account_id = p_account_id
        and transaction.environment = p_environment
    ), '[]'::jsonb)
  );
$$;

create or replace function private.compute_ledger_checkpoint_sha256_v1(
  p_account_id text,
  p_environment text
)
returns text
language sql
stable
security definer
set search_path = ''
as $$
  select private.sha256_jsonb_v1(
    private.account_ledger_payload_v1(p_account_id, p_environment)
  );
$$;

create or replace function private.compute_position_projection_sha256_v1(
  p_account_id text
)
returns text
language sql
stable
security definer
set search_path = ''
as $$
  select private.sha256_jsonb_v1(coalesce((
    select jsonb_agg(jsonb_build_object(
      'symbol', position.symbol,
      'quantity', position.quantity,
      'reserved_quantity', position.reserved_quantity,
      'pending_sell_quantity', position.pending_sell_quantity,
      'average_cost_krw', private.canonical_numeric_v1(position.average_cost_krw),
      'projection_version', position.projection_version
    ) order by position.symbol)
    from private.position_projection as position
    where position.account_id = p_account_id
  ), '[]'::jsonb));
$$;

revoke execute on function
  private.sha256_jsonb_v1(jsonb),
  private.canonical_numeric_v1(numeric),
  private.reference_component_sha256_v1(text, jsonb),
  private.account_ledger_payload_v1(text, text),
  private.compute_ledger_checkpoint_sha256_v1(text, text),
  private.compute_position_projection_sha256_v1(text)
from public, anon, authenticated, service_role;

revoke execute on function private.guard_control_approval_review_v1()
from public, anon, authenticated, service_role;

-- Extend the existing one-time step-up contract without weakening any of the
-- operation-command shapes introduced in 0020.
create or replace function private.validate_command_draft_v1(
  p_action text,
  p_command_type text,
  p_payload jsonb
)
returns void
language plpgsql
immutable
security definer
set search_path = ''
as $$
begin
  if p_action = 'request' and p_command_type = 'resolve_unknown_execution' then
    perform private.assert_exact_json_keys(p_payload, array[
      'schema_version', 'request_id', 'environment', 'idempotency_key',
      'command_type', 'break_id', 'evidence_sha256', 'reason_code',
      'expected_break_state', 'expected_break_revision',
      'requested_at', 'expires_at'
    ]);
  elsif p_action = 'request' and p_command_type = 'pause_paper' then
    perform private.assert_exact_json_keys(p_payload, array[
      'schema_version', 'request_id', 'environment', 'idempotency_key',
      'expected_state_version', 'requested_at', 'expires_at', 'command_type',
      'reason_code'
    ]);
  elsif p_action = 'request' and p_command_type in (
    'resume_paper', 'activate_paper_strategy', 'start_contract_test',
    'apply_risk_policy_version'
  ) then
    perform private.assert_exact_json_keys(p_payload, array[
      'schema_version', 'request_id', 'environment', 'idempotency_key',
      'expected_state_version', 'requested_at', 'expires_at', 'command_type',
      'qualification_id', 'strategy_version_id', 'risk_policy_version_id',
      'release_sha', 'ledger_checkpoint', 'reason_code'
    ]);
  elsif p_action = 'request' and p_command_type = 'reference_bundle' then
    perform private.assert_exact_json_keys(p_payload, array[
      'schema_version', 'request_id', 'command_type', 'idempotency_key',
      'account_id', 'environment', 'release_sha', 'bundle_version',
      'bundle_sha256', 'artifact_uri', 'policy', 'cost_schedule',
      'market_calendar', 'market_calendar_sessions', 'execution_model',
      'provider_contract', 'requested_at', 'expires_at', 'reason_code'
    ]);
  elsif p_action = 'request' and p_command_type = 'qualification_finalization' then
    perform private.assert_exact_json_keys(p_payload, array[
      'schema_version', 'request_id', 'command_type', 'idempotency_key',
      'qualification_id', 'account_id', 'environment',
      'reference_bundle_request_id', 'account_snapshot_id',
      'account_snapshot_sequence', 'ledger_checkpoint_sha256',
      'dataset_version', 'execution_policy_version',
      'execution_policy_sha256', 'risk_policy_version_id',
      'risk_policy_sha256', 'strategy_version_id',
      'provider_contract_version', 'provider_openapi_sha256',
      'g1_run_id', 'g2_run_id', 'contract_run_id', 'release_sha',
      'valid_from', 'valid_until', 'requested_at', 'expires_at',
      'reason_code'
    ]);
  elsif p_action = 'review' and p_command_type = 'resolve_unknown_execution' then
    perform private.assert_exact_json_keys(p_payload, array[
      'schema_version', 'review_id', 'command_id', 'command_type',
      'reviewer_role', 'decision', 'reason_code',
      'expected_receipt_revision', 'expected_break_revision',
      'evidence_sha256', 'reviewed_at'
    ]);
  elsif p_action = 'review' and p_command_type in (
    'pause_paper', 'resume_paper', 'activate_paper_strategy',
    'start_contract_test', 'apply_risk_policy_version'
  ) then
    perform private.assert_exact_json_keys(p_payload, array[
      'schema_version', 'review_id', 'command_id', 'command_type',
      'reviewer_role', 'decision', 'reason_code',
      'expected_receipt_revision', 'reviewed_at'
    ]);
  elsif p_action = 'review' and p_command_type in (
    'reference_bundle', 'qualification_finalization'
  ) then
    perform private.assert_exact_json_keys(p_payload, array[
      'schema_version', 'review_id', 'request_id', 'command_type',
      'reviewer_role', 'decision', 'reason_code',
      'expected_request_payload_sha256', 'reviewed_at'
    ]);
  else
    raise exception 'step_up_draft_binding_invalid' using errcode = '22023';
  end if;
  if coalesce((p_payload->>'schema_version')::integer, -1) <> 1
     or p_payload->>'command_type' is distinct from p_command_type then
    raise exception 'step_up_draft_schema_invalid' using errcode = '22023';
  end if;
  if jsonb_typeof(p_payload->'schema_version') <> 'number'
     or (
       p_action = 'review'
       and p_command_type not in ('reference_bundle', 'qualification_finalization')
       and jsonb_typeof(p_payload->'expected_receipt_revision') <> 'number'
     )
     or (
       p_action = 'request'
       and p_command_type = 'resolve_unknown_execution'
       and jsonb_typeof(p_payload->'expected_break_revision') <> 'number'
     )
     or (
       p_action = 'request'
       and p_command_type not in (
         'resolve_unknown_execution', 'reference_bundle',
         'qualification_finalization'
       )
       and jsonb_typeof(p_payload->'expected_state_version') <> 'number'
     )
     or (
       p_action = 'review'
       and p_command_type = 'resolve_unknown_execution'
       and jsonb_typeof(p_payload->'expected_break_revision') <> 'number'
     )
     or (
       p_action = 'request'
       and p_command_type = 'qualification_finalization'
       and jsonb_typeof(p_payload->'account_snapshot_sequence') <> 'number'
     ) then
    raise exception 'step_up_draft_json_type_invalid' using errcode = '22023';
  end if;
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'step_up_draft_schema_invalid' using errcode = '22023';
end;
$$;

create or replace function private.reference_bundle_content_v1(p_payload jsonb)
returns jsonb
language sql
immutable
security definer
set search_path = ''
as $$
  select jsonb_build_object(
    'schema_version', 1,
    'account_id', p_payload->'account_id',
    'environment', p_payload->'environment',
    'release_sha', p_payload->'release_sha',
    'bundle_version', p_payload->'bundle_version',
    'artifact_uri', p_payload->'artifact_uri',
    'policy', p_payload->'policy',
    'cost_schedule', p_payload->'cost_schedule',
    'market_calendar', p_payload->'market_calendar',
    'market_calendar_sessions', p_payload->'market_calendar_sessions',
    'execution_model', p_payload->'execution_model',
    'provider_contract', p_payload->'provider_contract'
  );
$$;

create or replace function private.validate_reference_bundle_draft_v1(
  p_payload jsonb
)
returns void
language plpgsql
immutable
security definer
set search_path = ''
as $$
declare
  policy jsonb := p_payload->'policy';
  cost_schedule jsonb := p_payload->'cost_schedule';
  calendar jsonb := p_payload->'market_calendar';
  sessions jsonb := p_payload->'market_calendar_sessions';
  model jsonb := p_payload->'execution_model';
  contract jsonb := p_payload->'provider_contract';
  session_row jsonb;
begin
  perform private.validate_command_draft_v1(
    'request', 'reference_bundle', p_payload
  );
  perform private.assert_exact_json_keys(policy, array[
    'policy_version', 'policy_sha256', 'price_model', 'fill_model',
    'parameters', 'effective_from', 'effective_until'
  ]);
  perform private.assert_exact_json_keys(policy->'parameters', array[
    'corporate_action_evidence_sha256', 'execution_model_version',
    'market_calendar_sha256', 'market_calendar_version',
    'tick_size_evidence_sha256', 'volume_model_evidence_sha256'
  ]);
  perform private.assert_exact_json_keys(cost_schedule, array[
    'schedule_version', 'schedule_sha256', 'buy_commission_rate',
    'sell_commission_rate', 'sell_tax_rate', 'settlement_days',
    'effective_from', 'effective_until'
  ]);
  perform private.assert_exact_json_keys(calendar, array[
    'calendar_version', 'calendar_sha256', 'timezone_name',
    'valid_from', 'valid_until'
  ]);
  perform private.assert_exact_json_keys(model, array[
    'model_version', 'model_sha256', 'tick_size_evidence_sha256',
    'volume_model_evidence_sha256', 'corporate_action_evidence_sha256',
    'effective_from', 'effective_until'
  ]);
  if jsonb_typeof(sessions) <> 'array'
     or jsonb_array_length(sessions) < 1
     or jsonb_array_length(sessions) > 400 then
    raise exception 'reference_bundle_sessions_invalid' using errcode = '22023';
  end if;
  for session_row in select value from jsonb_array_elements(sessions) loop
    perform private.assert_exact_json_keys(session_row, array[
      'session_date', 'is_open', 'session_sha256'
    ]);
    if jsonb_typeof(session_row->'is_open') <> 'boolean'
       or coalesce(
         session_row->>'session_sha256' ~ '^[0-9a-f]{64}$', false
       ) is false
       or private.reference_component_sha256_v1(
         'market_calendar_session', session_row
       ) <> session_row->>'session_sha256' then
      raise exception 'reference_bundle_session_digest_invalid' using errcode = '22023';
    end if;
    perform (session_row->>'session_date')::date;
  end loop;
  if (
    select count(*) <> count(distinct value->>'session_date')
    from jsonb_array_elements(sessions)
  ) then
    raise exception 'reference_bundle_session_date_duplicate' using errcode = '22023';
  end if;
  if sessions is distinct from (
    select jsonb_agg(value order by value->>'session_date')
    from jsonb_array_elements(sessions)
  ) then
    raise exception 'reference_bundle_sessions_not_sorted' using errcode = '22023';
  end if;
  if coalesce(
       p_payload->>'account_id' ~ '^[a-z0-9][a-z0-9_-]{2,63}$', false
     ) is false
     or p_payload->>'environment' not in ('paper', 'contract_test')
     or coalesce(
       p_payload->>'release_sha' ~ '^[0-9a-f]{40}([0-9a-f]{24})?$', false
     ) is false
     or nullif(btrim(p_payload->>'bundle_version'), '') is null
     or coalesce(p_payload->>'bundle_sha256' ~ '^[0-9a-f]{64}$', false) is false
     or coalesce(
       p_payload->>'artifact_uri' ~ '^https://[^/?#[:space:]]+/', false
     ) is false
     or p_payload->>'artifact_uri' ~ '[?#]'
     or p_payload->>'reason_code' <> 'reference_artifact_registration'
     or policy->>'price_model' <> 'next_executable_minute_v1'
     or policy->>'fill_model' <> 'whole_share_volume_bounded_v1'
     or nullif(btrim(policy->>'policy_version'), '') is null
     or coalesce(policy->>'policy_sha256' ~ '^[0-9a-f]{64}$', false) is false
     or nullif(btrim(cost_schedule->>'schedule_version'), '') is null
     or coalesce(
       cost_schedule->>'schedule_sha256' ~ '^[0-9a-f]{64}$', false
     ) is false
     or nullif(btrim(calendar->>'calendar_version'), '') is null
     or coalesce(calendar->>'calendar_sha256' ~ '^[0-9a-f]{64}$', false) is false
     or nullif(btrim(model->>'model_version'), '') is null
     or coalesce(model->>'model_sha256' ~ '^[0-9a-f]{64}$', false) is false
     or coalesce(
       model->>'tick_size_evidence_sha256' ~ '^[0-9a-f]{64}$', false
     ) is false
     or coalesce(
       model->>'volume_model_evidence_sha256' ~ '^[0-9a-f]{64}$', false
     ) is false
     or coalesce(
       model->>'corporate_action_evidence_sha256' ~ '^[0-9a-f]{64}$', false
     ) is false
     or calendar->>'timezone_name' <> 'Asia/Seoul'
     or jsonb_typeof(cost_schedule->'settlement_days') <> 'number'
     or (cost_schedule->>'settlement_days')::integer not between 0 and 10
     or jsonb_typeof(cost_schedule->'buy_commission_rate') <> 'string'
     or jsonb_typeof(cost_schedule->'sell_commission_rate') <> 'string'
     or jsonb_typeof(cost_schedule->'sell_tax_rate') <> 'string'
     or (cost_schedule->>'buy_commission_rate')::numeric not between 0 and 0.999999999999
     or (cost_schedule->>'sell_commission_rate')::numeric not between 0 and 0.999999999999
     or (cost_schedule->>'sell_tax_rate')::numeric not between 0 and 0.999999999999 then
    raise exception 'reference_bundle_values_invalid' using errcode = '22023';
  end if;
  if private.reference_component_sha256_v1('policy', policy)
       <> policy->>'policy_sha256'
     or private.reference_component_sha256_v1('cost_schedule', cost_schedule)
       <> cost_schedule->>'schedule_sha256'
     or private.reference_component_sha256_v1('market_calendar', calendar)
       <> calendar->>'calendar_sha256'
     or private.reference_component_sha256_v1('execution_model', model)
       <> model->>'model_sha256'
     or private.sha256_jsonb_v1(private.reference_bundle_content_v1(p_payload))
       <> p_payload->>'bundle_sha256' then
    raise exception 'reference_bundle_digest_mismatch' using errcode = '23514';
  end if;
  if policy->'parameters'->>'execution_model_version'
       is distinct from model->>'model_version'
     or policy->'parameters'->>'market_calendar_version'
       is distinct from calendar->>'calendar_version'
     or policy->'parameters'->>'market_calendar_sha256'
       is distinct from calendar->>'calendar_sha256'
     or policy->'parameters'->>'tick_size_evidence_sha256'
       is distinct from model->>'tick_size_evidence_sha256'
     or policy->'parameters'->>'volume_model_evidence_sha256'
       is distinct from model->>'volume_model_evidence_sha256'
     or policy->'parameters'->>'corporate_action_evidence_sha256'
       is distinct from model->>'corporate_action_evidence_sha256' then
    raise exception 'reference_bundle_cross_pin_mismatch' using errcode = '23514';
  end if;
  if (policy->>'effective_until')::timestamptz
       <= (policy->>'effective_from')::timestamptz
     or (cost_schedule->>'effective_until')::timestamptz
       <= (cost_schedule->>'effective_from')::timestamptz
     or (calendar->>'valid_until')::date < (calendar->>'valid_from')::date
     or (model->>'effective_until')::timestamptz
       <= (model->>'effective_from')::timestamptz then
    raise exception 'reference_bundle_window_invalid' using errcode = '22023';
  end if;
  if exists (
    select 1 from jsonb_array_elements(sessions) as session(value)
    where (session.value->>'session_date')::date
      not between (calendar->>'valid_from')::date
              and (calendar->>'valid_until')::date
  ) then
    raise exception 'reference_bundle_session_outside_calendar'
      using errcode = '23514';
  end if;
  if p_payload->>'environment' = 'paper' then
    if jsonb_typeof(contract) <> 'null' then
      raise exception 'paper_provider_contract_forbidden' using errcode = '23514';
    end if;
  else
    perform private.assert_exact_json_keys(contract, array[
      'provider', 'qualification_environment', 'execution_transport',
      'contract_version', 'contract_sha256', 'openapi_sha256',
      'official_artifact_uri', 'retrieved_at', 'release_sha',
      'effective_from', 'effective_until'
    ]);
    if contract->>'provider' <> 'toss'
       or contract->>'qualification_environment' <> 'contract_test'
       or contract->>'execution_transport' <> 'local_contract_simulator'
       or contract->>'release_sha' is distinct from p_payload->>'release_sha'
       or nullif(btrim(contract->>'contract_version'), '') is null
       or coalesce(contract->>'contract_sha256' ~ '^[0-9a-f]{64}$', false) is false
       or coalesce(contract->>'openapi_sha256' ~ '^[0-9a-f]{64}$', false) is false
       or coalesce(
         contract->>'official_artifact_uri' ~ '^https://[^/?#[:space:]]+/', false
       ) is false
       or contract->>'official_artifact_uri' ~ '[?#]'
       or (contract->>'effective_until')::timestamptz
         <= (contract->>'effective_from')::timestamptz
       or private.reference_component_sha256_v1('provider_contract', contract)
         <> contract->>'contract_sha256' then
      raise exception 'provider_contract_component_invalid' using errcode = '23514';
    end if;
  end if;
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'reference_bundle_values_invalid' using errcode = '22023';
end;
$$;

create or replace function private.control_approval_receipt_v1(p_request_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  request_row private.control_approval_requests%rowtype;
  review_row private.control_approval_reviews%rowtype;
  state_value text;
begin
  select * into request_row
  from private.control_approval_requests
  where id = p_request_id;
  if not found then
    raise exception 'control_approval_request_not_found' using errcode = 'P0002';
  end if;
  select * into review_row
  from private.control_approval_reviews
  where request_id = p_request_id;
  state_value := case
    when review_row.id is not null and review_row.decision = 'approved' then 'approved'
    when review_row.id is not null then 'rejected'
    when request_row.expires_at <= clock_timestamp() then 'expired'
    else 'requested'
  end;
  return jsonb_build_object(
    'schema_version', 1,
    'request_id', request_row.id,
    'request_kind', request_row.request_kind,
    'account_id', request_row.account_id,
    'environment', request_row.environment,
    'state', state_value,
    'request_payload_sha256', request_row.payload_sha256,
    'requester_user_id', request_row.requester_user_id,
    'requested_at', request_row.requested_at,
    'expires_at', request_row.expires_at,
    'review_id', review_row.id,
    'reviewer_user_id', review_row.reviewer_user_id,
    'reviewed_at', review_row.reviewed_at,
    'reference_bundle_materialized', exists (
      select 1 from private.reference_bundle_materializations
      where request_id = request_row.id
    ),
    'qualification_id', (
      select qualification_id from private.qualification_finalizations
      where request_id = request_row.id
    )
  );
end;
$$;

create or replace function private.request_reference_bundle_v1_impl(
  p_request_payload jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid;
  request_id_value uuid;
  draft_payload jsonb;
  payload_digest text;
  requested_time timestamptz;
  expiry_time timestamptz;
  existing_row private.control_approval_requests%rowtype;
begin
  perform private.assert_exact_json_keys(p_request_payload, array[
    'schema_version', 'request_id', 'command_type', 'idempotency_key',
    'account_id', 'environment', 'release_sha', 'bundle_version',
    'bundle_sha256', 'artifact_uri', 'policy', 'cost_schedule',
    'market_calendar', 'market_calendar_sessions', 'execution_model',
    'provider_contract', 'requested_at', 'expires_at', 'reason_code',
    'step_up_grant_id', 'command_hash', 'step_up_grant_issued_at',
    'step_up_grant_expires_at', 'step_up_grant_one_time',
    'step_up_grant_consumed_at', 'bound_action', 'bound_command_type'
  ]);
  if jsonb_typeof(p_request_payload->'step_up_grant_one_time') <> 'boolean'
     or jsonb_typeof(p_request_payload->'step_up_grant_consumed_at') <> 'null' then
    raise exception 'reference_bundle_request_json_type_invalid' using errcode = '22023';
  end if;
  draft_payload := private.command_draft_v1(p_request_payload);
  perform private.validate_reference_bundle_draft_v1(draft_payload);
  request_id_value := (p_request_payload->>'request_id')::uuid;
  requested_time := (p_request_payload->>'requested_at')::timestamptz;
  expiry_time := (p_request_payload->>'expires_at')::timestamptz;
  if requested_time < clock_timestamp() - interval '5 minutes'
     or requested_time > clock_timestamp() + interval '30 seconds'
     or expiry_time <= requested_time
     or expiry_time > requested_time + interval '24 hours'
     or p_request_payload->>'idempotency_key'
       !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$' then
    raise exception 'reference_bundle_request_values_invalid' using errcode = '22023';
  end if;
  actor := private.require_human_roles(array['release_manager'], true);
  perform private.require_recent_aal2();
  if private.has_active_role(actor, 'platform_admin') then
    raise exception 'platform_admin_trading_action_forbidden' using errcode = '42501';
  end if;
  if not exists (
    select 1 from private.trading_accounts
    where account_id = p_request_payload->>'account_id'
      and environment = p_request_payload->>'environment'
      and state = 'open'
  ) then
    raise exception 'reference_bundle_account_not_open' using errcode = '23514';
  end if;
  payload_digest := private.sha256_jsonb_v1(draft_payload);
  select * into existing_row
  from private.control_approval_requests
  where request_kind = 'reference_bundle'
    and idempotency_key = p_request_payload->>'idempotency_key';
  if found then
    if existing_row.id <> request_id_value
       or existing_row.payload_sha256 <> payload_digest
       or existing_row.payload <> draft_payload
       or existing_row.requester_user_id <> actor then
      raise exception 'control_approval_idempotency_conflict' using errcode = '23505';
    end if;
    return private.control_approval_receipt_v1(existing_row.id);
  end if;
  if p_request_payload->>'command_hash' is distinct from
       private.compute_command_sha256('operation_v1:request', draft_payload) then
    raise exception 'reference_bundle_request_hash_mismatch' using errcode = '42501';
  end if;
  perform private.consume_bound_step_up_v1(
    p_request_payload, 'request', 'reference_bundle', requested_time,
    'request_reference_bundle_v1'
  );
  insert into private.control_approval_requests (
    id, request_kind, account_id, environment, payload, payload_sha256,
    requester_user_id, requested_at, expires_at, idempotency_key
  ) values (
    request_id_value, 'reference_bundle', p_request_payload->>'account_id',
    p_request_payload->>'environment', draft_payload, payload_digest,
    actor, requested_time, expiry_time, p_request_payload->>'idempotency_key'
  );
  perform private.write_audit_event(
    'human', actor, 'release_manager', null, null,
    p_request_payload->>'release_sha', 'reference_bundle_requested',
    'control_approval_request', request_id_value::text, request_id_value,
    null, p_request_payload->>'reason_code', null,
    array['payload_sha256'], null, payload_digest, null
  );
  return private.control_approval_receipt_v1(request_id_value);
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'reference_bundle_request_values_invalid' using errcode = '22023';
end;
$$;

create or replace function private.materialize_reference_bundle_v1(
  p_request_id uuid,
  p_review_id uuid
)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
  request_row private.control_approval_requests%rowtype;
  review_row private.control_approval_reviews%rowtype;
  payload jsonb;
  evidence_id_value uuid := gen_random_uuid();
  policy_id_value uuid;
  cost_id_value uuid;
  calendar_id_value uuid;
  model_id_value uuid;
  provider_id_value uuid;
begin
  select * into request_row
  from private.control_approval_requests
  where id = p_request_id and request_kind = 'reference_bundle'
  for update;
  select * into review_row
  from private.control_approval_reviews
  where id = p_review_id and request_id = p_request_id
    and decision = 'approved';
  if request_row.id is null or review_row.id is null then
    raise exception 'approved_reference_bundle_review_required' using errcode = '23514';
  end if;
  if exists (
    select 1 from private.reference_bundle_materializations
    where request_id = p_request_id
  ) then
    select evidence_id into evidence_id_value
    from private.reference_bundle_materializations
    where request_id = p_request_id;
    return evidence_id_value;
  end if;
  payload := request_row.payload;
  perform private.validate_reference_bundle_draft_v1(payload);
  insert into private.control_evidence (
    id, evidence_type, environment, artifact_uri, artifact_sha256,
    captured_at, verified_at, verified_by, metadata_summary
  ) values (
    evidence_id_value, 'reference_bundle', request_row.environment,
    payload->>'artifact_uri', payload->>'bundle_sha256',
    request_row.requested_at, review_row.reviewed_at,
    review_row.reviewer_user_id,
    jsonb_build_object(
      'schema_version', 1,
      'bundle_version', payload->>'bundle_version',
      'release_sha', payload->>'release_sha',
      'policy_sha256', payload->'policy'->>'policy_sha256',
      'schedule_sha256', payload->'cost_schedule'->>'schedule_sha256',
      'calendar_sha256', payload->'market_calendar'->>'calendar_sha256',
      'model_sha256', payload->'execution_model'->>'model_sha256',
      'provider_contract_sha256', payload->'provider_contract'->>'contract_sha256'
    )
  );
  select id into calendar_id_value
  from private.market_calendars
  where environment = request_row.environment
    and calendar_version = payload->'market_calendar'->>'calendar_version'
    and calendar_sha256 = payload->'market_calendar'->>'calendar_sha256'
    and timezone_name = payload->'market_calendar'->>'timezone_name'
    and valid_from = (payload->'market_calendar'->>'valid_from')::date
    and valid_until = (payload->'market_calendar'->>'valid_until')::date
    and status = 'approved';
  if calendar_id_value is null then
    calendar_id_value := gen_random_uuid();
    insert into private.market_calendars (
      id, environment, calendar_version, calendar_sha256, timezone_name,
      valid_from, valid_until, status, evidence_id, requested_by, reviewed_by
    ) values (
      calendar_id_value, request_row.environment,
      payload->'market_calendar'->>'calendar_version',
      payload->'market_calendar'->>'calendar_sha256',
      payload->'market_calendar'->>'timezone_name',
      (payload->'market_calendar'->>'valid_from')::date,
      (payload->'market_calendar'->>'valid_until')::date,
      'approved', evidence_id_value, request_row.requester_user_id,
      review_row.reviewer_user_id
    );
    insert into private.market_calendar_sessions (
      calendar_id, session_date, is_open, session_sha256
    )
    select calendar_id_value, (value->>'session_date')::date,
           (value->>'is_open')::boolean, value->>'session_sha256'
    from jsonb_array_elements(payload->'market_calendar_sessions');
  elsif payload->'market_calendar_sessions' is distinct from (
    select jsonb_agg(jsonb_build_object(
      'session_date', session.session_date::text,
      'is_open', session.is_open,
      'session_sha256', session.session_sha256
    ) order by session.session_date)
    from private.market_calendar_sessions as session
    where session.calendar_id = calendar_id_value
  ) then
    raise exception 'reference_bundle_existing_calendar_sessions_mismatch'
      using errcode = '23514';
  end if;
  select id into model_id_value
  from private.paper_execution_model_registry
  where environment = request_row.environment
    and model_version = payload->'execution_model'->>'model_version'
    and tick_size_evidence_sha256 =
      payload->'execution_model'->>'tick_size_evidence_sha256'
    and volume_model_evidence_sha256 =
      payload->'execution_model'->>'volume_model_evidence_sha256'
    and corporate_action_evidence_sha256 =
      payload->'execution_model'->>'corporate_action_evidence_sha256'
    and market_calendar_id = calendar_id_value
    and effective_from = (payload->'execution_model'->>'effective_from')::timestamptz
    and effective_until = (payload->'execution_model'->>'effective_until')::timestamptz
    and status = 'approved';
  if model_id_value is null then
    model_id_value := gen_random_uuid();
    insert into private.paper_execution_model_registry (
      id, environment, model_version, tick_size_evidence_sha256,
      volume_model_evidence_sha256, corporate_action_evidence_sha256,
      market_calendar_id, status, evidence_id, requested_by, reviewed_by,
      effective_from, effective_until
    ) values (
      model_id_value, request_row.environment,
      payload->'execution_model'->>'model_version',
      payload->'execution_model'->>'tick_size_evidence_sha256',
      payload->'execution_model'->>'volume_model_evidence_sha256',
      payload->'execution_model'->>'corporate_action_evidence_sha256',
      calendar_id_value, 'approved', evidence_id_value,
      request_row.requester_user_id, review_row.reviewer_user_id,
      (payload->'execution_model'->>'effective_from')::timestamptz,
      (payload->'execution_model'->>'effective_until')::timestamptz
    );
  end if;
  select id into policy_id_value
  from private.paper_execution_policies
  where account_id = request_row.account_id
    and policy_version = payload->'policy'->>'policy_version'
    and policy_sha256 = payload->'policy'->>'policy_sha256'
    and price_model = payload->'policy'->>'price_model'
    and fill_model = payload->'policy'->>'fill_model'
    and parameters = payload->'policy'->'parameters'
    and effective_from = (payload->'policy'->>'effective_from')::timestamptz
    and effective_until = (payload->'policy'->>'effective_until')::timestamptz
    and status = 'approved';
  if policy_id_value is null then
    policy_id_value := gen_random_uuid();
    insert into private.paper_execution_policies (
      id, account_id, policy_version, policy_sha256, status, price_model,
      fill_model, parameters, evidence_id, requested_by, reviewed_by,
      effective_from, effective_until
    ) values (
      policy_id_value, request_row.account_id,
      payload->'policy'->>'policy_version',
      payload->'policy'->>'policy_sha256', 'approved',
      payload->'policy'->>'price_model', payload->'policy'->>'fill_model',
      payload->'policy'->'parameters', evidence_id_value,
      request_row.requester_user_id, review_row.reviewer_user_id,
      (payload->'policy'->>'effective_from')::timestamptz,
      (payload->'policy'->>'effective_until')::timestamptz
    );
  end if;
  select id into cost_id_value
  from private.execution_cost_schedules
  where account_id = request_row.account_id
    and schedule_version = payload->'cost_schedule'->>'schedule_version'
    and schedule_sha256 = payload->'cost_schedule'->>'schedule_sha256'
    and buy_commission_rate =
      (payload->'cost_schedule'->>'buy_commission_rate')::numeric
    and sell_commission_rate =
      (payload->'cost_schedule'->>'sell_commission_rate')::numeric
    and sell_tax_rate = (payload->'cost_schedule'->>'sell_tax_rate')::numeric
    and settlement_days = (payload->'cost_schedule'->>'settlement_days')::integer
    and effective_from = (payload->'cost_schedule'->>'effective_from')::timestamptz
    and effective_until = (payload->'cost_schedule'->>'effective_until')::timestamptz
    and status = 'approved';
  if cost_id_value is null then
    cost_id_value := gen_random_uuid();
    insert into private.execution_cost_schedules (
      id, account_id, schedule_version, schedule_sha256,
      buy_commission_rate, sell_commission_rate, sell_tax_rate,
      settlement_days, status, evidence_id, requested_by, reviewed_by,
      effective_from, effective_until
    ) values (
      cost_id_value, request_row.account_id,
      payload->'cost_schedule'->>'schedule_version',
      payload->'cost_schedule'->>'schedule_sha256',
      (payload->'cost_schedule'->>'buy_commission_rate')::numeric,
      (payload->'cost_schedule'->>'sell_commission_rate')::numeric,
      (payload->'cost_schedule'->>'sell_tax_rate')::numeric,
      (payload->'cost_schedule'->>'settlement_days')::integer,
      'approved', evidence_id_value, request_row.requester_user_id,
      review_row.reviewer_user_id,
      (payload->'cost_schedule'->>'effective_from')::timestamptz,
      (payload->'cost_schedule'->>'effective_until')::timestamptz
    );
  end if;
  if request_row.environment = 'contract_test' then
    select id into provider_id_value
    from private.provider_contract_registry
    where provider = payload->'provider_contract'->>'provider'
      and qualification_environment =
        payload->'provider_contract'->>'qualification_environment'
      and contract_version = payload->'provider_contract'->>'contract_version'
      and openapi_sha256 = payload->'provider_contract'->>'openapi_sha256'
      and execution_transport = payload->'provider_contract'->>'execution_transport'
      and official_artifact_uri = payload->'provider_contract'->>'official_artifact_uri'
      and retrieved_at = (payload->'provider_contract'->>'retrieved_at')::timestamptz
      and release_sha = payload->'provider_contract'->>'release_sha'
      and effective_from = (payload->'provider_contract'->>'effective_from')::timestamptz
      and effective_until = (payload->'provider_contract'->>'effective_until')::timestamptz
      and status = 'approved';
    if provider_id_value is null then
      provider_id_value := gen_random_uuid();
      insert into private.provider_contract_registry (
        id, provider, qualification_environment, execution_transport,
        contract_version, openapi_sha256, official_artifact_uri, retrieved_at,
        evidence_id, release_sha, status, requested_by, reviewed_by,
        effective_from, effective_until
      ) values (
        provider_id_value, payload->'provider_contract'->>'provider',
        payload->'provider_contract'->>'qualification_environment',
        payload->'provider_contract'->>'execution_transport',
        payload->'provider_contract'->>'contract_version',
        payload->'provider_contract'->>'openapi_sha256',
        payload->'provider_contract'->>'official_artifact_uri',
        (payload->'provider_contract'->>'retrieved_at')::timestamptz,
        evidence_id_value, payload->'provider_contract'->>'release_sha',
        'approved', request_row.requester_user_id, review_row.reviewer_user_id,
        (payload->'provider_contract'->>'effective_from')::timestamptz,
        (payload->'provider_contract'->>'effective_until')::timestamptz
      );
    end if;
  end if;
  insert into private.reference_bundle_materializations (
    request_id, review_id, evidence_id, policy_id, cost_schedule_id,
    calendar_id, execution_model_id, provider_contract_id,
    execution_model_sha256, provider_contract_sha256, bundle_sha256,
    materialized_at
  ) values (
    request_row.id, review_row.id, evidence_id_value, policy_id_value,
    cost_id_value, calendar_id_value, model_id_value, provider_id_value,
    payload->'execution_model'->>'model_sha256',
    payload->'provider_contract'->>'contract_sha256',
    payload->>'bundle_sha256', review_row.reviewed_at
  );
  return evidence_id_value;
end;
$$;

create or replace function private.review_reference_bundle_v1_impl(
  p_review_payload jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid;
  review_id_value uuid;
  request_id_value uuid;
  reviewed_time timestamptz;
  draft_payload jsonb;
  review_digest text;
  request_row private.control_approval_requests%rowtype;
  existing_review private.control_approval_reviews%rowtype;
  evidence_id_value uuid;
begin
  perform private.assert_exact_json_keys(p_review_payload, array[
    'schema_version', 'review_id', 'request_id', 'command_type',
    'reviewer_role', 'decision', 'reason_code',
    'expected_request_payload_sha256', 'reviewed_at',
    'step_up_grant_id', 'command_hash', 'step_up_grant_issued_at',
    'step_up_grant_expires_at', 'step_up_grant_one_time',
    'step_up_grant_consumed_at', 'bound_action', 'bound_command_type'
  ]);
  draft_payload := private.command_draft_v1(p_review_payload);
  perform private.validate_command_draft_v1(
    'review', 'reference_bundle', draft_payload
  );
  if p_review_payload->>'reviewer_role' <> 'release_manager'
     or p_review_payload->>'decision' not in ('approve', 'reject')
     or (
       p_review_payload->>'decision' = 'approve'
       and p_review_payload->>'reason_code' <> 'artifact_verified'
     )
     or (
       p_review_payload->>'decision' = 'reject'
       and p_review_payload->>'reason_code' not in (
         'artifact_mismatch', 'evidence_incomplete', 'superseded'
       )
     ) then
    raise exception 'reference_bundle_review_values_invalid' using errcode = '22023';
  end if;
  review_id_value := (p_review_payload->>'review_id')::uuid;
  request_id_value := (p_review_payload->>'request_id')::uuid;
  reviewed_time := (p_review_payload->>'reviewed_at')::timestamptz;
  if reviewed_time < clock_timestamp() - interval '5 minutes'
     or reviewed_time > clock_timestamp() + interval '30 seconds' then
    raise exception 'reference_bundle_review_time_invalid' using errcode = '22023';
  end if;
  actor := private.require_human_roles(array['release_manager'], true);
  perform private.require_recent_aal2();
  if private.has_active_role(actor, 'platform_admin') then
    raise exception 'platform_admin_trading_action_forbidden' using errcode = '42501';
  end if;
  select * into request_row
  from private.control_approval_requests
  where id = request_id_value and request_kind = 'reference_bundle'
  for update;
  if not found then
    raise exception 'reference_bundle_request_not_found' using errcode = 'P0002';
  end if;
  if request_row.expires_at <= reviewed_time
     or request_row.payload_sha256
       is distinct from p_review_payload->>'expected_request_payload_sha256' then
    raise exception 'reference_bundle_review_stale' using errcode = '40001';
  end if;
  review_digest := private.sha256_jsonb_v1(draft_payload);
  select * into existing_review
  from private.control_approval_reviews
  where request_id = request_id_value;
  if found then
    if existing_review.id <> review_id_value
       or existing_review.payload_sha256 <> review_digest
       or existing_review.payload <> draft_payload
       or existing_review.reviewer_user_id <> actor then
      raise exception 'control_approval_review_conflict' using errcode = '23505';
    end if;
    return private.control_approval_receipt_v1(request_id_value);
  end if;
  if p_review_payload->>'command_hash' is distinct from
       private.compute_command_sha256('operation_v1:review', draft_payload) then
    raise exception 'reference_bundle_review_hash_mismatch' using errcode = '42501';
  end if;
  perform private.consume_bound_step_up_v1(
    p_review_payload, 'review', 'reference_bundle', reviewed_time,
    'review_reference_bundle_v1'
  );
  insert into private.control_approval_reviews (
    id, request_id, decision, payload, payload_sha256,
    expected_request_payload_sha256, reviewer_user_id, reviewed_at
  ) values (
    review_id_value, request_id_value,
    case when p_review_payload->>'decision' = 'approve'
      then 'approved' else 'rejected' end,
    draft_payload, review_digest,
    p_review_payload->>'expected_request_payload_sha256', actor, reviewed_time
  );
  if p_review_payload->>'decision' = 'approve' then
    evidence_id_value := private.materialize_reference_bundle_v1(
      request_id_value, review_id_value
    );
  end if;
  perform private.write_audit_event(
    'human', actor, 'release_manager', null, null,
    request_row.payload->>'release_sha',
    'reference_bundle_' || case
      when p_review_payload->>'decision' = 'approve' then 'approved'
      else 'rejected' end,
    'control_approval_request', request_id_value::text, review_id_value,
    null, p_review_payload->>'reason_code', null,
    array['decision'], request_row.payload_sha256, review_digest,
    evidence_id_value
  );
  return private.control_approval_receipt_v1(request_id_value);
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'reference_bundle_review_values_invalid' using errcode = '22023';
end;
$$;

create or replace function private.capture_qualification_snapshot_v1_impl(
  p_account_id text,
  p_environment text,
  p_release_sha text,
  p_observed_at timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  account_row private.trading_accounts%rowtype;
  cash_row private.cash_balance_projection%rowtype;
  snapshot_row private.account_snapshots%rowtype;
  next_sequence bigint;
  checkpoint_sha text;
  position_sha text;
begin
  perform private.require_service_role();
  if p_environment not in ('paper', 'contract_test')
     or p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_observed_at < clock_timestamp() - interval '5 minutes'
     or p_observed_at > clock_timestamp() + interval '30 seconds' then
    raise exception 'qualification_snapshot_values_invalid' using errcode = '22023';
  end if;
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended('qualification-snapshot:' || p_account_id, 0)
  );
  select * into account_row
  from private.trading_accounts
  where account_id = p_account_id
  for update;
  if not found or account_row.environment <> p_environment
     or account_row.state <> 'open' then
    raise exception 'qualification_snapshot_account_not_open' using errcode = '23514';
  end if;
  if exists (
    select 1 from private.execution_controls
    where account_id = p_account_id and execution_enabled is true
  ) then
    raise exception 'qualification_snapshot_requires_disabled_execution' using errcode = '23514';
  end if;
  if exists (
    select 1
    from private.execution_reconciliation_state as state
    join private.order_intents as intent on intent.id = state.intent_id
    where intent.account_id = p_account_id
      and intent.environment = p_environment
      and state.state <> 'complete'
  ) then
    raise exception 'qualification_snapshot_nonterminal_intent_present' using errcode = '23514';
  end if;
  if exists (
    select 1
    from private.reconciliation_breaks as break_row
    join private.reconciliation_runs as run_row on run_row.id = break_row.run_id
    where run_row.account_id = p_account_id
      and run_row.environment = p_environment
      and break_row.state <> 'resolved'
  ) then
    raise exception 'qualification_snapshot_unresolved_break_present' using errcode = '23514';
  end if;
  if exists (
    select 1
    from private.order_reservations as reservation
    join lateral (
      select event.remaining_cash_krw, event.remaining_quantity
      from private.reservation_events as event
      where event.reservation_id = reservation.id
      order by event.event_sequence desc
      limit 1
    ) as latest on true
    where reservation.account_id = p_account_id
      and reservation.environment = p_environment
      and (latest.remaining_cash_krw > 0 or latest.remaining_quantity > 0)
  ) then
    raise exception 'qualification_snapshot_active_reservation_present' using errcode = '23514';
  end if;
  lock table private.accounting_transactions in share mode;
  lock table private.accounting_postings in share mode;
  lock table private.cash_balance_projection in share mode;
  lock table private.position_projection in share mode;
  select * into cash_row
  from private.cash_balance_projection
  where account_id = p_account_id;
  if not found or cash_row.last_journal_entry_id is null then
    raise exception 'qualification_snapshot_opening_journal_required' using errcode = '23514';
  end if;
  checkpoint_sha := private.compute_ledger_checkpoint_sha256_v1(
    p_account_id, p_environment
  );
  position_sha := private.compute_position_projection_sha256_v1(p_account_id);
  select * into snapshot_row
  from private.account_snapshots
  where account_id = p_account_id
    and environment = p_environment
    and checkpoint_schema_version = 1
    and ledger_checkpoint_sha256 = checkpoint_sha
  order by sequence desc
  limit 1;
  if found then
    return jsonb_build_object(
      'schema_version', 1,
      'account_snapshot_id', snapshot_row.id,
      'account_snapshot_sequence', snapshot_row.sequence,
      'ledger_checkpoint_sha256', snapshot_row.ledger_checkpoint_sha256,
      'inserted', false
    );
  end if;
  select coalesce(max(sequence), 0) + 1 into next_sequence
  from private.account_snapshots
  where account_id = p_account_id and environment = p_environment;
  insert into private.account_snapshots (
    account_id, environment, sequence, cash_krw, reserved_cash_krw,
    positions_sha256, source_type, source_id, observed_at,
    checkpoint_schema_version, ledger_checkpoint_sha256
  ) values (
    p_account_id, p_environment, next_sequence, cash_row.settled_cash_krw,
    cash_row.reserved_cash_krw, position_sha, 'ledger_projection',
    cash_row.last_journal_entry_id, p_observed_at, 1, checkpoint_sha
  ) returning * into snapshot_row;
  return jsonb_build_object(
    'schema_version', 1,
    'account_snapshot_id', snapshot_row.id,
    'account_snapshot_sequence', snapshot_row.sequence,
    'ledger_checkpoint_sha256', snapshot_row.ledger_checkpoint_sha256,
    'inserted', true
  );
end;
$$;

create or replace function private.qualification_run_content_v1(p_payload jsonb)
returns jsonb
language sql
immutable
security definer
set search_path = ''
as $$ select p_payload - 'evidence_sha256'; $$;

create or replace function private.validate_qualification_run_v1(
  p_payload jsonb
)
returns void
language plpgsql
immutable
security definer
set search_path = ''
as $$
declare
  manifest jsonb := p_payload->'evidence_manifest';
  check_row jsonb;
  required_checks text[];
  actual_checks text[];
  all_pass boolean;
begin
  perform private.assert_exact_json_keys(p_payload, array[
    'schema_version', 'run_id', 'run_kind', 'account_id', 'environment',
    'reference_bundle_request_id', 'account_snapshot_id',
    'account_snapshot_sequence', 'ledger_checkpoint_sha256', 'release_sha',
    'suite_version', 'started_at', 'completed_at', 'result',
    'evidence_manifest', 'evidence_sha256', 'worker_id'
  ]);
  perform private.assert_exact_json_keys(manifest, array[
    'schema_version', 'checks'
  ]);
  if jsonb_typeof(p_payload->'schema_version') <> 'number'
     or jsonb_typeof(p_payload->'account_snapshot_sequence') <> 'number'
     or coalesce((p_payload->>'schema_version')::integer, -1) <> 1
     or jsonb_typeof(manifest->'schema_version') <> 'number'
     or coalesce((manifest->>'schema_version')::integer, -1) <> 1
     or jsonb_typeof(manifest->'checks') <> 'array'
     or p_payload->>'run_kind' not in ('g1', 'g2', 'contract_qualification')
     or p_payload->>'environment' not in ('paper', 'contract_test')
     or p_payload->>'result' not in ('pass', 'fail')
     or p_payload->>'ledger_checkpoint_sha256' !~ '^[0-9a-f]{64}$'
     or p_payload->>'release_sha' !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_payload->>'evidence_sha256' !~ '^[0-9a-f]{64}$'
     or nullif(btrim(p_payload->>'suite_version'), '') is null then
    raise exception 'qualification_run_values_invalid' using errcode = '22023';
  end if;
  required_checks := case p_payload->>'run_kind'
    when 'g1' then array[
      'ledger_balance', 'no_fictitious_sell', 'observation_idempotency',
      'restart_recovery', 'semantic_dedupe'
    ]::text[]
    when 'g2' then array[
      'command_ack', 'fencing', 'maker_checker', 'outbox_delivery',
      'restore_drill'
    ]::text[]
    else array[
      'cancel_lifecycle', 'create_lifecycle', 'fault_injection',
      'production_order_network_zero', 'status_partial_terminal'
    ]::text[]
  end;
  for check_row in select value from jsonb_array_elements(manifest->'checks') loop
    perform private.assert_exact_json_keys(check_row, array[
      'check_id', 'status', 'evidence_sha256', 'metrics'
    ]);
    if check_row->>'status' not in ('pass', 'fail')
       or check_row->>'evidence_sha256' !~ '^[0-9a-f]{64}$'
       or jsonb_typeof(check_row->'metrics') <> 'object' then
      raise exception 'qualification_run_check_invalid' using errcode = '22023';
    end if;
    if check_row->>'check_id' = 'production_order_network_zero' then
      perform private.assert_exact_json_keys(check_row->'metrics', array[
        'request_count'
      ]);
      if jsonb_typeof(check_row->'metrics'->'request_count') <> 'number'
         or (check_row->'metrics'->>'request_count')::integer <> 0 then
        raise exception 'production_order_network_zero_not_proven' using errcode = '23514';
      end if;
    end if;
  end loop;
  select array_agg(value->>'check_id' order by value->>'check_id'),
         bool_and(value->>'status' = 'pass')
  into actual_checks, all_pass
  from jsonb_array_elements(manifest->'checks');
  if actual_checks is distinct from required_checks
     or (p_payload->>'result' = 'pass') is distinct from all_pass then
    raise exception 'qualification_run_check_set_invalid' using errcode = '23514';
  end if;
  if p_payload->>'run_kind' = 'contract_qualification'
     and p_payload->>'environment' <> 'contract_test' then
    raise exception 'contract_qualification_environment_invalid' using errcode = '23514';
  end if;
  if private.sha256_jsonb_v1(private.qualification_run_content_v1(p_payload))
       <> p_payload->>'evidence_sha256' then
    raise exception 'qualification_run_evidence_digest_mismatch' using errcode = '23514';
  end if;
  perform (p_payload->>'run_id')::uuid;
  perform (p_payload->>'reference_bundle_request_id')::uuid;
  perform (p_payload->>'account_snapshot_id')::uuid;
  perform (p_payload->>'worker_id')::uuid;
  if (p_payload->>'account_snapshot_sequence')::bigint <= 0
     or (p_payload->>'completed_at')::timestamptz
       < (p_payload->>'started_at')::timestamptz then
    raise exception 'qualification_run_window_invalid' using errcode = '22023';
  end if;
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'qualification_run_values_invalid' using errcode = '22023';
end;
$$;

create or replace function private.register_qualification_run_v1_impl(
  p_run_payload jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  run_id_value uuid;
  existing_row private.qualification_runs%rowtype;
  snapshot_row private.account_snapshots%rowtype;
  bundle_row private.reference_bundle_materializations%rowtype;
  request_row private.control_approval_requests%rowtype;
begin
  perform private.require_service_role();
  perform private.validate_qualification_run_v1(p_run_payload);
  run_id_value := (p_run_payload->>'run_id')::uuid;
  select * into existing_row
  from private.qualification_runs where id = run_id_value;
  if found then
    if existing_row.evidence_sha256 <> p_run_payload->>'evidence_sha256'
       or existing_row.evidence_manifest <> p_run_payload->'evidence_manifest' then
      raise exception 'qualification_run_idempotency_conflict' using errcode = '23505';
    end if;
    return jsonb_build_object(
      'schema_version', 1, 'run_id', existing_row.id,
      'run_kind', existing_row.run_kind, 'result', existing_row.result,
      'evidence_sha256', existing_row.evidence_sha256, 'inserted', false
    );
  end if;
  if not exists (
    select 1
    from private.worker_leases as lease
    where lease.account_id = p_run_payload->>'account_id'
      and lease.holder_id = p_run_payload->>'worker_id'
      and lease.release_sha = p_run_payload->>'release_sha'
      and lease.expires_at > clock_timestamp()
  ) then
    raise exception 'qualification_run_worker_lease_missing_or_stale'
      using errcode = '40001';
  end if;
  select * into bundle_row
  from private.reference_bundle_materializations
  where request_id = (p_run_payload->>'reference_bundle_request_id')::uuid;
  select * into request_row
  from private.control_approval_requests
  where id = (p_run_payload->>'reference_bundle_request_id')::uuid;
  if bundle_row.request_id is null or request_row.id is null
     or request_row.account_id <> p_run_payload->>'account_id'
     or request_row.environment <> p_run_payload->>'environment'
     or request_row.payload->>'release_sha' <> p_run_payload->>'release_sha' then
    raise exception 'qualification_run_reference_bundle_mismatch' using errcode = '23514';
  end if;
  select * into snapshot_row
  from private.account_snapshots
  where id = (p_run_payload->>'account_snapshot_id')::uuid;
  if not found
     or snapshot_row.account_id <> p_run_payload->>'account_id'
     or snapshot_row.environment <> p_run_payload->>'environment'
     or snapshot_row.checkpoint_schema_version <> 1
     or snapshot_row.sequence <> (p_run_payload->>'account_snapshot_sequence')::bigint
     or snapshot_row.ledger_checkpoint_sha256
       <> p_run_payload->>'ledger_checkpoint_sha256' then
    raise exception 'qualification_run_snapshot_mismatch' using errcode = '23514';
  end if;
  if exists (
    select 1 from private.execution_controls
    where account_id = p_run_payload->>'account_id'
      and execution_enabled is true
  ) then
    raise exception 'qualification_run_requires_disabled_execution' using errcode = '23514';
  end if;
  if (p_run_payload->>'completed_at')::timestamptz
       > clock_timestamp() + interval '30 seconds' then
    raise exception 'qualification_run_future_completion_invalid' using errcode = '22023';
  end if;
  insert into private.qualification_runs (
    id, run_kind, account_id, environment, reference_bundle_request_id,
    account_snapshot_id, account_snapshot_sequence,
    ledger_checkpoint_sha256, release_sha, suite_version, result,
    evidence_manifest, evidence_sha256, started_at, completed_at, worker_id
  ) values (
    run_id_value, p_run_payload->>'run_kind', p_run_payload->>'account_id',
    p_run_payload->>'environment',
    (p_run_payload->>'reference_bundle_request_id')::uuid,
    (p_run_payload->>'account_snapshot_id')::uuid,
    (p_run_payload->>'account_snapshot_sequence')::bigint,
    p_run_payload->>'ledger_checkpoint_sha256', p_run_payload->>'release_sha',
    p_run_payload->>'suite_version', p_run_payload->>'result',
    p_run_payload->'evidence_manifest', p_run_payload->>'evidence_sha256',
    (p_run_payload->>'started_at')::timestamptz,
    (p_run_payload->>'completed_at')::timestamptz,
    (p_run_payload->>'worker_id')::uuid
  );
  return jsonb_build_object(
    'schema_version', 1, 'run_id', run_id_value,
    'run_kind', p_run_payload->>'run_kind',
    'result', p_run_payload->>'result',
    'evidence_sha256', p_run_payload->>'evidence_sha256',
    'inserted', true
  );
end;
$$;

create or replace function private.validate_qualification_finalization_draft_v1(
  p_payload jsonb
)
returns void
language plpgsql
immutable
security definer
set search_path = ''
as $$
begin
  perform private.validate_command_draft_v1(
    'request', 'qualification_finalization', p_payload
  );
  if p_payload->>'account_id' !~ '^[a-z0-9][a-z0-9_-]{2,63}$'
     or p_payload->>'environment' not in ('paper', 'contract_test')
     or p_payload->>'ledger_checkpoint_sha256' !~ '^[0-9a-f]{64}$'
     or p_payload->>'execution_policy_sha256' !~ '^[0-9a-f]{64}$'
     or p_payload->>'risk_policy_sha256' !~ '^[0-9a-f]{64}$'
     or p_payload->>'release_sha' !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or nullif(btrim(p_payload->>'dataset_version'), '') is null
     or nullif(btrim(p_payload->>'execution_policy_version'), '') is null
     or p_payload->>'reason_code' <> 'qualification_evidence_ready'
     or (p_payload->>'account_snapshot_sequence')::bigint <= 0
     or (p_payload->>'valid_until')::timestamptz
       <= (p_payload->>'valid_from')::timestamptz
     or (p_payload->>'expires_at')::timestamptz
       <= (p_payload->>'requested_at')::timestamptz
     or (p_payload->>'expires_at')::timestamptz
       > (p_payload->>'requested_at')::timestamptz + interval '24 hours' then
    raise exception 'qualification_finalization_values_invalid' using errcode = '22023';
  end if;
  perform (p_payload->>'request_id')::uuid;
  perform (p_payload->>'qualification_id')::uuid;
  perform (p_payload->>'reference_bundle_request_id')::uuid;
  perform (p_payload->>'account_snapshot_id')::uuid;
  perform (p_payload->>'risk_policy_version_id')::uuid;
  perform (p_payload->>'strategy_version_id')::uuid;
  perform (p_payload->>'g1_run_id')::uuid;
  perform (p_payload->>'g2_run_id')::uuid;
  if p_payload->>'environment' = 'paper' then
    if jsonb_typeof(p_payload->'provider_contract_version') <> 'null'
       or jsonb_typeof(p_payload->'provider_openapi_sha256') <> 'null'
       or jsonb_typeof(p_payload->'contract_run_id') <> 'null' then
      raise exception 'paper_qualification_provider_pin_forbidden' using errcode = '23514';
    end if;
  else
    if nullif(btrim(p_payload->>'provider_contract_version'), '') is null
       or p_payload->>'provider_openapi_sha256' !~ '^[0-9a-f]{64}$'
       or jsonb_typeof(p_payload->'contract_run_id') <> 'string' then
      raise exception 'contract_qualification_provider_pin_required' using errcode = '23514';
    end if;
    perform (p_payload->>'contract_run_id')::uuid;
  end if;
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'qualification_finalization_values_invalid' using errcode = '22023';
end;
$$;

create or replace function private.assert_qualification_candidate_v1(
  p_payload jsonb,
  p_checked_at timestamptz
)
returns void
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  bundle_row private.reference_bundle_materializations%rowtype;
  request_row private.control_approval_requests%rowtype;
  snapshot_row private.account_snapshots%rowtype;
  g1_row private.qualification_runs%rowtype;
  g2_row private.qualification_runs%rowtype;
  contract_run_row private.qualification_runs%rowtype;
  policy_row private.paper_execution_policies%rowtype;
  cost_row private.execution_cost_schedules%rowtype;
  calendar_row private.market_calendars%rowtype;
  model_row private.paper_execution_model_registry%rowtype;
  provider_row private.provider_contract_registry%rowtype;
  control_row private.execution_controls%rowtype;
  valid_from_value timestamptz := (p_payload->>'valid_from')::timestamptz;
  valid_until_value timestamptz := (p_payload->>'valid_until')::timestamptz;
begin
  perform private.validate_qualification_finalization_draft_v1(p_payload);
  select * into bundle_row
  from private.reference_bundle_materializations
  where request_id = (p_payload->>'reference_bundle_request_id')::uuid;
  select * into request_row
  from private.control_approval_requests
  where id = (p_payload->>'reference_bundle_request_id')::uuid;
  if bundle_row.request_id is null or request_row.id is null
     or request_row.account_id <> p_payload->>'account_id'
     or request_row.environment <> p_payload->>'environment'
     or request_row.payload->>'release_sha' <> p_payload->>'release_sha'
     or request_row.payload->>'bundle_version' <> p_payload->>'dataset_version' then
    raise exception 'qualification_reference_bundle_mismatch' using errcode = '23514';
  end if;
  select * into snapshot_row
  from private.account_snapshots
  where id = (p_payload->>'account_snapshot_id')::uuid;
  if not found
     or snapshot_row.account_id <> p_payload->>'account_id'
     or snapshot_row.environment <> p_payload->>'environment'
     or snapshot_row.checkpoint_schema_version <> 1
     or snapshot_row.sequence <> (p_payload->>'account_snapshot_sequence')::bigint
     or snapshot_row.ledger_checkpoint_sha256
       <> p_payload->>'ledger_checkpoint_sha256'
     or exists (
       select 1 from private.account_snapshots as newer
       where newer.account_id = snapshot_row.account_id
         and newer.environment = snapshot_row.environment
         and newer.checkpoint_schema_version = 1
         and newer.sequence > snapshot_row.sequence
     ) then
    raise exception 'qualification_latest_snapshot_required' using errcode = '23514';
  end if;
  select * into g1_row from private.qualification_runs
  where id = (p_payload->>'g1_run_id')::uuid;
  select * into g2_row from private.qualification_runs
  where id = (p_payload->>'g2_run_id')::uuid;
  if g1_row.id is null or g2_row.id is null
     or g1_row.run_kind <> 'g1' or g2_row.run_kind <> 'g2'
     or g1_row.result <> 'pass' or g2_row.result <> 'pass'
     or g1_row.account_id <> p_payload->>'account_id'
     or g2_row.account_id <> p_payload->>'account_id'
     or g1_row.environment <> p_payload->>'environment'
     or g2_row.environment <> p_payload->>'environment'
     or g1_row.reference_bundle_request_id <> bundle_row.request_id
     or g2_row.reference_bundle_request_id <> bundle_row.request_id
     or g1_row.account_snapshot_id <> snapshot_row.id
     or g2_row.account_snapshot_id <> snapshot_row.id
     or g1_row.ledger_checkpoint_sha256 <> snapshot_row.ledger_checkpoint_sha256
     or g2_row.ledger_checkpoint_sha256 <> snapshot_row.ledger_checkpoint_sha256
     or g1_row.release_sha <> p_payload->>'release_sha'
     or g2_row.release_sha <> p_payload->>'release_sha'
     or g1_row.completed_at > p_checked_at or g2_row.completed_at > p_checked_at then
    raise exception 'qualification_pass_runs_required' using errcode = '23514';
  end if;
  if p_payload->>'environment' = 'contract_test' then
    select * into contract_run_row from private.qualification_runs
    where id = (p_payload->>'contract_run_id')::uuid;
    if not found
       or contract_run_row.run_kind <> 'contract_qualification'
       or contract_run_row.result <> 'pass'
       or contract_run_row.account_id <> p_payload->>'account_id'
       or contract_run_row.environment <> 'contract_test'
       or contract_run_row.reference_bundle_request_id <> bundle_row.request_id
       or contract_run_row.account_snapshot_id <> snapshot_row.id
       or contract_run_row.ledger_checkpoint_sha256
         <> snapshot_row.ledger_checkpoint_sha256
       or contract_run_row.release_sha <> p_payload->>'release_sha'
       or contract_run_row.completed_at > p_checked_at then
      raise exception 'contract_qualification_pass_run_required' using errcode = '23514';
    end if;
  end if;
  select * into policy_row from private.paper_execution_policies
  where id = bundle_row.policy_id;
  select * into cost_row from private.execution_cost_schedules
  where id = bundle_row.cost_schedule_id;
  select * into calendar_row from private.market_calendars
  where id = bundle_row.calendar_id;
  select * into model_row from private.paper_execution_model_registry
  where id = bundle_row.execution_model_id;
  if policy_row.id is null or cost_row.id is null
     or calendar_row.id is null or model_row.id is null
     or policy_row.status <> 'approved' or cost_row.status <> 'approved'
     or calendar_row.status <> 'approved' or model_row.status <> 'approved'
     or policy_row.account_id <> p_payload->>'account_id'
     or cost_row.account_id <> p_payload->>'account_id'
     or calendar_row.environment <> p_payload->>'environment'
     or model_row.environment <> p_payload->>'environment'
     or model_row.market_calendar_id <> calendar_row.id
     or policy_row.policy_version <> p_payload->>'execution_policy_version'
     or policy_row.policy_sha256 <> p_payload->>'execution_policy_sha256'
     or valid_from_value < policy_row.effective_from
     or valid_until_value > policy_row.effective_until
     or valid_from_value < cost_row.effective_from
     or valid_until_value > cost_row.effective_until
     or valid_from_value < model_row.effective_from
     or valid_until_value > model_row.effective_until
     or valid_from_value::date < calendar_row.valid_from
     or valid_until_value::date > calendar_row.valid_until then
    raise exception 'qualification_reference_window_or_policy_mismatch' using errcode = '23514';
  end if;
  select * into control_row
  from private.execution_controls
  where account_id = p_payload->>'account_id';
  if not found or control_row.environment <> p_payload->>'environment'
     or control_row.execution_enabled is true
     or control_row.execution_policy_version <> p_payload->>'execution_policy_version'
     or control_row.execution_policy_sha256 <> p_payload->>'execution_policy_sha256'
     or control_row.active_risk_policy_version_id
       is distinct from (p_payload->>'risk_policy_version_id')::uuid
     or control_row.risk_policy_sha256 <> p_payload->>'risk_policy_sha256' then
    raise exception 'qualification_current_control_pin_mismatch' using errcode = '23514';
  end if;
  if not exists (
    select 1 from public.strategy_versions
    where id = (p_payload->>'strategy_version_id')::uuid
      and status in ('paper', 'active')
  ) then
    raise exception 'qualification_strategy_not_reviewed' using errcode = '23514';
  end if;
  if p_payload->>'environment' = 'paper' then
    if bundle_row.provider_contract_id is not null
       or control_row.provider_contract_version is not null
       or control_row.provider_openapi_sha256 is not null then
      raise exception 'paper_qualification_provider_pin_forbidden' using errcode = '23514';
    end if;
  else
    select * into provider_row from private.provider_contract_registry
    where id = bundle_row.provider_contract_id;
    if not found or provider_row.status <> 'approved'
       or provider_row.contract_version <> p_payload->>'provider_contract_version'
       or provider_row.openapi_sha256 <> p_payload->>'provider_openapi_sha256'
       or provider_row.release_sha <> p_payload->>'release_sha'
       or control_row.provider_contract_version <> provider_row.contract_version
       or control_row.provider_openapi_sha256 <> provider_row.openapi_sha256
       or valid_from_value < provider_row.effective_from
       or valid_until_value > provider_row.effective_until then
      raise exception 'contract_qualification_provider_pin_mismatch' using errcode = '23514';
    end if;
  end if;
  if valid_from_value > p_checked_at
     or valid_until_value <= p_checked_at
     or (p_payload->>'expires_at')::timestamptz > valid_until_value then
    raise exception 'qualification_validity_window_not_current' using errcode = '23514';
  end if;
end;
$$;

create or replace function private.request_qualification_finalization_v1_impl(
  p_request_payload jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid;
  request_id_value uuid;
  requested_time timestamptz;
  expiry_time timestamptz;
  draft_payload jsonb;
  payload_digest text;
  existing_row private.control_approval_requests%rowtype;
begin
  perform private.assert_exact_json_keys(p_request_payload, array[
    'schema_version', 'request_id', 'command_type', 'idempotency_key',
    'qualification_id', 'account_id', 'environment',
    'reference_bundle_request_id', 'account_snapshot_id',
    'account_snapshot_sequence', 'ledger_checkpoint_sha256',
    'dataset_version', 'execution_policy_version',
    'execution_policy_sha256', 'risk_policy_version_id',
    'risk_policy_sha256', 'strategy_version_id',
    'provider_contract_version', 'provider_openapi_sha256',
    'g1_run_id', 'g2_run_id', 'contract_run_id', 'release_sha',
    'valid_from', 'valid_until', 'requested_at', 'expires_at',
    'reason_code', 'step_up_grant_id', 'command_hash',
    'step_up_grant_issued_at', 'step_up_grant_expires_at',
    'step_up_grant_one_time', 'step_up_grant_consumed_at',
    'bound_action', 'bound_command_type'
  ]);
  if jsonb_typeof(p_request_payload->'step_up_grant_one_time') <> 'boolean'
     or jsonb_typeof(p_request_payload->'step_up_grant_consumed_at') <> 'null' then
    raise exception 'qualification_finalization_request_json_type_invalid'
      using errcode = '22023';
  end if;
  draft_payload := private.command_draft_v1(p_request_payload);
  perform private.validate_qualification_finalization_draft_v1(draft_payload);
  request_id_value := (p_request_payload->>'request_id')::uuid;
  requested_time := (p_request_payload->>'requested_at')::timestamptz;
  expiry_time := (p_request_payload->>'expires_at')::timestamptz;
  if requested_time < clock_timestamp() - interval '5 minutes'
     or requested_time > clock_timestamp() + interval '30 seconds'
     or p_request_payload->>'idempotency_key'
       !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$' then
    raise exception 'qualification_finalization_request_values_invalid'
      using errcode = '22023';
  end if;
  actor := private.require_human_roles(array['release_manager'], true);
  perform private.require_recent_aal2();
  if private.has_active_role(actor, 'platform_admin') then
    raise exception 'platform_admin_trading_action_forbidden' using errcode = '42501';
  end if;
  if not exists (
    select 1 from private.trading_accounts
    where account_id = p_request_payload->>'account_id'
      and environment = p_request_payload->>'environment'
      and state = 'open'
  ) then
    raise exception 'qualification_account_not_open' using errcode = '23514';
  end if;
  perform private.assert_qualification_candidate_v1(draft_payload, requested_time);
  payload_digest := private.sha256_jsonb_v1(draft_payload);
  select * into existing_row
  from private.control_approval_requests
  where request_kind = 'qualification_finalization'
    and idempotency_key = p_request_payload->>'idempotency_key';
  if found then
    if existing_row.id <> request_id_value
       or existing_row.payload_sha256 <> payload_digest
       or existing_row.payload <> draft_payload
       or existing_row.requester_user_id <> actor then
      raise exception 'control_approval_idempotency_conflict' using errcode = '23505';
    end if;
    return private.control_approval_receipt_v1(existing_row.id);
  end if;
  if p_request_payload->>'command_hash' is distinct from
       private.compute_command_sha256('operation_v1:request', draft_payload) then
    raise exception 'qualification_finalization_request_hash_mismatch'
      using errcode = '42501';
  end if;
  perform private.consume_bound_step_up_v1(
    p_request_payload, 'request', 'qualification_finalization', requested_time,
    'request_qualification_finalization_v1'
  );
  insert into private.control_approval_requests (
    id, request_kind, account_id, environment, payload, payload_sha256,
    requester_user_id, requested_at, expires_at, idempotency_key
  ) values (
    request_id_value, 'qualification_finalization',
    p_request_payload->>'account_id', p_request_payload->>'environment',
    draft_payload, payload_digest, actor, requested_time, expiry_time,
    p_request_payload->>'idempotency_key'
  );
  perform private.write_audit_event(
    'human', actor, 'release_manager', null, null,
    p_request_payload->>'release_sha', 'qualification_finalization_requested',
    'control_approval_request', request_id_value::text, request_id_value,
    null, p_request_payload->>'reason_code', null,
    array['payload_sha256'], null, payload_digest, null
  );
  return private.control_approval_receipt_v1(request_id_value);
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'qualification_finalization_request_values_invalid'
      using errcode = '22023';
end;
$$;

create or replace function private.materialize_qualification_v1(
  p_request_id uuid,
  p_review_id uuid
)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
  request_row private.control_approval_requests%rowtype;
  review_row private.control_approval_reviews%rowtype;
  payload jsonb;
  g1_row private.qualification_runs%rowtype;
  g2_row private.qualification_runs%rowtype;
  contract_row private.qualification_runs%rowtype;
  qualification_id_value uuid;
  g1_evidence_id_value uuid := gen_random_uuid();
  g2_evidence_id_value uuid := gen_random_uuid();
  contract_evidence_id_value uuid;
begin
  select * into request_row
  from private.control_approval_requests
  where id = p_request_id and request_kind = 'qualification_finalization'
  for update;
  select * into review_row
  from private.control_approval_reviews
  where id = p_review_id and request_id = p_request_id
    and decision = 'approved';
  if request_row.id is null or review_row.id is null then
    raise exception 'approved_qualification_review_required' using errcode = '23514';
  end if;
  if exists (
    select 1 from private.qualification_finalizations
    where request_id = p_request_id
  ) then
    select qualification_id into qualification_id_value
    from private.qualification_finalizations
    where request_id = p_request_id;
    return qualification_id_value;
  end if;
  payload := request_row.payload;
  perform private.assert_qualification_candidate_v1(payload, review_row.reviewed_at);
  qualification_id_value := (payload->>'qualification_id')::uuid;
  select * into g1_row from private.qualification_runs
  where id = (payload->>'g1_run_id')::uuid;
  select * into g2_row from private.qualification_runs
  where id = (payload->>'g2_run_id')::uuid;
  insert into private.control_evidence (
    id, evidence_type, environment, artifact_uri, artifact_sha256,
    captured_at, verified_at, verified_by, metadata_summary
  ) values
  (
    g1_evidence_id_value, 'g1_qualification', request_row.environment,
    'urn:sha256:' || g1_row.evidence_sha256, g1_row.evidence_sha256,
    g1_row.started_at, review_row.reviewed_at, review_row.reviewer_user_id,
    jsonb_build_object(
      'schema_version', 1, 'run_id', g1_row.id,
      'reference_bundle_request_id', g1_row.reference_bundle_request_id,
      'account_snapshot_id', g1_row.account_snapshot_id,
      'release_sha', g1_row.release_sha
    )
  ),
  (
    g2_evidence_id_value, 'g2_qualification', request_row.environment,
    'urn:sha256:' || g2_row.evidence_sha256, g2_row.evidence_sha256,
    g2_row.started_at, review_row.reviewed_at, review_row.reviewer_user_id,
    jsonb_build_object(
      'schema_version', 1, 'run_id', g2_row.id,
      'reference_bundle_request_id', g2_row.reference_bundle_request_id,
      'account_snapshot_id', g2_row.account_snapshot_id,
      'release_sha', g2_row.release_sha
    )
  );
  if request_row.environment = 'contract_test' then
    contract_evidence_id_value := gen_random_uuid();
    select * into contract_row from private.qualification_runs
    where id = (payload->>'contract_run_id')::uuid;
    insert into private.control_evidence (
      id, evidence_type, environment, artifact_uri, artifact_sha256,
      captured_at, verified_at, verified_by, metadata_summary
    ) values (
      contract_evidence_id_value, 'contract_qualification',
      request_row.environment,
      'urn:sha256:' || contract_row.evidence_sha256,
      contract_row.evidence_sha256, contract_row.started_at,
      review_row.reviewed_at, review_row.reviewer_user_id,
      jsonb_build_object(
        'schema_version', 1, 'run_id', contract_row.id,
        'reference_bundle_request_id', contract_row.reference_bundle_request_id,
        'account_snapshot_id', contract_row.account_snapshot_id,
        'release_sha', contract_row.release_sha
      )
    );
  end if;
  insert into private.qualifications (
    id, environment, status, release_sha, ledger_checkpoint,
    dataset_version, execution_policy_version, execution_policy_sha256,
    risk_policy_sha256, strategy_version_id, risk_policy_version_id,
    provider_contract_version, provider_openapi_sha256,
    valid_from, valid_until, g1_status, g1_checked_at, g1_evidence_id,
    g2_status, g2_checked_at, g2_evidence_id, account_id,
    checkpoint_schema_version, account_snapshot_id,
    account_snapshot_sequence, ledger_checkpoint_sha256,
    reference_bundle_request_id, g1_run_id, g2_run_id, contract_run_id,
    approval_request_id
  ) values (
    qualification_id_value, request_row.environment, 'qualified',
    payload->>'release_sha',
    'snapshot-v1:' || (payload->>'account_snapshot_id') || ':'
      || (payload->>'account_snapshot_sequence') || ':'
      || (payload->>'ledger_checkpoint_sha256'),
    payload->>'dataset_version', payload->>'execution_policy_version',
    payload->>'execution_policy_sha256', payload->>'risk_policy_sha256',
    (payload->>'strategy_version_id')::uuid,
    (payload->>'risk_policy_version_id')::uuid,
    payload->>'provider_contract_version', payload->>'provider_openapi_sha256',
    (payload->>'valid_from')::timestamptz,
    (payload->>'valid_until')::timestamptz,
    'pass', g1_row.completed_at, g1_evidence_id_value,
    'pass', g2_row.completed_at, g2_evidence_id_value,
    request_row.account_id, 1, (payload->>'account_snapshot_id')::uuid,
    (payload->>'account_snapshot_sequence')::bigint,
    payload->>'ledger_checkpoint_sha256',
    (payload->>'reference_bundle_request_id')::uuid,
    g1_row.id, g2_row.id, contract_row.id, request_row.id
  );
  insert into private.qualification_finalizations (
    request_id, review_id, qualification_id, g1_evidence_id,
    g2_evidence_id, contract_evidence_id, finalized_at
  ) values (
    request_row.id, review_row.id, qualification_id_value,
    g1_evidence_id_value, g2_evidence_id_value,
    contract_evidence_id_value, review_row.reviewed_at
  );
  return qualification_id_value;
end;
$$;

create or replace function private.review_qualification_finalization_v1_impl(
  p_review_payload jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid;
  review_id_value uuid;
  request_id_value uuid;
  reviewed_time timestamptz;
  draft_payload jsonb;
  review_digest text;
  request_row private.control_approval_requests%rowtype;
  existing_review private.control_approval_reviews%rowtype;
  qualification_id_value uuid;
begin
  perform private.assert_exact_json_keys(p_review_payload, array[
    'schema_version', 'review_id', 'request_id', 'command_type',
    'reviewer_role', 'decision', 'reason_code',
    'expected_request_payload_sha256', 'reviewed_at',
    'step_up_grant_id', 'command_hash', 'step_up_grant_issued_at',
    'step_up_grant_expires_at', 'step_up_grant_one_time',
    'step_up_grant_consumed_at', 'bound_action', 'bound_command_type'
  ]);
  draft_payload := private.command_draft_v1(p_review_payload);
  perform private.validate_command_draft_v1(
    'review', 'qualification_finalization', draft_payload
  );
  if p_review_payload->>'reviewer_role' <> 'risk_approver'
     or p_review_payload->>'decision' not in ('approve', 'reject')
     or (
       p_review_payload->>'decision' = 'approve'
       and p_review_payload->>'reason_code' <> 'qualification_verified'
     )
     or (
       p_review_payload->>'decision' = 'reject'
       and p_review_payload->>'reason_code' not in (
         'evidence_incomplete', 'risk_rejected', 'superseded'
       )
     ) then
    raise exception 'qualification_finalization_review_values_invalid'
      using errcode = '22023';
  end if;
  review_id_value := (p_review_payload->>'review_id')::uuid;
  request_id_value := (p_review_payload->>'request_id')::uuid;
  reviewed_time := (p_review_payload->>'reviewed_at')::timestamptz;
  if reviewed_time < clock_timestamp() - interval '5 minutes'
     or reviewed_time > clock_timestamp() + interval '30 seconds' then
    raise exception 'qualification_finalization_review_time_invalid'
      using errcode = '22023';
  end if;
  actor := private.require_human_roles(array['risk_approver'], true);
  perform private.require_recent_aal2();
  if private.has_active_role(actor, 'platform_admin') then
    raise exception 'platform_admin_trading_action_forbidden' using errcode = '42501';
  end if;
  select * into request_row
  from private.control_approval_requests
  where id = request_id_value and request_kind = 'qualification_finalization'
  for update;
  if not found then
    raise exception 'qualification_finalization_request_not_found'
      using errcode = 'P0002';
  end if;
  if request_row.expires_at <= reviewed_time
     or request_row.payload_sha256
       is distinct from p_review_payload->>'expected_request_payload_sha256' then
    raise exception 'qualification_finalization_review_stale'
      using errcode = '40001';
  end if;
  if p_review_payload->>'decision' = 'approve' then
    perform private.assert_qualification_candidate_v1(
      request_row.payload, reviewed_time
    );
  end if;
  review_digest := private.sha256_jsonb_v1(draft_payload);
  select * into existing_review
  from private.control_approval_reviews where request_id = request_id_value;
  if found then
    if existing_review.id <> review_id_value
       or existing_review.payload_sha256 <> review_digest
       or existing_review.payload <> draft_payload
       or existing_review.reviewer_user_id <> actor then
      raise exception 'control_approval_review_conflict' using errcode = '23505';
    end if;
    return private.control_approval_receipt_v1(request_id_value);
  end if;
  if p_review_payload->>'command_hash' is distinct from
       private.compute_command_sha256('operation_v1:review', draft_payload) then
    raise exception 'qualification_finalization_review_hash_mismatch'
      using errcode = '42501';
  end if;
  perform private.consume_bound_step_up_v1(
    p_review_payload, 'review', 'qualification_finalization', reviewed_time,
    'review_qualification_finalization_v1'
  );
  insert into private.control_approval_reviews (
    id, request_id, decision, payload, payload_sha256,
    expected_request_payload_sha256, reviewer_user_id, reviewed_at
  ) values (
    review_id_value, request_id_value,
    case when p_review_payload->>'decision' = 'approve'
      then 'approved' else 'rejected' end,
    draft_payload, review_digest,
    p_review_payload->>'expected_request_payload_sha256', actor, reviewed_time
  );
  if p_review_payload->>'decision' = 'approve' then
    qualification_id_value := private.materialize_qualification_v1(
      request_id_value, review_id_value
    );
  end if;
  perform private.write_audit_event(
    'human', actor, 'risk_approver', null, null,
    request_row.payload->>'release_sha',
    'qualification_finalization_' || case
      when p_review_payload->>'decision' = 'approve' then 'approved'
      else 'rejected' end,
    'control_approval_request', request_id_value::text, review_id_value,
    null, p_review_payload->>'reason_code', null,
    array['decision'], request_row.payload_sha256, review_digest,
    case when qualification_id_value is null then null
      else (select g2_evidence_id from private.qualifications
            where id = qualification_id_value) end
  );
  return private.control_approval_receipt_v1(request_id_value);
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'qualification_finalization_review_values_invalid'
      using errcode = '22023';
end;
$$;

create or replace function private.guard_operation_command_v1_qualification()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  qualification_row private.qualifications%rowtype;
begin
  if new.command_type not in (
    'paper_resume', 'contract_test_enable',
    'strategy_promotion', 'risk_policy_change'
  ) then
    return new;
  end if;
  select * into qualification_row
  from private.qualifications
  where id::text = new.requested_change->>'qualification_id'
    and checkpoint_schema_version = 1
    and status = 'qualified'
    and account_id = new.requested_change->>'account_id'
    and environment = new.requested_change->>'environment'
    and valid_from <= new.requested_at
    and valid_until > new.requested_at
    and release_sha = new.target_release_sha
    and release_sha = new.requested_change->>'release_sha'
    and ledger_checkpoint = new.requested_change->>'ledger_checkpoint'
    and ledger_checkpoint = 'snapshot-v1:' || account_snapshot_id::text
      || ':' || account_snapshot_sequence::text
      || ':' || ledger_checkpoint_sha256
    and strategy_version_id::text = new.requested_change->>'strategy_version_id'
    and risk_policy_version_id::text = new.requested_change->>'risk_policy_version_id'
    and g1_status = 'pass' and g2_status = 'pass';
  if not found
     or exists (
       select 1 from private.account_snapshots as newer
       where newer.account_id = qualification_row.account_id
         and newer.environment = qualification_row.environment
         and newer.checkpoint_schema_version = 1
         and newer.sequence > qualification_row.account_snapshot_sequence
     )
     or private.compute_ledger_checkpoint_sha256_v1(
       qualification_row.account_id, qualification_row.environment
     ) <> qualification_row.ledger_checkpoint_sha256 then
    raise exception 'qualification_v1_required_for_operation_command'
      using errcode = '23514';
  end if;
  return new;
end;
$$;

drop trigger if exists guard_operation_command_v1_qualification
  on private.operation_commands;
create trigger guard_operation_command_v1_qualification
  before insert on private.operation_commands
  for each row execute function private.guard_operation_command_v1_qualification();

create or replace function private.guard_execution_control_qualification_freshness_v1()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  command_row private.operation_commands%rowtype;
  qualification_row private.qualifications%rowtype;
  authorization_time timestamptz := clock_timestamp();
begin
  if not new.execution_enabled then
    return new;
  end if;
  if exists (
    select 1
    from private.reconciliation_breaks as reconciliation_break
    where reconciliation_break.account_id = new.account_id
      and reconciliation_break.break_type <> 'legacy_unreconciled'
      and reconciliation_break.state <> 'resolved'
  ) then
    raise exception 'unresolved_reconciliation_break_blocks_execution_enable'
      using errcode = '40001';
  end if;
  if not old.execution_enabled
     and new.last_command_id is not distinct from old.last_command_id then
    raise exception 'new_qualified_command_required_for_execution_enable'
      using errcode = '23514';
  end if;
  if new.last_command_id is not distinct from old.last_command_id
     and (
       new.active_strategy_version_id is distinct from old.active_strategy_version_id
       or new.active_risk_policy_version_id is distinct from old.active_risk_policy_version_id
       or new.execution_policy_version is distinct from old.execution_policy_version
       or new.execution_policy_sha256 is distinct from old.execution_policy_sha256
       or new.risk_policy_sha256 is distinct from old.risk_policy_sha256
       or new.provider_contract_version is distinct from old.provider_contract_version
       or new.provider_openapi_sha256 is distinct from old.provider_openapi_sha256
     ) then
    raise exception 'new_qualified_command_required_for_control_pin_change'
      using errcode = '23514';
  end if;
  select * into command_row
  from private.operation_commands where id = new.last_command_id;
  if not found or command_row.command_type not in (
    'paper_resume', 'contract_test_enable',
    'strategy_promotion', 'risk_policy_change'
  )
     or command_row.state not in ('claimed', 'applied')
     or (command_row.requested_change->>'expected_state_version')::bigint
       <> old.control_epoch then
    raise exception 'qualified_operation_command_required_for_enabled_control'
      using errcode = '23514';
  end if;
  select * into qualification_row
  from private.qualifications
  where id::text = command_row.requested_change->>'qualification_id'
    and checkpoint_schema_version = 1
    and status = 'qualified'
    and account_id = new.account_id
    and environment = new.environment
    and valid_from <= authorization_time
    and valid_until > authorization_time
    and g1_status = 'pass' and g2_status = 'pass'
    and release_sha = command_row.target_release_sha
    and release_sha = command_row.requested_change->>'release_sha'
    and ledger_checkpoint = command_row.requested_change->>'ledger_checkpoint'
    and ledger_checkpoint = 'snapshot-v1:' || account_snapshot_id::text
      || ':' || account_snapshot_sequence::text
      || ':' || ledger_checkpoint_sha256
    and strategy_version_id::text = command_row.requested_change->>'strategy_version_id'
    and risk_policy_version_id::text = command_row.requested_change->>'risk_policy_version_id'
    and strategy_version_id::text = new.active_strategy_version_id
    and risk_policy_version_id = new.active_risk_policy_version_id
    and execution_policy_version = new.execution_policy_version
    and execution_policy_sha256 = new.execution_policy_sha256
    and risk_policy_sha256 = new.risk_policy_sha256
    and provider_contract_version is not distinct from new.provider_contract_version
    and provider_openapi_sha256 is not distinct from new.provider_openapi_sha256;
  if not found
     or exists (
       select 1 from private.account_snapshots as newer
       where newer.account_id = qualification_row.account_id
         and newer.environment = qualification_row.environment
         and newer.checkpoint_schema_version = 1
         and newer.sequence > qualification_row.account_snapshot_sequence
     )
     or private.compute_ledger_checkpoint_sha256_v1(
       qualification_row.account_id, qualification_row.environment
     ) <> qualification_row.ledger_checkpoint_sha256 then
    raise exception 'qualified_evidence_bundle_expired_or_mismatched_at_application'
      using errcode = '23514';
  end if;
  new.expires_at := least(new.expires_at, qualification_row.valid_until);
  if new.expires_at <= new.effective_at then
    raise exception 'qualified_execution_window_expired_at_application'
      using errcode = '23514';
  end if;
  return new;
end;
$$;

-- Legacy qualifications remain readable but can no longer keep execution
-- enabled after the v1 checkpoint contract is installed.
with convergence_time as (
  select clock_timestamp() as value
)
update private.execution_controls as control
set execution_enabled = false,
    control_epoch = control.control_epoch + 1,
    effective_at = greatest(
      convergence_time.value, control.updated_at + interval '1 microsecond'
    ),
    expires_at = greatest(
      control.expires_at,
      greatest(
        convergence_time.value, control.updated_at + interval '1 microsecond'
      ) + interval '1 second'
    ),
    updated_reason_code = 'qualification_v1_required',
    updated_at = greatest(
      convergence_time.value, control.updated_at + interval '1 microsecond'
    )
from convergence_time
where control.execution_enabled
  and not exists (
    select 1
    from private.operation_commands as command
    join private.qualifications as qualification
      on qualification.id::text = command.requested_change->>'qualification_id'
    where command.id = control.last_command_id
      and qualification.checkpoint_schema_version = 1
      and qualification.status = 'qualified'
      and qualification.account_id = control.account_id
      and qualification.environment = control.environment
      and qualification.valid_from <= convergence_time.value
      and qualification.valid_until > convergence_time.value
      and qualification.ledger_checkpoint = 'snapshot-v1:'
        || qualification.account_snapshot_id::text || ':'
        || qualification.account_snapshot_sequence::text || ':'
        || qualification.ledger_checkpoint_sha256
  );

create or replace function private.get_control_approval_receipt_v1_impl(
  p_request_id uuid
)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  actor uuid;
begin
  actor := private.require_human_roles(array[
    'platform_admin', 'operator', 'risk_approver', 'strategy_reviewer',
    'auditor', 'release_manager', 'viewer'
  ], true);
  return private.control_approval_receipt_v1(p_request_id);
end;
$$;

create or replace function api.request_reference_bundle_v1(
  request_payload jsonb
)
returns jsonb
language sql
security invoker
set search_path = ''
as $$ select private.request_reference_bundle_v1_impl(request_payload); $$;

create or replace function api.review_reference_bundle_v1(
  review_payload jsonb
)
returns jsonb
language sql
security invoker
set search_path = ''
as $$ select private.review_reference_bundle_v1_impl(review_payload); $$;

create or replace function api.request_qualification_finalization_v1(
  request_payload jsonb
)
returns jsonb
language sql
security invoker
set search_path = ''
as $$
  select private.request_qualification_finalization_v1_impl(request_payload);
$$;

create or replace function api.review_qualification_finalization_v1(
  review_payload jsonb
)
returns jsonb
language sql
security invoker
set search_path = ''
as $$
  select private.review_qualification_finalization_v1_impl(review_payload);
$$;

create or replace function api.get_control_approval_receipt_v1(
  request_id uuid
)
returns jsonb
language sql
stable
security invoker
set search_path = ''
as $$ select private.get_control_approval_receipt_v1_impl(request_id); $$;

create or replace function worker_api.capture_qualification_snapshot_v1(
  p_account_id text,
  p_environment text,
  p_release_sha text,
  p_observed_at timestamptz
)
returns jsonb
language sql
security invoker
set search_path = ''
as $$
  select private.capture_qualification_snapshot_v1_impl(
    p_account_id, p_environment, p_release_sha, p_observed_at
  );
$$;

create or replace function worker_api.register_qualification_run_v1(
  run_payload jsonb
)
returns jsonb
language sql
security invoker
set search_path = ''
as $$ select private.register_qualification_run_v1_impl(run_payload); $$;

revoke execute on function
  private.reference_bundle_content_v1(jsonb),
  private.validate_reference_bundle_draft_v1(jsonb),
  private.control_approval_receipt_v1(uuid),
  private.request_reference_bundle_v1_impl(jsonb),
  private.materialize_reference_bundle_v1(uuid, uuid),
  private.review_reference_bundle_v1_impl(jsonb),
  private.capture_qualification_snapshot_v1_impl(text, text, text, timestamptz),
  private.qualification_run_content_v1(jsonb),
  private.validate_qualification_run_v1(jsonb),
  private.register_qualification_run_v1_impl(jsonb),
  private.validate_qualification_finalization_draft_v1(jsonb),
  private.assert_qualification_candidate_v1(jsonb, timestamptz),
  private.request_qualification_finalization_v1_impl(jsonb),
  private.materialize_qualification_v1(uuid, uuid),
  private.review_qualification_finalization_v1_impl(jsonb),
  private.guard_operation_command_v1_qualification(),
  private.guard_execution_control_qualification_freshness_v1(),
  private.get_control_approval_receipt_v1_impl(uuid)
from public, anon, authenticated, service_role;

grant execute on function
  private.request_reference_bundle_v1_impl(jsonb),
  private.review_reference_bundle_v1_impl(jsonb),
  private.request_qualification_finalization_v1_impl(jsonb),
  private.review_qualification_finalization_v1_impl(jsonb),
  private.get_control_approval_receipt_v1_impl(uuid)
to authenticated;

grant execute on function
  private.capture_qualification_snapshot_v1_impl(text, text, text, timestamptz),
  private.register_qualification_run_v1_impl(jsonb)
to service_role;

revoke all on function
  api.request_reference_bundle_v1(jsonb),
  api.review_reference_bundle_v1(jsonb),
  api.request_qualification_finalization_v1(jsonb),
  api.review_qualification_finalization_v1(jsonb),
  api.get_control_approval_receipt_v1(uuid)
from public, anon, service_role;
grant execute on function
  api.request_reference_bundle_v1(jsonb),
  api.review_reference_bundle_v1(jsonb),
  api.request_qualification_finalization_v1(jsonb),
  api.review_qualification_finalization_v1(jsonb),
  api.get_control_approval_receipt_v1(uuid)
to authenticated;

revoke all on function
  worker_api.capture_qualification_snapshot_v1(text, text, text, timestamptz),
  worker_api.register_qualification_run_v1(jsonb)
from public, anon, authenticated;
grant execute on function
  worker_api.capture_qualification_snapshot_v1(text, text, text, timestamptz),
  worker_api.register_qualification_run_v1(jsonb)
to service_role;
