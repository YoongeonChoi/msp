-- Reviewed accounting closure for execution observations that entered the
-- manual/unknown state.
--
-- The existing V1 unknown-resolution workflow is intentionally left as an
-- evidence-only workflow.  Only a schema_version=2 request which passes the
-- dedicated maker/checker and worker claim/apply path below may add provider
-- fills or accounting entries.

begin;

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
      'contract_qualification',
      'unknown_execution_resolution'
    )
  );

create table private.unknown_execution_resolution_requests_v2 (
  id uuid primary key,
  command_id uuid not null unique references private.operation_commands(id),
  break_id uuid not null references private.reconciliation_breaks(id),
  intent_id uuid not null references private.order_intents(id),
  unknown_observation_id uuid not null
    references private.execution_observations(id),
  account_id text not null references private.trading_accounts(account_id),
  environment text not null check (environment in ('paper', 'contract_test')),
  provider_order_id text not null check (
    nullif(btrim(provider_order_id), '') is not null
  ),
  terminal_status text not null check (
    terminal_status in ('filled', 'canceled', 'expired', 'rejected')
  ),
  evidence_artifact_uri text not null,
  evidence_sha256 text not null check (evidence_sha256 ~ '^[0-9a-f]{64}$'),
  evidence_captured_at timestamptz not null,
  expected_break_revision bigint not null check (expected_break_revision >= 0),
  expected_cash_projection_version bigint not null check (
    expected_cash_projection_version >= 0
  ),
  expected_position_projection_version bigint check (
    expected_position_projection_version is null
    or expected_position_projection_version >= 0
  ),
  expected_reservation_event_sequence integer not null check (
    expected_reservation_event_sequence > 0
  ),
  expected_control_epoch bigint not null check (expected_control_epoch > 0),
  baseline_sequence integer not null check (baseline_sequence > 0),
  baseline_cumulative_quantity bigint not null check (
    baseline_cumulative_quantity >= 0
  ),
  baseline_cumulative_gross_krw bigint not null check (
    baseline_cumulative_gross_krw >= 0
  ),
  baseline_cumulative_commission_krw bigint not null check (
    baseline_cumulative_commission_krw >= 0
  ),
  baseline_cumulative_tax_krw bigint not null check (
    baseline_cumulative_tax_krw >= 0
  ),
  payload jsonb not null check (jsonb_typeof(payload) = 'object'),
  payload_sha256 text not null check (payload_sha256 ~ '^[0-9a-f]{64}$'),
  requester_user_id uuid not null references auth.users(id),
  requested_at timestamptz not null,
  expires_at timestamptz not null,
  idempotency_key text not null unique,
  created_at timestamptz not null default clock_timestamp(),
  constraint unknown_resolution_v2_identity_check check (id = command_id),
  constraint unknown_resolution_v2_window_check check (
    evidence_captured_at <= requested_at
    and expires_at > requested_at
    and expires_at <= requested_at + interval '24 hours'
  ),
  constraint unknown_resolution_v2_artifact_uri_check check (
    (
      evidence_artifact_uri ~ '^https://[^/?#[:space:]]+/'
      and evidence_artifact_uri !~ '[?#]'
    )
    or evidence_artifact_uri = 'urn:sha256:' || evidence_sha256
  ),
  unique (id, payload_sha256)
);

create table private.unknown_execution_resolution_fill_proposals_v2 (
  request_id uuid not null
    references private.unknown_execution_resolution_requests_v2(id),
  fill_sequence integer not null check (fill_sequence > 0),
  provider_order_id text not null check (
    nullif(btrim(provider_order_id), '') is not null
  ),
  provider_execution_id text not null check (
    nullif(btrim(provider_execution_id), '') is not null
  ),
  quantity bigint not null check (quantity > 0),
  price_krw bigint not null check (price_krw > 0),
  commission_krw bigint not null check (commission_krw >= 0),
  tax_krw bigint not null check (tax_krw >= 0),
  filled_at timestamptz not null,
  settlement_date date not null,
  evidence_sha256 text not null check (evidence_sha256 ~ '^[0-9a-f]{64}$'),
  proposal_sha256 text not null check (proposal_sha256 ~ '^[0-9a-f]{64}$'),
  created_at timestamptz not null default clock_timestamp(),
  primary key (request_id, fill_sequence),
  unique (request_id, provider_execution_id),
  unique (request_id, proposal_sha256)
);

create table private.unknown_execution_resolution_reviews_v2 (
  id uuid primary key,
  request_id uuid not null unique
    references private.unknown_execution_resolution_requests_v2(id),
  command_id uuid not null unique references private.operation_commands(id),
  decision text not null check (decision in ('approved', 'rejected')),
  reason_code text not null,
  expected_request_payload_sha256 text not null check (
    expected_request_payload_sha256 ~ '^[0-9a-f]{64}$'
  ),
  evidence_sha256 text not null check (evidence_sha256 ~ '^[0-9a-f]{64}$'),
  payload jsonb not null check (jsonb_typeof(payload) = 'object'),
  payload_sha256 text not null check (payload_sha256 ~ '^[0-9a-f]{64}$'),
  reviewer_user_id uuid not null references auth.users(id),
  step_up_grant_id uuid not null unique references private.step_up_grants(id),
  reviewed_at timestamptz not null,
  created_at timestamptz not null default clock_timestamp(),
  constraint unknown_resolution_v2_review_identity_check check (
    request_id = command_id
  ),
  unique (request_id, payload_sha256)
);

create table private.unknown_execution_resolution_work_items_v2 (
  command_id uuid primary key references private.operation_commands(id),
  request_id uuid not null unique
    references private.unknown_execution_resolution_requests_v2(id),
  review_id uuid not null unique
    references private.unknown_execution_resolution_reviews_v2(id),
  state text not null check (state in ('approved', 'claimed', 'applied')),
  revision bigint not null default 0 check (revision >= 0),
  approved_break_revision bigint not null check (approved_break_revision >= 0),
  claim_token uuid,
  claim_owner text,
  claim_release_sha text check (
    claim_release_sha is null
    or claim_release_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
  ),
  claim_fencing_token bigint check (
    claim_fencing_token is null or claim_fencing_token > 0
  ),
  claimed_at timestamptz,
  claim_expires_at timestamptz,
  applied_at timestamptz,
  updated_at timestamptz not null default clock_timestamp(),
  constraint unknown_resolution_v2_work_identity_check check (
    command_id = request_id
  ),
  constraint unknown_resolution_v2_work_shape_check check (
    (
      state = 'approved'
      and claim_token is null and claim_owner is null
      and claim_release_sha is null and claim_fencing_token is null
      and claimed_at is null and claim_expires_at is null and applied_at is null
    )
    or (
      state = 'claimed'
      and claim_token is not null
      and nullif(btrim(claim_owner), '') is not null
      and claim_release_sha is not null and claim_fencing_token is not null
      and claimed_at is not null and claim_expires_at > claimed_at
      and applied_at is null
    )
    or (
      state = 'applied'
      and claim_token is not null
      and nullif(btrim(claim_owner), '') is not null
      and claim_release_sha is not null and claim_fencing_token is not null
      and claimed_at is not null and claim_expires_at > claimed_at
      and applied_at >= claimed_at
    )
  )
);

create index unknown_resolution_v2_work_claim_index
  on private.unknown_execution_resolution_work_items_v2 (state, command_id)
  where state in ('approved', 'claimed');

create table private.unknown_execution_resolution_applications_v2 (
  id uuid primary key,
  command_id uuid not null unique references private.operation_commands(id),
  request_id uuid not null unique
    references private.unknown_execution_resolution_requests_v2(id),
  review_id uuid not null unique
    references private.unknown_execution_resolution_reviews_v2(id),
  break_id uuid not null unique references private.reconciliation_breaks(id),
  intent_id uuid not null unique references private.order_intents(id),
  evidence_id uuid not null references private.control_evidence(id),
  terminal_observation_id uuid not null unique
    references private.execution_observations(id),
  request_payload_sha256 text not null check (
    request_payload_sha256 ~ '^[0-9a-f]{64}$'
  ),
  review_payload_sha256 text not null check (
    review_payload_sha256 ~ '^[0-9a-f]{64}$'
  ),
  claim_token uuid not null,
  worker_id text not null,
  release_sha text not null check (
    release_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
  ),
  fencing_token bigint not null check (fencing_token > 0),
  control_epoch bigint not null check (control_epoch > 0),
  final_cumulative_quantity bigint not null check (final_cumulative_quantity >= 0),
  final_cumulative_gross_krw bigint not null check (
    final_cumulative_gross_krw >= 0
  ),
  final_cumulative_commission_krw bigint not null check (
    final_cumulative_commission_krw >= 0
  ),
  final_cumulative_tax_krw bigint not null check (
    final_cumulative_tax_krw >= 0
  ),
  terminal_status text not null check (
    terminal_status in ('filled', 'canceled', 'expired', 'rejected')
  ),
  command_revision bigint not null check (command_revision >= 0),
  work_revision bigint not null check (work_revision >= 0),
  applied_at timestamptz not null,
  application_sha256 text not null unique check (
    application_sha256 ~ '^[0-9a-f]{64}$'
  ),
  created_at timestamptz not null default clock_timestamp(),
  constraint unknown_resolution_v2_application_identity_check check (
    command_id = request_id
  )
);

create table private.unknown_execution_resolution_application_fills_v2 (
  application_id uuid not null
    references private.unknown_execution_resolution_applications_v2(id),
  fill_sequence integer not null check (fill_sequence > 0),
  proposal_sha256 text not null check (proposal_sha256 ~ '^[0-9a-f]{64}$'),
  observation_id uuid not null unique references private.execution_observations(id),
  fill_id uuid not null unique references private.fills(id),
  accounting_transaction_id uuid not null unique
    references private.accounting_transactions(id),
  settlement_obligation_id uuid not null unique
    references private.cash_settlement_obligations(id),
  provider_execution_id text not null,
  created_at timestamptz not null default clock_timestamp(),
  primary key (application_id, fill_sequence)
);

create trigger reject_unknown_resolution_v2_request_mutation
  before update or delete on private.unknown_execution_resolution_requests_v2
  for each row execute function private.reject_append_only_mutation();
create trigger reject_unknown_resolution_v2_fill_proposal_mutation
  before update or delete
  on private.unknown_execution_resolution_fill_proposals_v2
  for each row execute function private.reject_append_only_mutation();
create trigger reject_unknown_resolution_v2_review_mutation
  before update or delete on private.unknown_execution_resolution_reviews_v2
  for each row execute function private.reject_append_only_mutation();
create trigger reject_unknown_resolution_v2_application_mutation
  before update or delete
  on private.unknown_execution_resolution_applications_v2
  for each row execute function private.reject_append_only_mutation();
create trigger reject_unknown_resolution_v2_application_fill_mutation
  before update or delete
  on private.unknown_execution_resolution_application_fills_v2
  for each row execute function private.reject_append_only_mutation();

alter table private.unknown_execution_resolution_requests_v2 enable row level security;
alter table private.unknown_execution_resolution_fill_proposals_v2
  enable row level security;
alter table private.unknown_execution_resolution_reviews_v2 enable row level security;
alter table private.unknown_execution_resolution_work_items_v2 enable row level security;
alter table private.unknown_execution_resolution_applications_v2
  enable row level security;
alter table private.unknown_execution_resolution_application_fills_v2
  enable row level security;

create or replace function private.guard_unknown_resolution_v2_command_transition()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  phase text := coalesce(
    current_setting('msp.unknown_resolution_v2_phase', true), ''
  );
begin
  if old.command_type <> 'unknown_resolution'
     or old.requested_change->>'schema_version' is distinct from '2'
     or old.requested_change->>'command_type'
       is distinct from 'close_unknown_execution' then
    return new;
  end if;

  if new.requested_change is distinct from old.requested_change
     or new.command_sha256 is distinct from old.command_sha256
     or new.requester_user_id is distinct from old.requester_user_id
     or new.target_release_sha is distinct from old.target_release_sha
     or new.idempotency_key is distinct from old.idempotency_key
     or new.requested_at is distinct from old.requested_at
     or new.expires_at is distinct from old.expires_at then
    raise exception 'unknown_resolution_v2_command_manifest_is_immutable'
      using errcode = '42501';
  end if;

  if new.state is not distinct from old.state then
    return new;
  end if;
  if old.state = 'requested' and new.state in ('approved', 'rejected') then
    if phase <> 'review:' || old.id::text then
      raise exception 'unknown_resolution_v2_dedicated_review_required'
        using errcode = '42501';
    end if;
  elsif (
    (old.state = 'approved' and new.state = 'claimed')
    or (old.state = 'claimed' and new.state = 'claimed')
  ) then
    if phase <> 'claim:' || old.id::text then
      raise exception 'unknown_resolution_v2_dedicated_claim_required'
        using errcode = '42501';
    end if;
  elsif old.state = 'claimed' and new.state = 'applied' then
    if phase <> 'apply:' || old.id::text then
      raise exception 'unknown_resolution_v2_dedicated_apply_required'
        using errcode = '42501';
    end if;
  else
    raise exception 'unknown_resolution_v2_command_transition_invalid'
      using errcode = '23514';
  end if;
  return new;
end;
$$;

create trigger guard_unknown_resolution_v2_command_transition
  before update on private.operation_commands
  for each row execute function
    private.guard_unknown_resolution_v2_command_transition();

create or replace function private.validate_unknown_resolution_draft_v2(
  p_action text,
  p_payload jsonb
)
returns void
language plpgsql
immutable
security definer
set search_path = ''
as $$
declare
  fill_value jsonb;
  expected_fill_sequence integer := 1;
begin
  if jsonb_typeof(p_payload) <> 'object'
     or jsonb_typeof(p_payload->'schema_version') <> 'number'
     or (p_payload->>'schema_version')::integer <> 2
     or p_payload->>'command_type' is distinct from 'close_unknown_execution' then
    raise exception 'unknown_resolution_v2_draft_schema_invalid'
      using errcode = '22023';
  end if;

  if p_action = 'request' then
    perform private.assert_exact_json_keys(p_payload, array[
      'schema_version', 'request_id', 'environment', 'idempotency_key',
      'command_type', 'break_id', 'intent_id', 'unknown_observation_id',
      'provider_order_id', 'evidence_artifact_uri', 'evidence_sha256',
      'evidence_captured_at', 'reason_code', 'expected_break_state',
      'expected_break_revision', 'expected_reconciliation_state',
      'expected_cash_projection_version',
      'expected_position_projection_version',
      'expected_reservation_event_sequence', 'expected_control_epoch',
      'terminal_status', 'missing_fills', 'requested_at', 'expires_at'
    ]);
    if jsonb_typeof(p_payload->'expected_break_revision') <> 'number'
       or jsonb_typeof(p_payload->'expected_cash_projection_version') <> 'number'
       or jsonb_typeof(p_payload->'expected_reservation_event_sequence') <> 'number'
       or jsonb_typeof(p_payload->'expected_control_epoch') <> 'number'
       or jsonb_typeof(p_payload->'missing_fills') <> 'array'
       or jsonb_typeof(p_payload->'expected_position_projection_version')
         not in ('number', 'null')
       or jsonb_array_length(p_payload->'missing_fills') > 100 then
      raise exception 'unknown_resolution_v2_request_json_type_invalid'
        using errcode = '22023';
    end if;
    perform (p_payload->>'request_id')::uuid;
    perform (p_payload->>'break_id')::uuid;
    perform (p_payload->>'intent_id')::uuid;
    perform (p_payload->>'unknown_observation_id')::uuid;
    perform (p_payload->>'evidence_captured_at')::timestamptz;
    perform (p_payload->>'requested_at')::timestamptz;
    perform (p_payload->>'expires_at')::timestamptz;
    perform (p_payload->>'expected_break_revision')::bigint;
    perform (p_payload->>'expected_cash_projection_version')::bigint;
    if jsonb_typeof(p_payload->'expected_position_projection_version') = 'number' then
      perform (p_payload->>'expected_position_projection_version')::bigint;
    end if;
    perform (p_payload->>'expected_reservation_event_sequence')::integer;
    perform (p_payload->>'expected_control_epoch')::bigint;

    for fill_value in select value from jsonb_array_elements(p_payload->'missing_fills')
    loop
      perform private.assert_exact_json_keys(fill_value, array[
        'fill_sequence', 'provider_order_id', 'provider_execution_id',
        'quantity', 'price_krw', 'commission_krw', 'tax_krw',
        'filled_at', 'settlement_date', 'evidence_sha256'
      ]);
      if jsonb_typeof(fill_value->'fill_sequence') <> 'number'
         or jsonb_typeof(fill_value->'quantity') <> 'number'
         or jsonb_typeof(fill_value->'price_krw') <> 'number'
         or jsonb_typeof(fill_value->'commission_krw') <> 'number'
         or jsonb_typeof(fill_value->'tax_krw') <> 'number'
         or (fill_value->>'fill_sequence')::integer <> expected_fill_sequence
         or (fill_value->>'quantity')::bigint <= 0
         or (fill_value->>'price_krw')::bigint <= 0
         or (fill_value->>'commission_krw')::bigint < 0
         or (fill_value->>'tax_krw')::bigint < 0
         or nullif(btrim(fill_value->>'provider_order_id'), '') is null
         or nullif(btrim(fill_value->>'provider_execution_id'), '') is null
         or fill_value->>'evidence_sha256' !~ '^[0-9a-f]{64}$' then
        raise exception 'unknown_resolution_v2_fill_manifest_invalid'
          using errcode = '22023';
      end if;
      perform (fill_value->>'filled_at')::timestamptz;
      perform (fill_value->>'settlement_date')::date;
      expected_fill_sequence := expected_fill_sequence + 1;
    end loop;
  elsif p_action = 'review' then
    perform private.assert_exact_json_keys(p_payload, array[
      'schema_version', 'review_id', 'command_id', 'command_type',
      'reviewer_role', 'decision', 'reason_code',
      'expected_receipt_revision', 'expected_break_revision',
      'request_digest_sha256', 'evidence_sha256', 'reviewed_at'
    ]);
    if jsonb_typeof(p_payload->'expected_receipt_revision') <> 'number'
       or jsonb_typeof(p_payload->'expected_break_revision') <> 'number' then
      raise exception 'unknown_resolution_v2_review_json_type_invalid'
        using errcode = '22023';
    end if;
    perform (p_payload->>'review_id')::uuid;
    perform (p_payload->>'command_id')::uuid;
    perform (p_payload->>'expected_receipt_revision')::bigint;
    perform (p_payload->>'expected_break_revision')::bigint;
    perform (p_payload->>'reviewed_at')::timestamptz;
  else
    raise exception 'unknown_resolution_v2_action_invalid' using errcode = '22023';
  end if;
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'unknown_resolution_v2_draft_schema_invalid'
      using errcode = '22023';
end;
$$;

create or replace function private.issue_unknown_resolution_step_up_v2_impl(
  p_request_payload jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  action_name text;
  command_payload jsonb;
  command_hash text;
  result_row record;
  issued_time timestamptz;
begin
  perform private.assert_exact_json_keys(p_request_payload, array[
    'schema_version', 'bound_action', 'bound_command_type', 'command_payload'
  ]);
  if jsonb_typeof(p_request_payload->'schema_version') <> 'number'
     or (p_request_payload->>'schema_version')::integer <> 2
     or p_request_payload->>'bound_command_type'
       is distinct from 'close_unknown_execution' then
    raise exception 'unknown_resolution_v2_step_up_request_invalid'
      using errcode = '22023';
  end if;
  action_name := p_request_payload->>'bound_action';
  command_payload := p_request_payload->'command_payload';
  perform private.validate_unknown_resolution_draft_v2(
    action_name, command_payload
  );
  command_hash := private.compute_command_sha256(
    'operation_v2:' || action_name, command_payload
  );
  select * into result_row from private.issue_step_up_grant(command_hash);
  update private.step_up_grants
  set bound_action = action_name,
      bound_command_type = 'close_unknown_execution'
  where id = result_row.grant_id
  returning issued_at into issued_time;
  return jsonb_build_object(
    'schema_version', 2,
    'step_up_grant_id', result_row.grant_id,
    'command_hash', command_hash,
    'step_up_grant_issued_at', issued_time,
    'step_up_grant_expires_at', result_row.expires_at,
    'step_up_grant_one_time', true,
    'step_up_grant_consumed_at', null,
    'bound_action', action_name,
    'bound_command_type', 'close_unknown_execution'
  );
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'unknown_resolution_v2_step_up_request_invalid'
      using errcode = '22023';
end;
$$;

create or replace function private.consume_unknown_resolution_step_up_v2(
  p_payload jsonb,
  p_action text,
  p_action_at timestamptz,
  p_consumed_for text
)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid := (select auth.uid());
  expected_hash text;
  grant_id uuid;
begin
  expected_hash := private.compute_command_sha256(
    'operation_v2:' || p_action,
    private.command_draft_v1(p_payload)
  );
  grant_id := (p_payload->>'step_up_grant_id')::uuid;
  if p_payload->>'command_hash' is distinct from expected_hash
     or p_payload->>'bound_action' is distinct from p_action
     or p_payload->>'bound_command_type'
       is distinct from 'close_unknown_execution'
     or jsonb_typeof(p_payload->'step_up_grant_one_time') <> 'boolean'
     or (p_payload->'step_up_grant_one_time')::boolean is not true
     or jsonb_typeof(p_payload->'step_up_grant_consumed_at') <> 'null'
     or p_action_at < clock_timestamp() - interval '5 minutes'
     or p_action_at > clock_timestamp() + interval '30 seconds' then
    raise exception 'unknown_resolution_v2_step_up_binding_invalid'
      using errcode = '42501';
  end if;
  update private.step_up_grants
  set consumed_at = clock_timestamp(), consumed_for = p_consumed_for
  where id = grant_id
    and user_id = actor
    and command_sha256 = expected_hash
    and bound_action = p_action
    and bound_command_type = 'close_unknown_execution'
    and issued_at = (p_payload->>'step_up_grant_issued_at')::timestamptz
    and expires_at = (p_payload->>'step_up_grant_expires_at')::timestamptz
    and issued_at <= p_action_at + interval '30 seconds'
    and expires_at > p_action_at
    and expires_at > clock_timestamp()
    and session_binding_sha256 = private.current_session_binding_sha256()
    and consumed_at is null;
  if not found then
    raise exception 'unknown_resolution_v2_step_up_invalid_expired_or_consumed'
      using errcode = '42501';
  end if;
  return actor;
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'unknown_resolution_v2_step_up_binding_invalid'
      using errcode = '42501';
end;
$$;

create or replace function private.assert_unknown_resolution_fill_manifest_v2(
  p_intent_id uuid,
  p_unknown_observation_id uuid,
  p_provider_order_id text,
  p_terminal_status text,
  p_missing_fills jsonb,
  p_evidence_captured_at timestamptz
)
returns table (
  final_cumulative_quantity bigint,
  final_cumulative_gross_krw bigint,
  final_cumulative_commission_krw bigint,
  final_cumulative_tax_krw bigint,
  fill_count integer
)
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  intent_row private.order_intents%rowtype;
  unknown_row private.execution_observations%rowtype;
  attempt_row private.order_attempts%rowtype;
  binding_row private.provider_order_bindings%rowtype;
  fill_value jsonb;
  fill_sequence_value integer;
  quantity_value bigint;
  price_value bigint;
  commission_value bigint;
  tax_value bigint;
  filled_time timestamptz;
  settlement_date_value date;
  previous_fill_time timestamptz;
  gross_value bigint;
  expected_settlement_date date;
  settlement_days_value integer;
  buy_commission_rate_value numeric(18,12);
  sell_commission_rate_value numeric(18,12);
  sell_tax_rate_value numeric(18,12);
  calendar_id_value uuid;
  result_quantity bigint;
  result_gross bigint;
  result_commission bigint;
  result_tax bigint;
  result_count integer := 0;
begin
  if p_terminal_status not in ('filled', 'canceled', 'expired', 'rejected')
     or jsonb_typeof(p_missing_fills) <> 'array'
     or p_evidence_captured_at is null then
    raise exception 'unknown_resolution_v2_fill_manifest_values_invalid'
      using errcode = '22023';
  end if;

  select * into intent_row
  from private.order_intents
  where id = p_intent_id;
  select * into unknown_row
  from private.execution_observations
  where id = p_unknown_observation_id
    and intent_id = p_intent_id;
  select * into attempt_row
  from private.order_attempts
  where intent_id = p_intent_id;
  if intent_row.id is null
     or unknown_row.id is null
     or unknown_row.event_type <> 'unknown_requires_manual_check'
     or attempt_row.id is null then
    raise exception 'unknown_resolution_v2_execution_context_invalid'
      using errcode = '23514';
  end if;
  select * into binding_row
  from private.provider_order_bindings
  where attempt_id = attempt_row.id;
  if binding_row.attempt_id is null
     or binding_row.provider_order_id <> p_provider_order_id
     or unknown_row.provider_order_id <> p_provider_order_id then
    raise exception 'unknown_resolution_v2_provider_order_identity_mismatch'
      using errcode = '23514';
  end if;
  if exists (
    select 1
    from jsonb_array_elements(p_missing_fills) as proposed(value)
    group by proposed.value->>'provider_execution_id'
    having count(*) > 1
  ) then
    raise exception 'unknown_resolution_v2_provider_execution_identity_duplicated'
      using errcode = '23514';
  end if;

  result_quantity := unknown_row.cumulative_quantity;
  result_gross := unknown_row.cumulative_gross_krw;
  result_commission := unknown_row.cumulative_commission_krw;
  result_tax := unknown_row.cumulative_tax_krw;
  previous_fill_time := intent_row.decision_at;

  for fill_value in
    select value
    from jsonb_array_elements(p_missing_fills)
    order by (value->>'fill_sequence')::integer
  loop
    fill_sequence_value := (fill_value->>'fill_sequence')::integer;
    quantity_value := (fill_value->>'quantity')::bigint;
    price_value := (fill_value->>'price_krw')::bigint;
    commission_value := (fill_value->>'commission_krw')::bigint;
    tax_value := (fill_value->>'tax_krw')::bigint;
    filled_time := (fill_value->>'filled_at')::timestamptz;
    settlement_date_value := (fill_value->>'settlement_date')::date;
    gross_value := quantity_value * price_value;

    if fill_sequence_value <> result_count + 1
       or fill_value->>'provider_order_id' <> p_provider_order_id
       or filled_time < previous_fill_time
       or filled_time > p_evidence_captured_at
       or filled_time > intent_row.expires_at
       or (intent_row.side = 'buy' and price_value > intent_row.limit_price_krw)
       or (intent_row.side = 'sell' and price_value < intent_row.limit_price_krw)
       or result_quantity + quantity_value > intent_row.quantity
       or exists (
         select 1
         from private.fills as existing_fill
         where existing_fill.account_id = intent_row.account_id
           and existing_fill.broker = attempt_row.broker
           and existing_fill.provider_execution_id =
             fill_value->>'provider_execution_id'
       ) then
      raise exception 'unknown_resolution_v2_fill_sequence_or_identity_invalid'
        using errcode = '23514';
    end if;

    select
      schedule.settlement_days,
      schedule.buy_commission_rate,
      schedule.sell_commission_rate,
      schedule.sell_tax_rate,
      calendar.id
    into
      settlement_days_value,
      buy_commission_rate_value,
      sell_commission_rate_value,
      sell_tax_rate_value,
      calendar_id_value
    from private.execution_cost_schedules as schedule
    join private.control_evidence as schedule_evidence
      on schedule_evidence.id = schedule.evidence_id
     and schedule_evidence.artifact_sha256 =
       intent_row.cost_schedule_evidence_sha256
    join private.paper_execution_policies as policy
      on policy.account_id = intent_row.account_id
     and policy.policy_version = intent_row.execution_policy_version
     and policy.policy_sha256 = intent_row.execution_policy_sha256
     and policy.status = 'approved'
    join private.paper_execution_model_registry as model
      on model.environment = intent_row.environment
     and model.model_version = policy.parameters->>'execution_model_version'
     and model.status = 'approved'
    join private.market_calendars as calendar
      on calendar.id = model.market_calendar_id
     and calendar.status = 'approved'
     and calendar.calendar_version = policy.parameters->>'market_calendar_version'
     and calendar.calendar_sha256 = policy.parameters->>'market_calendar_sha256'
    where schedule.account_id = intent_row.account_id
      and schedule.schedule_version = intent_row.cost_schedule_version
      and schedule.status = 'approved'
      and schedule.effective_from <= filled_time
      and schedule.effective_until > filled_time
    limit 1;
    if settlement_days_value is null then
      raise exception 'unknown_resolution_v2_approved_cost_or_calendar_missing'
        using errcode = '23514';
    end if;
    select session.session_date into expected_settlement_date
    from private.market_calendar_sessions as session
    where session.calendar_id = calendar_id_value
      and session.session_date >= filled_time::date
      and session.is_open is true
    order by session.session_date
    offset settlement_days_value
    limit 1;
    if expected_settlement_date is null
       or settlement_date_value <> expected_settlement_date
       or (
         intent_row.side = 'buy'
         and (
           tax_value <> 0
           or commission_value <>
             ceil(gross_value * buy_commission_rate_value)::bigint
         )
       )
       or (
         intent_row.side = 'sell'
         and (
           commission_value <>
             ceil(gross_value * sell_commission_rate_value)::bigint
           or tax_value <> ceil(gross_value * sell_tax_rate_value)::bigint
         )
       ) then
      raise exception 'unknown_resolution_v2_cost_or_settlement_evidence_mismatch'
        using errcode = '23514';
    end if;

    result_quantity := result_quantity + quantity_value;
    result_gross := result_gross + gross_value;
    result_commission := result_commission + commission_value;
    result_tax := result_tax + tax_value;
    result_count := result_count + 1;
    previous_fill_time := filled_time;
  end loop;

  if (p_terminal_status = 'filled' and (
        result_count = 0 or result_quantity <> intent_row.quantity
      ))
     or (p_terminal_status in ('canceled', 'expired')
       and result_quantity >= intent_row.quantity)
     or (p_terminal_status = 'expired'
       and p_evidence_captured_at < intent_row.expires_at)
     or (p_terminal_status = 'rejected' and result_quantity <> 0) then
    raise exception 'unknown_resolution_v2_terminal_cumulative_state_invalid'
      using errcode = '23514';
  end if;
  if intent_row.environment = 'contract_test' and not exists (
    select 1
    from private.provider_contract_registry as contract
    where contract.provider = 'toss'
      and contract.qualification_environment = 'contract_test'
      and contract.execution_transport = 'local_contract_simulator'
      and contract.contract_version = intent_row.provider_contract_version
      and contract.openapi_sha256 = intent_row.provider_openapi_sha256
      and contract.status = 'approved'
      and contract.effective_from <= p_evidence_captured_at
      and contract.effective_until > p_evidence_captured_at
  ) then
    raise exception 'unknown_resolution_v2_provider_contract_not_approved'
      using errcode = '23514';
  end if;

  return query select
    result_quantity, result_gross, result_commission, result_tax, result_count;
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'unknown_resolution_v2_fill_manifest_values_invalid'
      using errcode = '22023';
end;
$$;

create or replace function private.unknown_resolution_v2_receipt(
  p_command_id uuid
)
returns jsonb
language sql
stable
security definer
set search_path = ''
as $$
  select jsonb_build_object(
    'schema_version', 2,
    'command_id', command.id,
    'break_id', request.break_id,
    'intent_id', request.intent_id,
    'state', command.state,
    'receipt_revision', command.revision,
    'break_revision', reconciliation_break.revision,
    'request_digest_sha256', request.payload_sha256,
    'review_digest_sha256', review.payload_sha256,
    'terminal_status', request.terminal_status,
    'claim_token', work.claim_token,
    'work_revision', work.revision,
    'application_id', application.id,
    'application_sha256', application.application_sha256,
    'accounting_mutation_allowed',
      command.state in ('approved', 'claimed', 'applied'),
    'resolution_complete', command.state = 'applied',
    'inserted', false
  )
  from private.operation_commands as command
  join private.unknown_execution_resolution_requests_v2 as request
    on request.command_id = command.id
  join private.reconciliation_breaks as reconciliation_break
    on reconciliation_break.id = request.break_id
  left join private.unknown_execution_resolution_reviews_v2 as review
    on review.command_id = command.id
  left join private.unknown_execution_resolution_work_items_v2 as work
    on work.command_id = command.id
  left join private.unknown_execution_resolution_applications_v2 as application
    on application.command_id = command.id
  where command.id = p_command_id;
$$;

create or replace function private.request_unknown_resolution_v2_impl(
  p_request_payload jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid;
  draft_payload jsonb;
  request_id_value uuid;
  break_id_value uuid;
  intent_id_value uuid;
  unknown_observation_id_value uuid;
  requested_time timestamptz;
  expiry_time timestamptz;
  evidence_captured_time timestamptz;
  expected_break_revision_value bigint;
  expected_cash_version bigint;
  expected_position_version bigint;
  expected_reservation_sequence integer;
  expected_control_epoch_value bigint;
  command_hash text;
  break_row private.reconciliation_breaks%rowtype;
  intent_row private.order_intents%rowtype;
  unknown_row private.execution_observations%rowtype;
  control_row private.execution_controls%rowtype;
  cash_row private.cash_balance_projection%rowtype;
  position_row private.position_projection%rowtype;
  reservation_row private.order_reservations%rowtype;
  reconciliation_row private.execution_reconciliation_state%rowtype;
  latest_reservation_sequence integer;
  position_exists boolean := false;
  existing_request private.unknown_execution_resolution_requests_v2%rowtype;
  existing_command private.operation_commands%rowtype;
  manifest_result record;
  fill_value jsonb;
  proposal_hash text;
begin
  perform private.assert_exact_json_keys(p_request_payload, array[
    'schema_version', 'request_id', 'environment', 'idempotency_key',
    'command_type', 'break_id', 'intent_id', 'unknown_observation_id',
    'provider_order_id', 'evidence_artifact_uri', 'evidence_sha256',
    'evidence_captured_at', 'reason_code', 'expected_break_state',
    'expected_break_revision', 'expected_reconciliation_state',
    'expected_cash_projection_version',
    'expected_position_projection_version',
    'expected_reservation_event_sequence', 'expected_control_epoch',
    'terminal_status', 'missing_fills', 'requested_at', 'expires_at',
    'step_up_grant_id', 'command_hash', 'step_up_grant_issued_at',
    'step_up_grant_expires_at', 'step_up_grant_one_time',
    'step_up_grant_consumed_at', 'bound_action', 'bound_command_type'
  ]);
  draft_payload := private.command_draft_v1(p_request_payload);
  perform private.validate_unknown_resolution_draft_v2(
    'request', draft_payload
  );
  actor := private.require_human_roles(array['operator'], true);
  perform private.require_recent_aal2();
  if private.has_active_role(actor, 'platform_admin') then
    raise exception 'platform_admin_trading_action_forbidden'
      using errcode = '42501';
  end if;

  request_id_value := (p_request_payload->>'request_id')::uuid;
  break_id_value := (p_request_payload->>'break_id')::uuid;
  intent_id_value := (p_request_payload->>'intent_id')::uuid;
  unknown_observation_id_value :=
    (p_request_payload->>'unknown_observation_id')::uuid;
  requested_time := (p_request_payload->>'requested_at')::timestamptz;
  expiry_time := (p_request_payload->>'expires_at')::timestamptz;
  evidence_captured_time :=
    (p_request_payload->>'evidence_captured_at')::timestamptz;
  expected_break_revision_value :=
    (p_request_payload->>'expected_break_revision')::bigint;
  expected_cash_version :=
    (p_request_payload->>'expected_cash_projection_version')::bigint;
  expected_position_version := case
    when jsonb_typeof(p_request_payload->'expected_position_projection_version')
      = 'number'
    then (p_request_payload->>'expected_position_projection_version')::bigint
    else null
  end;
  expected_reservation_sequence :=
    (p_request_payload->>'expected_reservation_event_sequence')::integer;
  expected_control_epoch_value :=
    (p_request_payload->>'expected_control_epoch')::bigint;

  if p_request_payload->>'environment' not in ('paper', 'contract_test')
     or p_request_payload->>'command_type' <> 'close_unknown_execution'
     or p_request_payload->>'reason_code' <> 'accounting_closure_requested'
     or p_request_payload->>'expected_break_state' <> 'open'
     or p_request_payload->>'expected_reconciliation_state' <> 'manual'
     or p_request_payload->>'terminal_status'
       not in ('filled', 'canceled', 'expired', 'rejected')
     or p_request_payload->>'evidence_sha256' !~ '^[0-9a-f]{64}$'
     or nullif(btrim(p_request_payload->>'provider_order_id'), '') is null
     or length(p_request_payload->>'provider_order_id') > 200
     or expected_break_revision_value < 0
     or expected_cash_version < 0
     or expected_position_version < 0
     or expected_reservation_sequence <= 0
     or expected_control_epoch_value <= 0
     or evidence_captured_time > requested_time
     or evidence_captured_time < requested_time - interval '30 days'
     or requested_time < clock_timestamp() - interval '5 minutes'
     or requested_time > clock_timestamp() + interval '30 seconds'
     or expiry_time <= requested_time
     or expiry_time > requested_time + interval '24 hours'
     or p_request_payload->>'idempotency_key'
       !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
     or not (
       (
         p_request_payload->>'evidence_artifact_uri'
           ~ '^https://[^/?#[:space:]]+/'
         and p_request_payload->>'evidence_artifact_uri' !~ '[?#]'
       )
       or p_request_payload->>'evidence_artifact_uri' =
         'urn:sha256:' || (p_request_payload->>'evidence_sha256')
     ) then
    raise exception 'unknown_resolution_v2_request_values_invalid'
      using errcode = '22023';
  end if;
  command_hash := private.compute_command_sha256(
    'operation_v2:request', draft_payload
  );
  if p_request_payload->>'command_hash' is distinct from command_hash then
    raise exception 'unknown_resolution_v2_request_hash_mismatch'
      using errcode = '42501';
  end if;

  select * into existing_request
  from private.unknown_execution_resolution_requests_v2 as request
  where request.idempotency_key = p_request_payload->>'idempotency_key';
  if found then
    if existing_request.id <> request_id_value
       or existing_request.payload_sha256 <> command_hash
       or existing_request.requester_user_id <> actor then
      raise exception 'unknown_resolution_v2_request_idempotency_conflict'
        using errcode = '23505';
    end if;
    return private.unknown_resolution_v2_receipt(existing_request.command_id);
  end if;
  select * into existing_command
  from private.operation_commands as command
  where command.idempotency_key = p_request_payload->>'idempotency_key';
  if found then
    raise exception 'unknown_resolution_v2_request_idempotency_conflict'
      using errcode = '23505';
  end if;

  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(intent_id_value::text, 68491723011702::bigint)
  );
  select * into break_row
  from private.reconciliation_breaks
  where id = break_id_value
  for update;
  select * into intent_row
  from private.order_intents
  where id = intent_id_value
  for update;
  select * into unknown_row
  from private.execution_observations
  where id = unknown_observation_id_value
  for update;
  if break_row.id is null
     or break_row.break_type <> 'execution'
     or break_row.state <> 'open'
     or break_row.revision <> expected_break_revision_value
     or break_row.resolution_command_id is not null
     or intent_row.id is null
     or intent_row.account_id <> break_row.account_id
     or intent_row.environment <> p_request_payload->>'environment'
     or unknown_row.id is null
     or unknown_row.intent_id <> intent_id_value
     or unknown_row.event_type <> 'unknown_requires_manual_check'
     or not exists (
       select 1
       from private.order_events as event
       where event.intent_id = intent_id_value
         and event.observation_id = unknown_observation_id_value
         and event.event_type = 'manual_check_quarantined'
         and event.event_summary->>'reconciliation_break_id' = break_id_value::text
     )
     or exists (
       select 1
       from private.execution_observations as later_observation
       where later_observation.intent_id = intent_id_value
         and later_observation.sequence > unknown_row.sequence
     ) then
    raise exception 'unknown_resolution_v2_break_or_observation_stale'
      using errcode = '40001';
  end if;

  select * into control_row
  from private.execution_controls
  where account_id = intent_row.account_id
  for update;
  select * into cash_row
  from private.cash_balance_projection
  where account_id = intent_row.account_id
  for update;
  select * into position_row
  from private.position_projection
  where account_id = intent_row.account_id
    and symbol = intent_row.symbol
  for update;
  position_exists := found;
  select * into reservation_row
  from private.order_reservations
  where intent_id = intent_id_value
  for update;
  select * into reconciliation_row
  from private.execution_reconciliation_state
  where intent_id = intent_id_value
  for update;
  select event_sequence into latest_reservation_sequence
  from private.reservation_events
  where reservation_id = reservation_row.id
  order by event_sequence desc
  limit 1
  for update;

  if control_row.account_id is null
     or control_row.execution_enabled is true
     or control_row.control_epoch <> expected_control_epoch_value
     or cash_row.account_id is null
     or cash_row.projection_version <> expected_cash_version
     or reservation_row.id is null
     or latest_reservation_sequence <> expected_reservation_sequence
     or reconciliation_row.intent_id is null
     or reconciliation_row.state <> 'manual'
     or (
       position_exists
       and position_row.projection_version
         is distinct from expected_position_version
     )
     or (
       not position_exists
       and expected_position_version is not null
     ) then
    raise exception 'unknown_resolution_v2_control_or_projection_stale'
      using errcode = '40001';
  end if;

  select * into manifest_result
  from private.assert_unknown_resolution_fill_manifest_v2(
    intent_id_value,
    unknown_observation_id_value,
    p_request_payload->>'provider_order_id',
    p_request_payload->>'terminal_status',
    p_request_payload->'missing_fills',
    evidence_captured_time
  );

  perform private.consume_unknown_resolution_step_up_v2(
    p_request_payload,
    'request',
    requested_time,
    'request_unknown_resolution_v2'
  );
  insert into private.operation_commands (
    id, command_type, state, requested_change, command_sha256, revision,
    target_release_sha, requester_user_id, requested_at, expires_at,
    idempotency_key
  ) values (
    request_id_value, 'unknown_resolution', 'requested', draft_payload,
    command_hash, 0, intent_row.release_sha, actor, requested_time,
    expiry_time, p_request_payload->>'idempotency_key'
  );
  insert into private.unknown_execution_resolution_requests_v2 (
    id, command_id, break_id, intent_id, unknown_observation_id,
    account_id, environment, provider_order_id, terminal_status,
    evidence_artifact_uri, evidence_sha256, evidence_captured_at,
    expected_break_revision, expected_cash_projection_version,
    expected_position_projection_version,
    expected_reservation_event_sequence, expected_control_epoch,
    baseline_sequence, baseline_cumulative_quantity,
    baseline_cumulative_gross_krw, baseline_cumulative_commission_krw,
    baseline_cumulative_tax_krw, payload, payload_sha256,
    requester_user_id, requested_at, expires_at, idempotency_key
  ) values (
    request_id_value, request_id_value, break_id_value, intent_id_value,
    unknown_observation_id_value, intent_row.account_id,
    intent_row.environment, p_request_payload->>'provider_order_id',
    p_request_payload->>'terminal_status',
    p_request_payload->>'evidence_artifact_uri',
    p_request_payload->>'evidence_sha256', evidence_captured_time,
    expected_break_revision_value, expected_cash_version,
    expected_position_version, expected_reservation_sequence,
    expected_control_epoch_value, unknown_row.sequence,
    unknown_row.cumulative_quantity, unknown_row.cumulative_gross_krw,
    unknown_row.cumulative_commission_krw,
    unknown_row.cumulative_tax_krw, draft_payload, command_hash,
    actor, requested_time, expiry_time,
    p_request_payload->>'idempotency_key'
  );
  for fill_value in
    select value
    from jsonb_array_elements(p_request_payload->'missing_fills')
    order by (value->>'fill_sequence')::integer
  loop
    proposal_hash := private.compute_command_sha256(
      'unknown_resolution_v2:fill', fill_value
    );
    insert into private.unknown_execution_resolution_fill_proposals_v2 (
      request_id, fill_sequence, provider_order_id, provider_execution_id,
      quantity, price_krw, commission_krw, tax_krw, filled_at,
      settlement_date, evidence_sha256, proposal_sha256
    ) values (
      request_id_value, (fill_value->>'fill_sequence')::integer,
      fill_value->>'provider_order_id', fill_value->>'provider_execution_id',
      (fill_value->>'quantity')::bigint, (fill_value->>'price_krw')::bigint,
      (fill_value->>'commission_krw')::bigint,
      (fill_value->>'tax_krw')::bigint,
      (fill_value->>'filled_at')::timestamptz,
      (fill_value->>'settlement_date')::date,
      fill_value->>'evidence_sha256', proposal_hash
    );
  end loop;
  update private.reconciliation_breaks
  set state = 'resolution_requested',
      resolution_command_id = request_id_value,
      revision = revision + 1
  where id = break_id_value
    and state = 'open'
    and revision = expected_break_revision_value;
  if not found then
    raise exception 'unknown_resolution_v2_break_or_observation_stale'
      using errcode = '40001';
  end if;
  insert into private.operation_command_events (
    command_id, event_type, actor_type, actor_user_id,
    event_summary, occurred_at
  ) values (
    request_id_value, 'requested', 'human', actor,
    jsonb_build_object(
      'schema_version', 2,
      'break_id', break_id_value,
      'intent_id', intent_id_value,
      'unknown_observation_id', unknown_observation_id_value,
      'request_digest_sha256', command_hash,
      'evidence_sha256', p_request_payload->>'evidence_sha256',
      'missing_fill_count', manifest_result.fill_count,
      'accounting_mutation_allowed', false
    ),
    requested_time
  );
  perform private.write_audit_event(
    'human', actor, 'operator', null, null, intent_row.release_sha,
    'unknown_execution_accounting_closure_requested',
    'reconciliation_break', break_id_value::text,
    intent_row.correlation_id, request_id_value,
    'accounting_closure_requested', null,
    array[
      'state', 'revision', 'request_digest_sha256',
      'evidence_sha256', 'missing_fill_count'
    ],
    null, command_hash, null
  );
  return private.unknown_resolution_v2_receipt(request_id_value)
    || jsonb_build_object('inserted', true);
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'unknown_resolution_v2_request_values_invalid'
      using errcode = '22023';
end;
$$;

create or replace function private.review_unknown_resolution_v2_impl(
  p_review_payload jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid;
  draft_payload jsonb;
  review_id_value uuid;
  command_id_value uuid;
  expected_command_revision bigint;
  expected_break_revision_value bigint;
  reviewed_time timestamptz;
  review_hash text;
  decision_value text;
  command_row private.operation_commands%rowtype;
  request_row private.unknown_execution_resolution_requests_v2%rowtype;
  existing_review private.unknown_execution_resolution_reviews_v2%rowtype;
  break_row private.reconciliation_breaks%rowtype;
  intent_row private.order_intents%rowtype;
  control_row private.execution_controls%rowtype;
  evidence_id_value uuid;
  step_up_id uuid;
  next_state text;
begin
  perform private.assert_exact_json_keys(p_review_payload, array[
    'schema_version', 'review_id', 'command_id', 'command_type',
    'reviewer_role', 'decision', 'reason_code',
    'expected_receipt_revision', 'expected_break_revision',
    'request_digest_sha256', 'evidence_sha256', 'reviewed_at',
    'step_up_grant_id', 'command_hash', 'step_up_grant_issued_at',
    'step_up_grant_expires_at', 'step_up_grant_one_time',
    'step_up_grant_consumed_at', 'bound_action', 'bound_command_type'
  ]);
  draft_payload := private.command_draft_v1(p_review_payload);
  perform private.validate_unknown_resolution_draft_v2(
    'review', draft_payload
  );
  actor := private.require_human_roles(array['risk_approver'], true);
  perform private.require_recent_aal2();
  if private.has_active_role(actor, 'platform_admin') then
    raise exception 'platform_admin_trading_action_forbidden'
      using errcode = '42501';
  end if;

  review_id_value := (p_review_payload->>'review_id')::uuid;
  command_id_value := (p_review_payload->>'command_id')::uuid;
  expected_command_revision :=
    (p_review_payload->>'expected_receipt_revision')::bigint;
  expected_break_revision_value :=
    (p_review_payload->>'expected_break_revision')::bigint;
  reviewed_time := (p_review_payload->>'reviewed_at')::timestamptz;
  decision_value := p_review_payload->>'decision';
  if p_review_payload->>'command_type' <> 'close_unknown_execution'
     or p_review_payload->>'reviewer_role' <> 'risk_approver'
     or decision_value not in ('approve', 'reject')
     or p_review_payload->>'request_digest_sha256' !~ '^[0-9a-f]{64}$'
     or p_review_payload->>'evidence_sha256' !~ '^[0-9a-f]{64}$'
     or (
       decision_value = 'approve'
       and p_review_payload->>'reason_code' <> 'evidence_sufficient'
     )
     or (
       decision_value = 'reject'
       and p_review_payload->>'reason_code' not in (
         'evidence_incomplete', 'accounting_adjustment_required'
       )
     )
     or expected_command_revision < 0
     or expected_break_revision_value < 0
     or reviewed_time < clock_timestamp() - interval '5 minutes'
     or reviewed_time > clock_timestamp() + interval '30 seconds' then
    raise exception 'unknown_resolution_v2_review_values_invalid'
      using errcode = '22023';
  end if;
  review_hash := private.compute_command_sha256(
    'operation_v2:review', draft_payload
  );
  if p_review_payload->>'command_hash' is distinct from review_hash then
    raise exception 'unknown_resolution_v2_review_hash_mismatch'
      using errcode = '42501';
  end if;

  select * into existing_review
  from private.unknown_execution_resolution_reviews_v2 as review
  where review.command_id = command_id_value;
  if found then
    if existing_review.id <> review_id_value
       or existing_review.payload_sha256 <> review_hash
       or existing_review.reviewer_user_id <> actor then
      raise exception 'unknown_resolution_v2_review_idempotency_conflict'
        using errcode = '23505';
    end if;
    return private.unknown_resolution_v2_receipt(command_id_value);
  end if;

  select * into request_row
  from private.unknown_execution_resolution_requests_v2
  where command_id = command_id_value;
  if not found then
    raise exception 'unknown_resolution_v2_request_not_found'
      using errcode = 'P0002';
  end if;
  if request_row.requester_user_id = actor then
    raise exception 'unknown_resolution_v2_self_review_forbidden'
      using errcode = '42501';
  end if;
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      request_row.intent_id::text, 68491723011702::bigint
    )
  );
  select * into command_row
  from private.operation_commands
  where id = command_id_value
  for update;
  select * into break_row
  from private.reconciliation_breaks
  where id = request_row.break_id
  for update;
  select * into intent_row
  from private.order_intents
  where id = request_row.intent_id
  for update;
  select * into control_row
  from private.execution_controls
  where account_id = request_row.account_id
  for update;
  if command_row.id is null
     or command_row.command_type <> 'unknown_resolution'
     or command_row.requested_change->>'schema_version' <> '2'
     or command_row.state <> 'requested'
     or command_row.revision <> expected_command_revision
     or command_row.expires_at <= reviewed_time
     or command_row.requester_user_id = actor
     or request_row.requester_user_id = actor
     or request_row.payload_sha256 <>
       p_review_payload->>'request_digest_sha256'
     or request_row.evidence_sha256 <> p_review_payload->>'evidence_sha256'
     or break_row.id is null
     or break_row.state <> 'resolution_requested'
     or break_row.resolution_command_id <> command_id_value
     or break_row.revision <> expected_break_revision_value
     or intent_row.id is null
     or control_row.account_id is null
     or control_row.execution_enabled is true
     or control_row.control_epoch <> request_row.expected_control_epoch
     or not exists (
       select 1
       from private.execution_reconciliation_state as reconciliation
       where reconciliation.intent_id = request_row.intent_id
         and reconciliation.state = 'manual'
     )
     or exists (
       select 1
       from private.execution_observations as later_observation
       where later_observation.intent_id = request_row.intent_id
         and later_observation.sequence > request_row.baseline_sequence
     ) then
    raise exception 'unknown_resolution_v2_review_target_stale'
      using errcode = '40001';
  end if;

  perform private.consume_unknown_resolution_step_up_v2(
    p_review_payload,
    'review',
    reviewed_time,
    'review_unknown_resolution_v2'
  );
  step_up_id := (p_review_payload->>'step_up_grant_id')::uuid;
  next_state := case
    when decision_value = 'approve' then 'approved'
    else 'rejected'
  end;
  if decision_value = 'approve' then
    evidence_id_value := md5(
      'unknown-execution-resolution-v2:' || command_id_value::text
    )::uuid;
    insert into private.control_evidence (
      id, evidence_type, environment, artifact_uri, artifact_sha256,
      captured_at, verified_at, verified_by, metadata_summary
    ) values (
      evidence_id_value, 'unknown_execution_resolution',
      request_row.environment, request_row.evidence_artifact_uri,
      request_row.evidence_sha256, request_row.evidence_captured_at,
      reviewed_time, actor,
      jsonb_build_object(
        'schema_version', 2,
        'request_id', request_row.id,
        'break_id', request_row.break_id,
        'intent_id', request_row.intent_id,
        'request_digest_sha256', request_row.payload_sha256,
        'review_digest_sha256', review_hash
      )
    );
  end if;

  perform set_config(
    'msp.unknown_resolution_v2_phase',
    'review:' || command_id_value::text,
    true
  );
  update private.operation_commands
  set state = next_state,
      reviewer_user_id = actor,
      reviewed_at = reviewed_time,
      review_reason = p_review_payload->>'reason_code',
      evidence_id = evidence_id_value,
      revision = revision + 1
  where id = command_id_value
    and state = 'requested'
    and revision = expected_command_revision;
  if not found then
    raise exception 'unknown_resolution_v2_review_target_stale'
      using errcode = '40001';
  end if;
  insert into private.operation_command_reviews (
    id, command_id, reviewer_user_id, reviewer_role, decision, reason_code,
    step_up_grant_id, reviewed_at, evidence_sha256, request_digest_sha256
  ) values (
    review_id_value, command_id_value, actor, 'risk_approver',
    case when decision_value = 'approve' then 'approved' else 'rejected' end,
    p_review_payload->>'reason_code', step_up_id, reviewed_time,
    request_row.evidence_sha256, request_row.payload_sha256
  );
  insert into private.unknown_execution_resolution_reviews_v2 (
    id, request_id, command_id, decision, reason_code,
    expected_request_payload_sha256, evidence_sha256, payload,
    payload_sha256, reviewer_user_id, step_up_grant_id, reviewed_at
  ) values (
    review_id_value, command_id_value, command_id_value,
    case when decision_value = 'approve' then 'approved' else 'rejected' end,
    p_review_payload->>'reason_code', request_row.payload_sha256,
    request_row.evidence_sha256, draft_payload, review_hash, actor,
    step_up_id, reviewed_time
  );
  update private.reconciliation_breaks
  set state = case
        when decision_value = 'approve' then 'resolution_requested'
        else 'open'
      end,
      resolution_command_id = case
        when decision_value = 'approve' then command_id_value
        else null
      end,
      revision = revision + 1
  where id = request_row.break_id
    and state = 'resolution_requested'
    and resolution_command_id = command_id_value
    and revision = expected_break_revision_value;
  if not found then
    raise exception 'unknown_resolution_v2_review_target_stale'
      using errcode = '40001';
  end if;
  if decision_value = 'approve' then
    insert into private.unknown_execution_resolution_work_items_v2 (
      command_id, request_id, review_id, state, revision,
      approved_break_revision, updated_at
    ) values (
      command_id_value, command_id_value, review_id_value,
      'approved', 0, expected_break_revision_value + 1, reviewed_time
    );
  end if;
  insert into private.operation_command_events (
    command_id, event_type, actor_type, actor_user_id,
    event_summary, occurred_at
  ) values (
    command_id_value, next_state, 'human', actor,
    jsonb_build_object(
      'schema_version', 2,
      'review_id', review_id_value,
      'break_id', request_row.break_id,
      'request_digest_sha256', request_row.payload_sha256,
      'review_digest_sha256', review_hash,
      'evidence_sha256', request_row.evidence_sha256,
      'accounting_mutation_allowed', decision_value = 'approve',
      'resolution_complete', false
    ),
    reviewed_time
  );
  perform private.write_audit_event(
    'human', actor, 'risk_approver', null, null, intent_row.release_sha,
    'unknown_execution_accounting_closure_reviewed',
    'reconciliation_break', request_row.break_id::text,
    intent_row.correlation_id, command_id_value,
    p_review_payload->>'reason_code', null,
    array[
      'command_state', 'break_state', 'request_digest_sha256',
      'review_digest_sha256', 'evidence_sha256'
    ],
    request_row.payload_sha256, review_hash, evidence_id_value
  );
  return private.unknown_resolution_v2_receipt(command_id_value)
    || jsonb_build_object('inserted', true);
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'unknown_resolution_v2_review_values_invalid'
      using errcode = '22023';
end;
$$;

create or replace function private.list_unknown_resolution_v2_impl(
  p_account_id text,
  p_holder_id text,
  p_release_sha text,
  p_fencing_token bigint,
  p_limit integer,
  p_now timestamptz
)
returns table (
  command_id uuid,
  request_id uuid,
  review_id uuid,
  break_id uuid,
  intent_id uuid,
  terminal_status text,
  request_payload_sha256 text,
  review_payload_sha256 text,
  command_revision bigint,
  work_revision bigint,
  work_state text,
  claim_token uuid,
  claim_expires_at timestamptz,
  expected_control_epoch bigint
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  authorization_time timestamptz := clock_timestamp();
begin
  perform private.require_service_role();
  if nullif(btrim(p_account_id), '') is null
     or nullif(btrim(p_holder_id), '') is null
     or p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_fencing_token <= 0
     or p_limit < 1 or p_limit > 100
     or p_now < authorization_time - interval '5 minutes'
     or p_now > authorization_time + interval '30 seconds' then
    raise exception 'unknown_resolution_v2_list_parameters_invalid'
      using errcode = '22023';
  end if;
  if not exists (
    select 1
    from private.worker_leases as lease
    where lease.account_id = p_account_id
      and lease.holder_id = p_holder_id
      and lease.release_sha = p_release_sha
      and lease.fencing_token = p_fencing_token
      and lease.expires_at > authorization_time
  ) then
    raise exception 'unknown_resolution_v2_worker_lease_stale'
      using errcode = '40001';
  end if;
  return query
  select
    command.id,
    request.id,
    review.id,
    request.break_id,
    request.intent_id,
    request.terminal_status,
    request.payload_sha256,
    review.payload_sha256,
    command.revision,
    work.revision,
    work.state,
    case
      when work.state = 'claimed'
       and work.claim_owner = p_holder_id
       and work.claim_release_sha = p_release_sha
       and work.claim_fencing_token = p_fencing_token
       and work.claim_expires_at > authorization_time
      then work.claim_token
      else null
    end,
    work.claim_expires_at,
    request.expected_control_epoch
  from private.unknown_execution_resolution_work_items_v2 as work
  join private.unknown_execution_resolution_requests_v2 as request
    on request.id = work.request_id
  join private.unknown_execution_resolution_reviews_v2 as review
    on review.id = work.review_id and review.decision = 'approved'
  join private.operation_commands as command
    on command.id = work.command_id
  join private.execution_controls as control
    on control.account_id = request.account_id
  where request.account_id = p_account_id
    and request.environment in ('paper', 'contract_test')
    and request.expires_at > authorization_time
    and command.target_release_sha = p_release_sha
    and control.execution_enabled is false
    and control.control_epoch = request.expected_control_epoch
    and (
      (work.state = 'approved' and command.state = 'approved')
      or (work.state = 'claimed' and command.state = 'claimed')
    )
  order by request.requested_at, request.id
  limit p_limit;
end;
$$;

create or replace function private.claim_unknown_resolution_v2_impl(
  p_command_id uuid,
  p_holder_id text,
  p_release_sha text,
  p_fencing_token bigint,
  p_expected_command_revision bigint,
  p_expected_work_revision bigint,
  p_now timestamptz
)
returns table (
  command_id uuid,
  claim_token uuid,
  command_revision bigint,
  work_revision bigint,
  claim_expires_at timestamptz
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  authorization_time timestamptz := clock_timestamp();
  command_row private.operation_commands%rowtype;
  request_row private.unknown_execution_resolution_requests_v2%rowtype;
  work_row private.unknown_execution_resolution_work_items_v2%rowtype;
  control_row private.execution_controls%rowtype;
  lease_row private.worker_leases%rowtype;
  new_claim_token uuid;
  new_claim_expiry timestamptz;
begin
  perform private.require_service_role();
  if p_command_id is null
     or nullif(btrim(p_holder_id), '') is null
     or p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_fencing_token <= 0
     or p_expected_command_revision < 0
     or p_expected_work_revision < 0
     or p_now < authorization_time - interval '5 minutes'
     or p_now > authorization_time + interval '30 seconds' then
    raise exception 'unknown_resolution_v2_claim_parameters_invalid'
      using errcode = '22023';
  end if;
  select * into request_row
  from private.unknown_execution_resolution_requests_v2 as request
  where request.command_id = p_command_id;
  if not found then
    raise exception 'unknown_resolution_v2_request_not_found'
      using errcode = 'P0002';
  end if;
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      request_row.intent_id::text, 68491723011702::bigint
    )
  );
  select * into command_row
  from private.operation_commands as command
  where command.id = p_command_id
  for update;
  select * into work_row
  from private.unknown_execution_resolution_work_items_v2 as work
  where work.command_id = p_command_id
  for update;
  select * into control_row
  from private.execution_controls
  where account_id = request_row.account_id
  for update;
  select * into lease_row
  from private.worker_leases
  where account_id = request_row.account_id
  for update;

  if command_row.id is null
     or work_row.command_id is null
     or command_row.revision <> p_expected_command_revision
     or work_row.revision <> p_expected_work_revision
     or command_row.expires_at <= authorization_time
     or command_row.target_release_sha <> p_release_sha
     or control_row.account_id is null
     or control_row.execution_enabled is true
     or control_row.control_epoch <> request_row.expected_control_epoch
     or lease_row.account_id is null
     or lease_row.holder_id <> p_holder_id
     or lease_row.release_sha <> p_release_sha
     or lease_row.fencing_token <> p_fencing_token
     or lease_row.expires_at <= authorization_time
     or not (
       (
         command_row.state = 'approved'
         and work_row.state = 'approved'
       )
       or (
         command_row.state = 'claimed'
         and work_row.state = 'claimed'
         and (
           work_row.claim_expires_at <= authorization_time
           or work_row.claim_owner <> p_holder_id
           or work_row.claim_release_sha <> p_release_sha
           or work_row.claim_fencing_token <> p_fencing_token
         )
       )
     ) then
    raise exception 'unknown_resolution_v2_claim_stale_or_not_claimable'
      using errcode = '40001';
  end if;
  new_claim_token := gen_random_uuid();
  new_claim_expiry := p_now + interval '30 seconds';
  update private.unknown_execution_resolution_work_items_v2 as work
  set state = 'claimed',
      revision = revision + 1,
      claim_token = new_claim_token,
      claim_owner = p_holder_id,
      claim_release_sha = p_release_sha,
      claim_fencing_token = p_fencing_token,
      claimed_at = p_now,
      claim_expires_at = new_claim_expiry,
      applied_at = null,
      updated_at = p_now
  where work.command_id = p_command_id
    and work.revision = p_expected_work_revision
  returning * into work_row;
  if not found then
    raise exception 'unknown_resolution_v2_claim_stale_or_not_claimable'
      using errcode = '40001';
  end if;
  perform set_config(
    'msp.unknown_resolution_v2_phase',
    'claim:' || p_command_id::text,
    true
  );
  update private.operation_commands as command
  set state = 'claimed',
      claimed_by_service = p_holder_id,
      claimed_at = p_now,
      claim_expires_at = new_claim_expiry,
      revision = revision + 1
  where command.id = p_command_id
    and command.revision = p_expected_command_revision
  returning * into command_row;
  if not found then
    raise exception 'unknown_resolution_v2_claim_stale_or_not_claimable'
      using errcode = '40001';
  end if;
  insert into private.operation_command_events (
    command_id, event_type, actor_type, service_principal,
    event_summary, occurred_at
  ) values (
    p_command_id, 'claimed', 'worker', p_holder_id,
    jsonb_build_object(
      'schema_version', 2,
      'claim_token', new_claim_token,
      'release_sha', p_release_sha,
      'fencing_token', p_fencing_token,
      'control_epoch', request_row.expected_control_epoch,
      'work_revision', work_row.revision
    ),
    p_now
  );
  perform private.write_audit_event(
    'worker', null, null, 'trading_worker', p_holder_id, p_release_sha,
    'unknown_execution_accounting_closure_claimed',
    'operation_command', p_command_id::text,
    (select correlation_id from private.order_intents
      where id = request_row.intent_id),
    p_command_id, 'dedicated_claim', null,
    array[
      'state', 'claim_token', 'release_sha', 'fencing_token',
      'control_epoch', 'revision'
    ],
    request_row.payload_sha256,
    private.compute_command_sha256(
      'unknown_resolution_v2:claim',
      jsonb_build_object(
        'claim_token', new_claim_token,
        'release_sha', p_release_sha,
        'fencing_token', p_fencing_token,
        'command_revision', command_row.revision,
        'work_revision', work_row.revision
      )
    ),
    command_row.evidence_id
  );
  return query select
    p_command_id, new_claim_token, command_row.revision,
    work_row.revision, new_claim_expiry;
end;
$$;

create or replace function private.apply_unknown_resolution_v2_impl(
  p_command_id uuid,
  p_claim_token uuid,
  p_holder_id text,
  p_release_sha text,
  p_fencing_token bigint,
  p_expected_command_revision bigint,
  p_expected_work_revision bigint,
  p_expected_control_epoch bigint,
  p_now timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  authorization_time timestamptz := clock_timestamp();
  command_row private.operation_commands%rowtype;
  request_row private.unknown_execution_resolution_requests_v2%rowtype;
  review_row private.unknown_execution_resolution_reviews_v2%rowtype;
  work_row private.unknown_execution_resolution_work_items_v2%rowtype;
  application_row private.unknown_execution_resolution_applications_v2%rowtype;
  break_row private.reconciliation_breaks%rowtype;
  intent_row private.order_intents%rowtype;
  unknown_row private.execution_observations%rowtype;
  attempt_row private.order_attempts%rowtype;
  reservation_row private.order_reservations%rowtype;
  control_row private.execution_controls%rowtype;
  lease_row private.worker_leases%rowtype;
  cash_row private.cash_balance_projection%rowtype;
  position_row private.position_projection%rowtype;
  position_exists boolean := false;
  proposal_manifest jsonb;
  manifest_result record;
  proposal_row private.unknown_execution_resolution_fill_proposals_v2%rowtype;
  proposal_count integer;
  observation_sequence integer;
  cumulative_quantity bigint;
  cumulative_gross bigint;
  cumulative_commission bigint;
  cumulative_tax bigint;
  observation_time timestamptz;
  previous_observation_time timestamptz;
  observation_status text;
  observation_hash text;
  observation_id_value uuid;
  terminal_observation_id_value uuid;
  fill_row private.fills%rowtype;
  accounting_result record;
  settlement_obligation_id_value uuid;
  accounting_key text;
  postings jsonb;
  gross_value bigint;
  cash_amount bigint;
  old_quantity bigint;
  old_average numeric(24,4);
  old_total_cost bigint;
  cost_relief bigint;
  realized_amount bigint;
  remaining_cash bigint;
  remaining_quantity bigint;
  target_remaining_cash bigint;
  target_remaining_quantity bigint;
  release_cash bigint;
  release_quantity bigint;
  reservation_sequence integer;
  buy_commission_rate_value numeric(18,12);
  application_id_value uuid;
  application_hash text;
  unresolved_incident_count integer;
begin
  perform private.require_service_role();
  if p_command_id is null
     or p_claim_token is null
     or nullif(btrim(p_holder_id), '') is null
     or p_release_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_fencing_token <= 0
     or p_expected_command_revision < 0
     or p_expected_work_revision < 0
     or p_expected_control_epoch <= 0
     or p_now < authorization_time - interval '5 minutes'
     or p_now > authorization_time + interval '30 seconds' then
    raise exception 'unknown_resolution_v2_apply_parameters_invalid'
      using errcode = '22023';
  end if;
  select * into request_row
  from private.unknown_execution_resolution_requests_v2
  where command_id = p_command_id;
  if not found then
    raise exception 'unknown_resolution_v2_request_not_found'
      using errcode = 'P0002';
  end if;
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      request_row.intent_id::text, 68491723011702::bigint
    )
  );
  select * into command_row
  from private.operation_commands
  where id = p_command_id
  for update;
  select * into review_row
  from private.unknown_execution_resolution_reviews_v2
  where command_id = p_command_id;
  select * into work_row
  from private.unknown_execution_resolution_work_items_v2
  where command_id = p_command_id
  for update;
  select * into break_row
  from private.reconciliation_breaks
  where id = request_row.break_id
  for update;
  select * into intent_row
  from private.order_intents
  where id = request_row.intent_id
  for update;
  select * into unknown_row
  from private.execution_observations
  where id = request_row.unknown_observation_id
  for update;
  select * into attempt_row
  from private.order_attempts
  where intent_id = request_row.intent_id
  for update;
  select * into control_row
  from private.execution_controls
  where account_id = request_row.account_id
  for update;
  select * into lease_row
  from private.worker_leases
  where account_id = request_row.account_id
  for update;

  if command_row.id is null
     or review_row.id is null
     or review_row.decision <> 'approved'
     or work_row.command_id is null
     or intent_row.id is null
     or unknown_row.id is null
     or attempt_row.id is null
     or command_row.target_release_sha <> p_release_sha
     or intent_row.release_sha <> p_release_sha
     or p_expected_control_epoch <> request_row.expected_control_epoch
     or control_row.account_id is null
     or control_row.execution_enabled is true
     or control_row.control_epoch <> p_expected_control_epoch
     or lease_row.account_id is null
     or lease_row.holder_id <> p_holder_id
     or lease_row.release_sha <> p_release_sha
     or lease_row.fencing_token <> p_fencing_token
     or lease_row.expires_at <= authorization_time then
    raise exception 'unknown_resolution_v2_apply_gate_stale'
      using errcode = '40001';
  end if;

  select * into application_row
  from private.unknown_execution_resolution_applications_v2
  where command_id = p_command_id;
  if found then
    if command_row.state <> 'applied'
       or work_row.state <> 'applied'
       or command_row.revision <> p_expected_command_revision
       or work_row.revision <> p_expected_work_revision
       or application_row.claim_token <> p_claim_token
       or application_row.worker_id <> p_holder_id
       or application_row.release_sha <> p_release_sha
       or application_row.fencing_token <> p_fencing_token
       or application_row.control_epoch <> p_expected_control_epoch
       or application_row.command_revision <> p_expected_command_revision
       or application_row.work_revision <> p_expected_work_revision then
      raise exception 'unknown_resolution_v2_apply_replay_conflict'
        using errcode = '40001';
    end if;
    return private.unknown_resolution_v2_receipt(p_command_id);
  end if;

  if command_row.state <> 'claimed'
     or work_row.state <> 'claimed'
     or command_row.revision <> p_expected_command_revision
     or work_row.revision <> p_expected_work_revision
     or work_row.claim_token <> p_claim_token
     or work_row.claim_owner <> p_holder_id
     or work_row.claim_release_sha <> p_release_sha
     or work_row.claim_fencing_token <> p_fencing_token
     or work_row.claim_expires_at <= authorization_time
     or command_row.claimed_by_service <> p_holder_id
     or command_row.claim_expires_at <= authorization_time
     or command_row.expires_at <= authorization_time
     or break_row.id is null
     or break_row.state <> 'resolution_requested'
     or break_row.resolution_command_id <> p_command_id
     or break_row.revision <> work_row.approved_break_revision
     or unknown_row.event_type <> 'unknown_requires_manual_check'
     or unknown_row.sequence <> request_row.baseline_sequence
     or unknown_row.cumulative_quantity <>
       request_row.baseline_cumulative_quantity
     or unknown_row.cumulative_gross_krw <>
       request_row.baseline_cumulative_gross_krw
     or unknown_row.cumulative_commission_krw <>
       request_row.baseline_cumulative_commission_krw
     or unknown_row.cumulative_tax_krw <>
       request_row.baseline_cumulative_tax_krw
     or exists (
       select 1
       from private.execution_observations as later_observation
       where later_observation.intent_id = request_row.intent_id
         and later_observation.sequence > request_row.baseline_sequence
     )
     or not exists (
       select 1
       from private.execution_reconciliation_state as reconciliation
       where reconciliation.intent_id = request_row.intent_id
         and reconciliation.state = 'manual'
     ) then
    raise exception 'unknown_resolution_v2_apply_state_stale'
      using errcode = '40001';
  end if;

  select * into reservation_row
  from private.order_reservations
  where intent_id = request_row.intent_id
  for update;
  select * into cash_row
  from private.cash_balance_projection
  where account_id = request_row.account_id
  for update;
  select * into position_row
  from private.position_projection
  where account_id = request_row.account_id
    and symbol = intent_row.symbol
  for update;
  position_exists := found;
  select
    event.remaining_cash_krw,
    event.remaining_quantity,
    event.event_sequence
  into remaining_cash, remaining_quantity, reservation_sequence
  from private.reservation_events as event
  where event.reservation_id = reservation_row.id
  order by event.event_sequence desc
  limit 1
  for update;
  if reservation_row.id is null
     or cash_row.account_id is null
     or cash_row.projection_version <>
       request_row.expected_cash_projection_version
     or reservation_sequence <>
       request_row.expected_reservation_event_sequence
     or (
       position_exists
       and position_row.projection_version is distinct from
         request_row.expected_position_projection_version
     )
     or (
       not position_exists
       and request_row.expected_position_projection_version is not null
     ) then
    raise exception 'unknown_resolution_v2_apply_projection_stale'
      using errcode = '40001';
  end if;

  select coalesce(
    jsonb_agg(
      jsonb_build_object(
        'fill_sequence', proposal.fill_sequence,
        'provider_order_id', proposal.provider_order_id,
        'provider_execution_id', proposal.provider_execution_id,
        'quantity', proposal.quantity,
        'price_krw', proposal.price_krw,
        'commission_krw', proposal.commission_krw,
        'tax_krw', proposal.tax_krw,
        'filled_at', proposal.filled_at,
        'settlement_date', proposal.settlement_date,
        'evidence_sha256', proposal.evidence_sha256
      ) order by proposal.fill_sequence
    ),
    '[]'::jsonb
  ), count(*)::integer
  into proposal_manifest, proposal_count
  from private.unknown_execution_resolution_fill_proposals_v2 as proposal
  where proposal.request_id = p_command_id;
  select * into manifest_result
  from private.assert_unknown_resolution_fill_manifest_v2(
    request_row.intent_id,
    request_row.unknown_observation_id,
    request_row.provider_order_id,
    request_row.terminal_status,
    proposal_manifest,
    request_row.evidence_captured_at
  );

  observation_sequence := request_row.baseline_sequence;
  cumulative_quantity := request_row.baseline_cumulative_quantity;
  cumulative_gross := request_row.baseline_cumulative_gross_krw;
  cumulative_commission := request_row.baseline_cumulative_commission_krw;
  cumulative_tax := request_row.baseline_cumulative_tax_krw;
  previous_observation_time := unknown_row.observed_at;

  for proposal_row in
    select *
    from private.unknown_execution_resolution_fill_proposals_v2 as proposal
    where proposal.request_id = p_command_id
    order by proposal.fill_sequence
  loop
    observation_sequence := observation_sequence + 1;
    cumulative_quantity := cumulative_quantity + proposal_row.quantity;
    gross_value := proposal_row.quantity * proposal_row.price_krw;
    cumulative_gross := cumulative_gross + gross_value;
    cumulative_commission :=
      cumulative_commission + proposal_row.commission_krw;
    cumulative_tax := cumulative_tax + proposal_row.tax_krw;
    observation_time := greatest(
      previous_observation_time, proposal_row.filled_at
    );
    observation_status := case
      when request_row.terminal_status = 'filled'
       and proposal_row.fill_sequence = proposal_count
      then 'filled'
      else 'partial_filled'
    end;
    observation_hash := private.compute_command_sha256(
      'unknown_resolution_v2:observation',
      jsonb_build_object(
        'command_id', p_command_id,
        'sequence', observation_sequence,
        'event_type', observation_status,
        'provider_order_id', request_row.provider_order_id,
        'provider_execution_id', proposal_row.provider_execution_id,
        'cumulative_quantity', cumulative_quantity,
        'cumulative_gross_krw', cumulative_gross,
        'cumulative_commission_krw', cumulative_commission,
        'cumulative_tax_krw', cumulative_tax,
        'proposal_sha256', proposal_row.proposal_sha256
      )
    );
    insert into private.execution_observations (
      intent_id, attempt_id, sequence, event_type, observed_at,
      cumulative_quantity, cumulative_gross_krw,
      cumulative_commission_krw, cumulative_tax_krw, reason_code,
      observation_sha256, provider_order_id, provider_execution_id,
      provider_observation_sha256
    ) values (
      request_row.intent_id, attempt_row.id, observation_sequence,
      observation_status, observation_time, cumulative_quantity,
      cumulative_gross, cumulative_commission, cumulative_tax,
      'reviewed_unknown_accounting_closure_v2', observation_hash,
      request_row.provider_order_id, proposal_row.provider_execution_id,
      observation_hash
    ) returning id into observation_id_value;
    insert into private.order_events (
      intent_id, attempt_id, observation_id, event_key, event_type,
      correlation_id, event_summary, occurred_at
    ) values (
      request_row.intent_id, attempt_row.id, observation_id_value,
      'unknown-resolution-v2:' || p_command_id::text || ':fill:'
        || proposal_row.fill_sequence::text,
      case when observation_status = 'filled'
        then 'terminal_confirmed' else 'observation_recorded' end,
      intent_row.correlation_id,
      jsonb_build_object(
        'schema_version', 2,
        'status', observation_status,
        'sequence', observation_sequence,
        'resolution_command_id', p_command_id,
        'proposal_sha256', proposal_row.proposal_sha256
      ),
      observation_time
    );
    insert into private.fills (
      event_id, intent_id, attempt_id, account_id, broker,
      provider_execution_id, quantity, price_krw, commission_krw,
      tax_krw, filled_at, settlement_date
    ) values (
      observation_id_value, request_row.intent_id, attempt_row.id,
      request_row.account_id, attempt_row.broker,
      proposal_row.provider_execution_id, proposal_row.quantity,
      proposal_row.price_krw, proposal_row.commission_krw,
      proposal_row.tax_krw, proposal_row.filled_at,
      proposal_row.settlement_date
    ) returning * into fill_row;

    reservation_sequence := reservation_sequence + 1;
    if intent_row.side = 'buy' then
      select schedule.buy_commission_rate into buy_commission_rate_value
      from private.execution_cost_schedules as schedule
      join private.control_evidence as evidence
        on evidence.id = schedule.evidence_id
       and evidence.artifact_sha256 =
         intent_row.cost_schedule_evidence_sha256
      where schedule.account_id = intent_row.account_id
        and schedule.schedule_version = intent_row.cost_schedule_version
        and schedule.status = 'approved'
        and schedule.effective_from <= proposal_row.filled_at
        and schedule.effective_until > proposal_row.filled_at
      limit 1;
      target_remaining_cash := case
        when observation_status = 'filled' then 0
        else
          (intent_row.quantity - cumulative_quantity)
            * intent_row.limit_price_krw
          + ceil(
              (intent_row.quantity - cumulative_quantity)
                * intent_row.limit_price_krw
                * buy_commission_rate_value
            )::bigint
      end;
      release_cash := remaining_cash - target_remaining_cash;
      cash_amount := gross_value + proposal_row.commission_krw
        + proposal_row.tax_krw;
      if release_cash < cash_amount then
        raise exception 'unknown_resolution_v2_buy_reservation_insufficient'
          using errcode = '23514';
      end if;
      update private.cash_balance_projection
      set reserved_cash_krw = reserved_cash_krw - release_cash,
          pending_debit_cash_krw = pending_debit_cash_krw + cash_amount,
          projection_version = projection_version + 1,
          projected_at = p_now
      where account_id = request_row.account_id
        and reserved_cash_krw >= release_cash;
      if not found then
        raise exception 'unknown_resolution_v2_cash_reservation_underflow'
          using errcode = '23514';
      end if;
      insert into private.reservation_events (
        reservation_id, intent_id, event_sequence, event_type,
        cash_delta_krw, quantity_delta, remaining_cash_krw,
        remaining_quantity, source_observation_id, occurred_at
      ) values (
        reservation_row.id, request_row.intent_id, reservation_sequence,
        case when observation_status = 'filled'
          then 'fully_consumed' else 'partially_consumed' end,
        -release_cash, 0, target_remaining_cash, 0,
        observation_id_value, observation_time
      );
      remaining_cash := target_remaining_cash;
    else
      target_remaining_quantity :=
        intent_row.quantity - cumulative_quantity;
      release_quantity := remaining_quantity - target_remaining_quantity;
      if release_quantity <> proposal_row.quantity then
        raise exception 'unknown_resolution_v2_sell_reservation_mismatch'
          using errcode = '23514';
      end if;
      update private.position_projection
      set reserved_quantity = reserved_quantity - release_quantity,
          pending_sell_quantity = pending_sell_quantity + proposal_row.quantity,
          projection_version = projection_version + 1,
          projected_at = p_now
      where account_id = request_row.account_id
        and symbol = intent_row.symbol
        and reserved_quantity >= release_quantity;
      if not found then
        raise exception 'unknown_resolution_v2_position_reservation_underflow'
          using errcode = '23514';
      end if;
      insert into private.reservation_events (
        reservation_id, intent_id, event_sequence, event_type,
        cash_delta_krw, quantity_delta, remaining_cash_krw,
        remaining_quantity, source_observation_id, occurred_at
      ) values (
        reservation_row.id, request_row.intent_id, reservation_sequence,
        case when observation_status = 'filled'
          then 'fully_consumed' else 'partially_consumed' end,
        0, -release_quantity, 0, target_remaining_quantity,
        observation_id_value, observation_time
      );
      remaining_quantity := target_remaining_quantity;
    end if;

    if intent_row.side = 'buy' then
      postings := jsonb_build_array(
        jsonb_build_object(
          'account', 'POSITION_COST',
          'debit_krw', gross_value,
          'credit_krw', 0
        )
      );
      if proposal_row.commission_krw > 0 then
        postings := postings || jsonb_build_array(jsonb_build_object(
          'account', 'FEES',
          'debit_krw', proposal_row.commission_krw,
          'credit_krw', 0
        ));
      end if;
      postings := postings || jsonb_build_array(jsonb_build_object(
        'account', 'CASH',
        'debit_krw', 0,
        'credit_krw', cash_amount
      ));
    else
      select quantity, average_cost_krw
      into old_quantity, old_average
      from private.position_projection
      where account_id = request_row.account_id
        and symbol = intent_row.symbol
      for update;
      old_total_cost := round(old_quantity * old_average)::bigint;
      if old_quantity < proposal_row.quantity or old_quantity <= 0 then
        raise exception 'unknown_resolution_v2_position_underflow'
          using errcode = '23514';
      end if;
      cost_relief := case
        when proposal_row.quantity = old_quantity then old_total_cost
        else floor(
          old_total_cost::numeric * proposal_row.quantity / old_quantity
        )::bigint
      end;
      realized_amount := gross_value - cost_relief;
      cash_amount := gross_value - proposal_row.commission_krw
        - proposal_row.tax_krw;
      if cash_amount <= 0 then
        raise exception 'unknown_resolution_v2_sell_cash_amount_invalid'
          using errcode = '23514';
      end if;
      postings := jsonb_build_array(jsonb_build_object(
        'account', 'CASH',
        'debit_krw', cash_amount,
        'credit_krw', 0
      ));
      if proposal_row.commission_krw > 0 then
        postings := postings || jsonb_build_array(jsonb_build_object(
          'account', 'FEES',
          'debit_krw', proposal_row.commission_krw,
          'credit_krw', 0
        ));
      end if;
      if proposal_row.tax_krw > 0 then
        postings := postings || jsonb_build_array(jsonb_build_object(
          'account', 'TAXES',
          'debit_krw', proposal_row.tax_krw,
          'credit_krw', 0
        ));
      end if;
      postings := postings || jsonb_build_array(jsonb_build_object(
        'account', 'POSITION_COST',
        'debit_krw', 0,
        'credit_krw', cost_relief
      ));
      if realized_amount > 0 then
        postings := postings || jsonb_build_array(jsonb_build_object(
          'account', 'REALIZED_PNL',
          'debit_krw', 0,
          'credit_krw', realized_amount
        ));
      elsif realized_amount < 0 then
        postings := postings || jsonb_build_array(jsonb_build_object(
          'account', 'REALIZED_PNL',
          'debit_krw', -realized_amount,
          'credit_krw', 0
        ));
      end if;
    end if;

    accounting_key := pg_catalog.encode(
      public.digest(
        pg_catalog.convert_to(
          request_row.intent_id::text || ':fill:'
            || observation_sequence::text,
          'UTF8'
        ),
        'sha256'
      ),
      'hex'
    );
    select * into accounting_result
    from private.post_accounting_transaction_impl(
      accounting_key,
      request_row.intent_id,
      observation_sequence,
      p_now,
      postings,
      p_holder_id,
      p_fencing_token
    );
    if accounting_result.inserted is not true then
      raise exception 'unknown_resolution_v2_accounting_replay_conflict'
        using errcode = '23514';
    end if;
    select obligation.id into settlement_obligation_id_value
    from private.cash_settlement_obligations as obligation
    where obligation.fill_id = fill_row.id
      and obligation.trade_accounting_transaction_id =
        accounting_result.transaction_id;
    if settlement_obligation_id_value is null then
      raise exception 'unknown_resolution_v2_settlement_obligation_missing'
        using errcode = '23514';
    end if;
    previous_observation_time := observation_time;
    terminal_observation_id_value := observation_id_value;
  end loop;

  if request_row.terminal_status <> 'filled' then
    observation_sequence := observation_sequence + 1;
    observation_time := greatest(
      previous_observation_time, request_row.evidence_captured_at
    );
    observation_hash := private.compute_command_sha256(
      'unknown_resolution_v2:terminal_observation',
      jsonb_build_object(
        'command_id', p_command_id,
        'sequence', observation_sequence,
        'event_type', request_row.terminal_status,
        'provider_order_id', request_row.provider_order_id,
        'cumulative_quantity', cumulative_quantity,
        'cumulative_gross_krw', cumulative_gross,
        'cumulative_commission_krw', cumulative_commission,
        'cumulative_tax_krw', cumulative_tax,
        'evidence_sha256', request_row.evidence_sha256
      )
    );
    insert into private.execution_observations (
      intent_id, attempt_id, sequence, event_type, observed_at,
      cumulative_quantity, cumulative_gross_krw,
      cumulative_commission_krw, cumulative_tax_krw, reason_code,
      observation_sha256, provider_order_id, provider_execution_id,
      provider_observation_sha256
    ) values (
      request_row.intent_id, attempt_row.id, observation_sequence,
      request_row.terminal_status, observation_time, cumulative_quantity,
      cumulative_gross, cumulative_commission, cumulative_tax,
      'reviewed_unknown_accounting_closure_v2', observation_hash,
      request_row.provider_order_id, null, observation_hash
    ) returning id into terminal_observation_id_value;
    insert into private.order_events (
      intent_id, attempt_id, observation_id, event_key, event_type,
      correlation_id, event_summary, occurred_at
    ) values (
      request_row.intent_id, attempt_row.id, terminal_observation_id_value,
      'unknown-resolution-v2:' || p_command_id::text || ':terminal',
      'terminal_confirmed', intent_row.correlation_id,
      jsonb_build_object(
        'schema_version', 2,
        'status', request_row.terminal_status,
        'sequence', observation_sequence,
        'resolution_command_id', p_command_id,
        'evidence_sha256', request_row.evidence_sha256
      ),
      observation_time
    );
    reservation_sequence := reservation_sequence + 1;
    if intent_row.side = 'buy' and remaining_cash > 0 then
      update private.cash_balance_projection
      set reserved_cash_krw = reserved_cash_krw - remaining_cash,
          projection_version = projection_version + 1,
          projected_at = p_now
      where account_id = request_row.account_id
        and reserved_cash_krw >= remaining_cash;
      if not found then
        raise exception 'unknown_resolution_v2_cash_release_underflow'
          using errcode = '23514';
      end if;
      insert into private.reservation_events (
        reservation_id, intent_id, event_sequence, event_type,
        cash_delta_krw, quantity_delta, remaining_cash_krw,
        remaining_quantity, source_observation_id, occurred_at
      ) values (
        reservation_row.id, request_row.intent_id, reservation_sequence,
        'released', -remaining_cash, 0, 0, 0,
        terminal_observation_id_value, observation_time
      );
      remaining_cash := 0;
    elsif intent_row.side = 'sell' and remaining_quantity > 0 then
      update private.position_projection
      set reserved_quantity = reserved_quantity - remaining_quantity,
          projection_version = projection_version + 1,
          projected_at = p_now
      where account_id = request_row.account_id
        and symbol = intent_row.symbol
        and reserved_quantity >= remaining_quantity;
      if not found then
        raise exception 'unknown_resolution_v2_position_release_underflow'
          using errcode = '23514';
      end if;
      insert into private.reservation_events (
        reservation_id, intent_id, event_sequence, event_type,
        cash_delta_krw, quantity_delta, remaining_cash_krw,
        remaining_quantity, source_observation_id, occurred_at
      ) values (
        reservation_row.id, request_row.intent_id, reservation_sequence,
        'released', 0, -remaining_quantity, 0, 0,
        terminal_observation_id_value, observation_time
      );
      remaining_quantity := 0;
    end if;
  end if;

  if terminal_observation_id_value is null
     or cumulative_quantity <> manifest_result.final_cumulative_quantity
     or cumulative_gross <> manifest_result.final_cumulative_gross_krw
     or cumulative_commission <>
       manifest_result.final_cumulative_commission_krw
     or cumulative_tax <> manifest_result.final_cumulative_tax_krw
     or (intent_row.side = 'buy' and remaining_cash <> 0)
     or (intent_row.side = 'sell' and remaining_quantity <> 0) then
    raise exception 'unknown_resolution_v2_terminal_postcondition_failed'
      using errcode = '23514';
  end if;

  update private.execution_reconciliation_state
  set state = 'complete',
      next_reconcile_at = p_now,
      lease_owner = null,
      lease_expires_at = null,
      last_reason_code = 'reviewed_unknown_accounting_closure_v2',
      updated_at = p_now
  where intent_id = request_row.intent_id
    and state = 'manual';
  if not found then
    raise exception 'unknown_resolution_v2_reconciliation_state_stale'
      using errcode = '40001';
  end if;
  update private.reconciliation_breaks
  set state = 'resolved',
      evidence_id = command_row.evidence_id,
      resolution_command_id = p_command_id,
      resolved_at = p_now,
      revision = revision + 1
  where id = request_row.break_id
    and state = 'resolution_requested'
    and revision = work_row.approved_break_revision;
  if not found then
    raise exception 'unknown_resolution_v2_break_stale'
      using errcode = '40001';
  end if;

  select count(*)::integer into unresolved_incident_count
  from private.incidents as incident
  where incident.incident_type = 'execution_unknown'
    and incident.correlation_id = intent_row.correlation_id
    and incident.status <> 'resolved';
  if unresolved_incident_count = 0
     or exists (
       select 1
       from private.incidents as incident
       where incident.incident_type = 'execution_unknown'
         and incident.correlation_id = intent_row.correlation_id
         and incident.status = 'acknowledged'
         and incident.acknowledged_by = review_row.reviewer_user_id
     ) then
    raise exception 'unknown_resolution_v2_incident_two_person_closure_invalid'
      using errcode = '23514';
  end if;
  update private.incidents
  set status = 'resolved',
      acknowledged_at = coalesce(acknowledged_at, request_row.requested_at),
      acknowledged_by = coalesce(
        acknowledged_by, request_row.requester_user_id
      ),
      resolved_at = p_now,
      resolved_by = review_row.reviewer_user_id,
      resolution_code = 'reviewed_unknown_accounting_closure_v2'
  where incident_type = 'execution_unknown'
    and correlation_id = intent_row.correlation_id
    and status <> 'resolved';

  application_id_value := md5(
    'unknown-execution-resolution-v2-application:' || p_command_id::text
  )::uuid;
  application_hash := private.compute_command_sha256(
    'unknown_resolution_v2:application',
    jsonb_build_object(
      'application_id', application_id_value,
      'command_id', p_command_id,
      'request_payload_sha256', request_row.payload_sha256,
      'review_payload_sha256', review_row.payload_sha256,
      'claim_token', p_claim_token,
      'worker_id', p_holder_id,
      'release_sha', p_release_sha,
      'fencing_token', p_fencing_token,
      'control_epoch', p_expected_control_epoch,
      'terminal_observation_id', terminal_observation_id_value,
      'terminal_status', request_row.terminal_status,
      'final_cumulative_quantity', cumulative_quantity,
      'final_cumulative_gross_krw', cumulative_gross,
      'final_cumulative_commission_krw', cumulative_commission,
      'final_cumulative_tax_krw', cumulative_tax,
      'command_revision', command_row.revision + 1,
      'work_revision', work_row.revision + 1
    )
  );
  insert into private.unknown_execution_resolution_applications_v2 (
    id, command_id, request_id, review_id, break_id, intent_id,
    evidence_id, terminal_observation_id, request_payload_sha256,
    review_payload_sha256, claim_token, worker_id, release_sha,
    fencing_token, control_epoch, final_cumulative_quantity,
    final_cumulative_gross_krw, final_cumulative_commission_krw,
    final_cumulative_tax_krw, terminal_status, command_revision,
    work_revision, applied_at, application_sha256
  ) values (
    application_id_value, p_command_id, p_command_id, review_row.id,
    request_row.break_id, request_row.intent_id, command_row.evidence_id,
    terminal_observation_id_value, request_row.payload_sha256,
    review_row.payload_sha256, p_claim_token, p_holder_id, p_release_sha,
    p_fencing_token, p_expected_control_epoch, cumulative_quantity,
    cumulative_gross, cumulative_commission, cumulative_tax,
    request_row.terminal_status, command_row.revision + 1,
    work_row.revision + 1, p_now, application_hash
  );
  insert into private.unknown_execution_resolution_application_fills_v2 (
    application_id, fill_sequence, proposal_sha256, observation_id,
    fill_id, accounting_transaction_id, settlement_obligation_id,
    provider_execution_id
  )
  select
    application_id_value,
    proposal.fill_sequence,
    proposal.proposal_sha256,
    fill.event_id,
    fill.id,
    obligation.trade_accounting_transaction_id,
    obligation.id,
    proposal.provider_execution_id
  from private.unknown_execution_resolution_fill_proposals_v2 as proposal
  join private.fills as fill
    on fill.intent_id = request_row.intent_id
   and fill.account_id = request_row.account_id
   and fill.broker = attempt_row.broker
   and fill.provider_execution_id = proposal.provider_execution_id
  join private.cash_settlement_obligations as obligation
    on obligation.fill_id = fill.id
  where proposal.request_id = p_command_id
  order by proposal.fill_sequence;
  if (select count(*)
      from private.unknown_execution_resolution_application_fills_v2
      where application_id = application_id_value) <> proposal_count then
    raise exception 'unknown_resolution_v2_application_fill_receipt_incomplete'
      using errcode = '23514';
  end if;

  update private.unknown_execution_resolution_work_items_v2
  set state = 'applied',
      revision = revision + 1,
      applied_at = p_now,
      updated_at = p_now
  where command_id = p_command_id
    and state = 'claimed'
    and revision = p_expected_work_revision
    and claim_token = p_claim_token
  returning * into work_row;
  if not found then
    raise exception 'unknown_resolution_v2_apply_state_stale'
      using errcode = '40001';
  end if;
  perform set_config(
    'msp.unknown_resolution_v2_phase',
    'apply:' || p_command_id::text,
    true
  );
  update private.operation_commands
  set state = 'applied',
      applied_at = p_now,
      result_summary = jsonb_build_object(
        'schema_version', 2,
        'application_id', application_id_value,
        'application_sha256', application_hash,
        'terminal_observation_id', terminal_observation_id_value,
        'terminal_status', request_row.terminal_status,
        'final_cumulative_quantity', cumulative_quantity,
        'resolution_complete', true
      ),
      revision = revision + 1
  where id = p_command_id
    and state = 'claimed'
    and revision = p_expected_command_revision
  returning * into command_row;
  if not found then
    raise exception 'unknown_resolution_v2_apply_state_stale'
      using errcode = '40001';
  end if;
  insert into private.operation_command_events (
    command_id, event_type, actor_type, service_principal,
    event_summary, occurred_at
  ) values (
    p_command_id, 'applied', 'worker', p_holder_id,
    jsonb_build_object(
      'schema_version', 2,
      'application_id', application_id_value,
      'application_sha256', application_hash,
      'terminal_observation_id', terminal_observation_id_value,
      'terminal_status', request_row.terminal_status,
      'final_cumulative_quantity', cumulative_quantity,
      'settlement_obligation_count', proposal_count,
      'resolution_complete', true
    ),
    p_now
  );
  perform private.write_audit_event(
    'worker', null, null, 'trading_worker', p_holder_id, p_release_sha,
    'unknown_execution_accounting_closure_applied',
    'reconciliation_break', request_row.break_id::text,
    intent_row.correlation_id, p_command_id,
    'reviewed_unknown_accounting_closure_v2', null,
    array[
      'application_sha256', 'terminal_observation_id',
      'final_cumulative_quantity', 'settlement_obligation_count',
      'break_state', 'incident_state', 'command_state'
    ],
    request_row.payload_sha256, application_hash, command_row.evidence_id
  );
  insert into private.delivery_outbox (
    event_type, aggregate_type, aggregate_id, dedupe_key,
    payload, destination_type
  ) values (
    'execution_unknown_resolved', 'reconciliation_break',
    request_row.break_id::text,
    'execution-unknown-resolved-v2:' || p_command_id::text,
    jsonb_build_object(
      'schema_version', 2,
      'command_id', p_command_id,
      'break_id', request_row.break_id,
      'intent_id', request_row.intent_id,
      'application_sha256', application_hash,
      'terminal_status', request_row.terminal_status,
      'settlement_obligation_count', proposal_count
    ),
    'incident_alert'
  );
  return private.unknown_resolution_v2_receipt(p_command_id)
    || jsonb_build_object('inserted', true);
end;
$$;

create or replace function api.issue_unknown_resolution_step_up_v2(
  request_payload jsonb
)
returns jsonb
language sql
security invoker
set search_path = ''
as $$
  select private.issue_unknown_resolution_step_up_v2_impl(request_payload);
$$;

create or replace function api.request_unknown_resolution_v2(
  request_payload jsonb
)
returns jsonb
language sql
security invoker
set search_path = ''
as $$
  select private.request_unknown_resolution_v2_impl(request_payload);
$$;

create or replace function api.review_unknown_resolution_v2(
  review_payload jsonb
)
returns jsonb
language sql
security invoker
set search_path = ''
as $$
  select private.review_unknown_resolution_v2_impl(review_payload);
$$;

create or replace function worker_api.list_unknown_resolution_v2(
  account_id text,
  holder_id text,
  release_sha text,
  fencing_token bigint,
  result_limit integer,
  observed_at timestamptz
)
returns table (
  command_id uuid,
  request_id uuid,
  review_id uuid,
  break_id uuid,
  intent_id uuid,
  terminal_status text,
  request_payload_sha256 text,
  review_payload_sha256 text,
  command_revision bigint,
  work_revision bigint,
  work_state text,
  claim_token uuid,
  claim_expires_at timestamptz,
  expected_control_epoch bigint
)
language sql
security invoker
set search_path = ''
as $$
  select *
  from private.list_unknown_resolution_v2_impl(
    account_id, holder_id, release_sha, fencing_token,
    result_limit, observed_at
  );
$$;

create or replace function worker_api.claim_unknown_resolution_v2(
  command_id uuid,
  holder_id text,
  release_sha text,
  fencing_token bigint,
  expected_command_revision bigint,
  expected_work_revision bigint,
  claimed_at timestamptz
)
returns table (
  command_id uuid,
  claim_token uuid,
  command_revision bigint,
  work_revision bigint,
  claim_expires_at timestamptz
)
language sql
security invoker
set search_path = ''
as $$
  select *
  from private.claim_unknown_resolution_v2_impl(
    command_id, holder_id, release_sha, fencing_token,
    expected_command_revision, expected_work_revision, claimed_at
  );
$$;

create or replace function worker_api.apply_unknown_resolution_v2(
  command_id uuid,
  claim_token uuid,
  holder_id text,
  release_sha text,
  fencing_token bigint,
  expected_command_revision bigint,
  expected_work_revision bigint,
  expected_control_epoch bigint,
  applied_at timestamptz
)
returns jsonb
language sql
security invoker
set search_path = ''
as $$
  select private.apply_unknown_resolution_v2_impl(
    command_id, claim_token, holder_id, release_sha, fencing_token,
    expected_command_revision, expected_work_revision,
    expected_control_epoch, applied_at
  );
$$;

revoke all on
  private.unknown_execution_resolution_requests_v2,
  private.unknown_execution_resolution_fill_proposals_v2,
  private.unknown_execution_resolution_reviews_v2,
  private.unknown_execution_resolution_work_items_v2,
  private.unknown_execution_resolution_applications_v2,
  private.unknown_execution_resolution_application_fills_v2
from public, anon, authenticated, service_role;

revoke execute on function
  private.guard_unknown_resolution_v2_command_transition(),
  private.validate_unknown_resolution_draft_v2(text, jsonb),
  private.issue_unknown_resolution_step_up_v2_impl(jsonb),
  private.consume_unknown_resolution_step_up_v2(jsonb, text, timestamptz, text),
  private.assert_unknown_resolution_fill_manifest_v2(
    uuid, uuid, text, text, jsonb, timestamptz
  ),
  private.unknown_resolution_v2_receipt(uuid),
  private.request_unknown_resolution_v2_impl(jsonb),
  private.review_unknown_resolution_v2_impl(jsonb),
  private.list_unknown_resolution_v2_impl(
    text, text, text, bigint, integer, timestamptz
  ),
  private.claim_unknown_resolution_v2_impl(
    uuid, text, text, bigint, bigint, bigint, timestamptz
  ),
  private.apply_unknown_resolution_v2_impl(
    uuid, uuid, text, text, bigint, bigint, bigint, bigint, timestamptz
  )
from public, anon, authenticated, service_role;

revoke execute on function
  api.issue_unknown_resolution_step_up_v2(jsonb),
  api.request_unknown_resolution_v2(jsonb),
  api.review_unknown_resolution_v2(jsonb),
  worker_api.list_unknown_resolution_v2(
    text, text, text, bigint, integer, timestamptz
  ),
  worker_api.claim_unknown_resolution_v2(
    uuid, text, text, bigint, bigint, bigint, timestamptz
  ),
  worker_api.apply_unknown_resolution_v2(
    uuid, uuid, text, text, bigint, bigint, bigint, bigint, timestamptz
  )
from public, anon, authenticated, service_role;

grant execute on function
  private.issue_unknown_resolution_step_up_v2_impl(jsonb),
  private.request_unknown_resolution_v2_impl(jsonb),
  private.review_unknown_resolution_v2_impl(jsonb)
to authenticated;
grant execute on function
  api.issue_unknown_resolution_step_up_v2(jsonb),
  api.request_unknown_resolution_v2(jsonb),
  api.review_unknown_resolution_v2(jsonb)
to authenticated;

grant execute on function
  private.list_unknown_resolution_v2_impl(
    text, text, text, bigint, integer, timestamptz
  ),
  private.claim_unknown_resolution_v2_impl(
    uuid, text, text, bigint, bigint, bigint, timestamptz
  ),
  private.apply_unknown_resolution_v2_impl(
    uuid, uuid, text, text, bigint, bigint, bigint, bigint, timestamptz
  ),
  worker_api.list_unknown_resolution_v2(
    text, text, text, bigint, integer, timestamptz
  ),
  worker_api.claim_unknown_resolution_v2(
    uuid, text, text, bigint, bigint, bigint, timestamptz
  ),
  worker_api.apply_unknown_resolution_v2(
    uuid, uuid, text, text, bigint, bigint, bigint, bigint, timestamptz
  )
to service_role;

commit;
