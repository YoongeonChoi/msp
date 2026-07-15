-- Canonical v1 operations contract completion.
--
-- This migration replaces the temporary v1 mutation stubs with strict,
-- server-hashed commands. Exposed functions remain SECURITY INVOKER; all
-- privileged implementation stays in the non-exposed private schema.

alter table private.incidents
  add column ack_due_at timestamptz,
  add column escalation_status text not null default 'not_required'
    check (escalation_status in ('not_required', 'pending', 'canceled', 'escalated'));

alter table private.delivery_outbox
  drop constraint delivery_outbox_status_check;
alter table private.delivery_outbox
  add constraint delivery_outbox_status_check check (
    status in ('pending', 'leased', 'delivered', 'dead_letter', 'canceled')
  );

create or replace function private.prepare_incident_escalation()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.severity = 'critical' then
    new.ack_due_at := coalesce(new.ack_due_at, new.opened_at + interval '5 minutes');
    new.escalation_status := 'pending';
  else
    new.ack_due_at := null;
    new.escalation_status := 'not_required';
  end if;
  return new;
end;
$$;

create trigger prepare_incident_escalation
  before insert on private.incidents
  for each row execute function private.prepare_incident_escalation();

create or replace function private.enqueue_incident_alerts()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  insert into private.delivery_outbox (
    event_type, aggregate_type, aggregate_id, dedupe_key, payload,
    destination_type, available_at
  ) values (
    'critical_incident_opened', 'incident', new.id::text,
    'critical-incident-opened:' || new.id::text,
    jsonb_build_object(
      'incident_id', new.id,
      'summary_code', new.summary_code,
      'ack_due_at', new.ack_due_at
    ),
    'incident_alert', new.opened_at
  );
  if new.severity = 'critical' then
    insert into private.delivery_outbox (
      event_type, aggregate_type, aggregate_id, dedupe_key, payload,
      destination_type, available_at
    ) values (
      'critical_incident_ack_escalation', 'incident', new.id::text,
      'critical-incident-escalation:' || new.id::text,
      jsonb_build_object(
        'incident_id', new.id,
        'summary_code', new.summary_code,
        'ack_due_at', new.ack_due_at
      ),
      'incident_alert', new.ack_due_at
    );
  end if;
  return new;
end;
$$;

create trigger enqueue_incident_alerts
  after insert on private.incidents
  for each row execute function private.enqueue_incident_alerts();

create or replace function private.quarantine_execution_observation(
  p_intent_id uuid,
  p_attempt_id uuid,
  p_sequence integer,
  p_provider_order_id text,
  p_provider_execution_id text,
  p_provider_observation_sha256 text,
  p_payload_sha256 text,
  p_reason_code text,
  p_observed_at timestamptz,
  p_holder_id text
)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
  intent_row private.order_intents%rowtype;
  quarantine_id uuid;
  run_id uuid;
  break_id uuid;
begin
  select * into intent_row from private.order_intents where id = p_intent_id;
  if not found then
    raise exception 'execution_intent_not_found' using errcode = 'P0002';
  end if;
  insert into private.quarantined_execution_observations (
    intent_id, attempt_id, sequence, provider_order_id,
    provider_execution_id, provider_observation_sha256, payload_sha256,
    reason_code, observed_at
  ) values (
    p_intent_id, p_attempt_id, p_sequence, p_provider_order_id,
    p_provider_execution_id, p_provider_observation_sha256, p_payload_sha256,
    p_reason_code, p_observed_at
  ) on conflict (intent_id, sequence, provider_observation_sha256) do nothing
  returning id into quarantine_id;
  if quarantine_id is null then
    select id into quarantine_id
    from private.quarantined_execution_observations
    where intent_id = p_intent_id
      and sequence = p_sequence
      and provider_observation_sha256 = p_provider_observation_sha256;
    return quarantine_id;
  end if;
  insert into private.reconciliation_runs (
    account_id, environment, started_at, completed_at, result, release_sha
  ) values (
    intent_row.account_id, intent_row.environment, clock_timestamp(),
    clock_timestamp(), 'breaks_found', intent_row.release_sha
  ) returning id into run_id;
  insert into private.reconciliation_breaks (
    run_id, account_id, break_type, state, detected_at, summary_code
  ) values (
    run_id, intent_row.account_id, 'execution', 'open',
    clock_timestamp(), p_reason_code
  ) returning id into break_id;
  insert into private.order_events (
    intent_id, attempt_id, event_key, event_type, correlation_id,
    event_summary, occurred_at
  ) values (
    p_intent_id, p_attempt_id,
    'quarantine:' || p_sequence::text || ':' || left(p_provider_observation_sha256, 16),
    'manual_check_quarantined', intent_row.correlation_id,
    jsonb_build_object(
      'quarantine_id', quarantine_id,
      'reconciliation_break_id', break_id,
      'reason_code', p_reason_code
    ), p_observed_at
  );
  insert into private.incidents (
    severity, incident_type, summary_code, correlation_id
  ) values (
    'critical', 'execution_quarantined', p_reason_code,
    intent_row.correlation_id
  );
  update private.execution_reconciliation_state
  set state = 'manual',
      lease_owner = null,
      lease_expires_at = null,
      last_reason_code = p_reason_code,
      updated_at = clock_timestamp()
  where intent_id = p_intent_id;
  perform private.write_audit_event(
    'worker', null, null, 'trading_worker', p_holder_id,
    intent_row.release_sha, 'execution_observation_quarantined',
    'order', p_intent_id::text, intent_row.correlation_id, null,
    p_reason_code, null,
    array['provider_order_id', 'provider_execution_id', 'sequence'],
    null, p_payload_sha256, null
  );
  return quarantine_id;
end;
$$;

create or replace function private.assert_exact_json_keys(
  p_payload jsonb,
  p_expected_keys text[]
)
returns void
language plpgsql
immutable
security definer
set search_path = ''
as $$
declare
  actual_keys text[];
  expected_keys text[];
begin
  if coalesce(jsonb_typeof(p_payload), '') <> 'object' then
    raise exception 'json_object_required' using errcode = '22023';
  end if;
  select coalesce(array_agg(key order by key), array[]::text[])
  into actual_keys
  from jsonb_object_keys(p_payload) as key;
  select coalesce(array_agg(key order by key), array[]::text[])
  into expected_keys
  from unnest(p_expected_keys) as key;
  if actual_keys is distinct from expected_keys then
    raise exception 'json_contract_keys_invalid' using errcode = '22023';
  end if;
end;
$$;

create or replace function private.command_review_v1(p_review_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  review_row private.operation_command_reviews%rowtype;
  command_row private.operation_commands%rowtype;
begin
  select * into review_row
  from private.operation_command_reviews
  where id = p_review_id;
  if not found then
    raise exception 'operation_command_review_not_found' using errcode = 'P0002';
  end if;
  select * into command_row
  from private.operation_commands
  where id = review_row.command_id;
  return jsonb_build_object(
    'schema_version', 1,
    'review_id', review_row.id,
    'command_id', review_row.command_id,
    'command_type', private.external_command_type_v1(command_row.command_type),
    'reviewer', private.actor_ref_v1(review_row.reviewer_user_id),
    'reviewer_role', review_row.reviewer_role,
    'step_up_grant_id', review_row.step_up_grant_id,
    'command_hash', (
      select command_sha256 from private.step_up_grants
      where id = review_row.step_up_grant_id
    ),
    'decision', review_row.decision,
    'reason_code', review_row.reason_code,
    'reviewed_at', review_row.reviewed_at,
    'receipt_revision', command_row.revision
  );
end;
$$;

create or replace function private.review_operation_command_v1_impl(
  p_review_payload jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid;
  review_id uuid;
  command_id uuid;
  command_type_external text;
  command_type_internal text;
  reviewer_role_value text;
  required_role text;
  decision_value text;
  next_state text;
  reviewed_time timestamptz;
  expected_revision bigint;
  draft_payload jsonb;
  command_hash text;
  command_row private.operation_commands%rowtype;
begin
  perform private.assert_exact_json_keys(p_review_payload, array[
    'schema_version', 'review_id', 'command_id', 'command_type',
    'reviewer_role', 'decision', 'reason_code', 'expected_receipt_revision',
    'reviewed_at', 'step_up_grant_id', 'command_hash',
    'step_up_grant_issued_at', 'step_up_grant_expires_at',
    'step_up_grant_one_time', 'step_up_grant_consumed_at',
    'bound_action', 'bound_command_type'
  ]);
  if jsonb_typeof(p_review_payload->'schema_version') <> 'number'
     or jsonb_typeof(p_review_payload->'expected_receipt_revision') <> 'number'
     or jsonb_typeof(p_review_payload->'step_up_grant_one_time') <> 'boolean'
     or jsonb_typeof(p_review_payload->'step_up_grant_consumed_at') <> 'null' then
    raise exception 'operation_review_json_type_invalid' using errcode = '22023';
  end if;
  draft_payload := private.command_draft_v1(p_review_payload);
  command_type_external := p_review_payload->>'command_type';
  perform private.validate_command_draft_v1(
    'review', command_type_external, draft_payload
  );
  if coalesce((p_review_payload->>'schema_version')::integer, -1) <> 1 then
    raise exception 'operation_review_schema_version_invalid' using errcode = '22023';
  end if;
  review_id := (p_review_payload->>'review_id')::uuid;
  command_id := (p_review_payload->>'command_id')::uuid;
  command_type_internal := private.internal_command_type_v1(command_type_external);
  reviewer_role_value := p_review_payload->>'reviewer_role';
  decision_value := p_review_payload->>'decision';
  reviewed_time := (p_review_payload->>'reviewed_at')::timestamptz;
  expected_revision := (p_review_payload->>'expected_receipt_revision')::bigint;
  required_role := case command_type_external
    when 'activate_paper_strategy' then 'strategy_reviewer'
    when 'start_contract_test' then 'risk_approver'
    when 'pause_paper' then 'risk_approver'
    when 'resume_paper' then 'risk_approver'
    when 'apply_risk_policy_version' then 'risk_approver'
    else null
  end;
  if required_role is null
     or reviewer_role_value <> required_role
     or decision_value not in ('approve', 'reject')
     or p_review_payload->>'reason_code' not in (
       'policy_satisfied', 'evidence_incomplete', 'risk_rejected',
       'separation_of_duties'
     )
     or expected_revision < 0
     or reviewed_time < clock_timestamp() - interval '5 minutes'
     or reviewed_time > clock_timestamp() + interval '30 seconds' then
    raise exception 'operation_review_values_invalid' using errcode = '22023';
  end if;
  actor := private.require_human_roles(array[required_role], true);
  if private.has_active_role(actor, 'platform_admin') then
    raise exception 'platform_admin_trading_action_forbidden' using errcode = '42501';
  end if;
  command_hash := private.compute_command_sha256(
    'operation_v1:review', draft_payload
  );
  if p_review_payload->>'command_hash' <> command_hash then
    raise exception 'operation_review_hash_mismatch' using errcode = '42501';
  end if;
  perform private.consume_bound_step_up_v1(
    p_review_payload, 'review', command_type_external,
    reviewed_time, 'review_operation_command_v1'
  );

  select * into command_row
  from private.operation_commands
  where id = command_id
  for update;
  if not found then
    raise exception 'operation_command_not_found' using errcode = 'P0002';
  end if;
  if command_row.command_type <> command_type_internal
     or command_row.state <> 'requested'
     or command_row.revision <> expected_revision
     or command_row.expires_at <= reviewed_time then
    raise exception 'operation_command_not_reviewable_or_stale' using errcode = '40001';
  end if;
  if command_row.requester_user_id = actor then
    raise exception 'operation_command_self_review_forbidden' using errcode = '42501';
  end if;
  next_state := case when decision_value = 'approve' then 'approved' else 'rejected' end;
  update private.operation_commands
  set state = next_state,
      reviewer_user_id = actor,
      reviewed_at = reviewed_time,
      review_reason = p_review_payload->>'reason_code',
      revision = revision + 1
  where id = command_id
  returning * into command_row;
  insert into private.operation_command_reviews (
    id, command_id, reviewer_user_id, reviewer_role, decision, reason_code,
    step_up_grant_id, reviewed_at
  ) values (
    review_id, command_id, actor, required_role,
    case when decision_value = 'approve' then 'approved' else 'rejected' end,
    p_review_payload->>'reason_code',
    (p_review_payload->>'step_up_grant_id')::uuid, reviewed_time
  );
  insert into private.operation_command_events (
    command_id, event_type, actor_type, actor_user_id, event_summary, occurred_at
  ) values (
    command_id, next_state, 'human', actor,
    jsonb_build_object(
      'review_id', review_id,
      'reviewer_role', required_role,
      'reason_code', p_review_payload->>'reason_code'
    ), reviewed_time
  );
  perform private.write_audit_event(
    'human', actor, required_role, null, null, command_row.target_release_sha,
    'command_reviewed', 'command', command_id::text, review_id,
    command_id, p_review_payload->>'reason_code', null,
    array['state', 'reviewer_user_id', 'revision'], null,
    command_hash, command_row.evidence_id
  );
  return private.operation_command_receipt_v1(command_id);
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'operation_review_values_invalid' using errcode = '22023';
end;
$$;

create or replace function private.act_on_operation_incident_v1_impl(
  p_action_payload jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid;
  action_id uuid;
  incident_id_value uuid;
  action_value text;
  expected_status_value text;
  reason_value text;
  acted_time timestamptz;
  incident_row private.incidents%rowtype;
begin
  perform private.assert_exact_json_keys(p_action_payload, array[
    'schema_version', 'action_id', 'incident_id', 'action', 'reason_code',
    'expected_status', 'acted_at'
  ]);
  if jsonb_typeof(p_action_payload->'schema_version') <> 'number' then
    raise exception 'incident_action_json_type_invalid' using errcode = '22023';
  end if;
  if coalesce((p_action_payload->>'schema_version')::integer, -1) <> 1 then
    raise exception 'incident_action_schema_version_invalid' using errcode = '22023';
  end if;
  action_id := (p_action_payload->>'action_id')::uuid;
  incident_id_value := (p_action_payload->>'incident_id')::uuid;
  action_value := p_action_payload->>'action';
  expected_status_value := p_action_payload->>'expected_status';
  reason_value := p_action_payload->>'reason_code';
  acted_time := (p_action_payload->>'acted_at')::timestamptz;
  if action_value not in ('acknowledge', 'resolve')
     or expected_status_value not in ('open', 'acknowledged', 'mitigating')
     or acted_time < clock_timestamp() - interval '5 minutes'
     or acted_time > clock_timestamp() + interval '30 seconds'
     or (action_value = 'acknowledge' and reason_value <> 'operator_acknowledged')
     or (action_value = 'resolve' and reason_value <> 'mitigation_verified') then
    raise exception 'incident_action_values_invalid' using errcode = '22023';
  end if;
  select * into incident_row
  from private.incidents
  where id = incident_id_value
  for update;
  if not found or incident_row.status <> expected_status_value then
    raise exception 'incident_action_state_stale' using errcode = '40001';
  end if;
  if action_value = 'acknowledge' then
    actor := private.require_human_roles(array['operator'], true);
    perform private.require_recent_aal2();
    if private.has_active_role(actor, 'platform_admin') then
      raise exception 'platform_admin_incident_action_forbidden' using errcode = '42501';
    end if;
    if incident_row.status <> 'open' then
      raise exception 'incident_not_open' using errcode = '23514';
    end if;
    update private.incidents
    set status = 'acknowledged', acknowledged_at = acted_time,
        acknowledged_by = actor,
        escalation_status = case
          when escalation_status = 'pending' then 'canceled'
          else escalation_status
        end
    where id = incident_id_value
    returning * into incident_row;
    update private.delivery_outbox
    set status = 'canceled', lease_owner = null, lease_expires_at = null
    where aggregate_type = 'incident'
      and aggregate_id = incident_id_value::text
      and event_type = 'critical_incident_ack_escalation'
      and status in ('pending', 'leased');
  else
    actor := private.require_human_roles(array['risk_approver'], true);
    perform private.require_recent_aal2();
    if private.has_active_role(actor, 'platform_admin')
       or incident_row.acknowledged_by = actor then
      raise exception 'incident_resolution_requires_distinct_risk_approver'
        using errcode = '42501';
    end if;
    if incident_row.status <> 'acknowledged' then
      raise exception 'incident_not_acknowledged' using errcode = '23514';
    end if;
    update private.incidents
    set status = 'resolved', resolved_at = acted_time,
        resolved_by = actor, resolution_code = reason_value
    where id = incident_id_value
    returning * into incident_row;
  end if;
  perform private.write_audit_event(
    'human', actor,
    case when action_value = 'acknowledge' then 'operator' else 'risk_approver' end,
    null, null, null,
    'incident_' || case when action_value = 'acknowledge' then 'acknowledged' else 'resolved' end,
    'incident', incident_id_value::text, action_id, null, reason_value, null,
    array['status'], null, null, null
  );
  return jsonb_build_object(
    'schema_version', 1,
    'incident_id', incident_row.id,
    'status', incident_row.status,
    'action_id', action_id,
    'acted_at', acted_time
  );
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'incident_action_values_invalid' using errcode = '22023';
end;
$$;

create or replace function private.operation_command_receipt_v1(p_command_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  command_row private.operation_commands%rowtype;
  external_type text;
  control_state text;
  approver_id uuid;
  ack_event private.operation_command_events%rowtype;
  ack_payload jsonb;
begin
  select * into command_row
  from private.operation_commands
  where id = p_command_id;
  if not found then
    raise exception 'operation_command_not_found' using errcode = 'P0002';
  end if;
  external_type := private.external_command_type_v1(command_row.command_type);
  if external_type is null then
    raise exception 'operation_command_not_in_v1_contract' using errcode = '23514';
  end if;
  control_state := case
    when command_row.state in ('approved', 'claimed', 'applied', 'failed') then 'approved'
    else command_row.state
  end;
  approver_id := case
    when control_state <> 'approved' then null
    when command_row.command_type = 'emergency_stop' then command_row.requester_user_id
    else command_row.reviewer_user_id
  end;

  if command_row.state in ('claimed', 'applied', 'failed') then
    select * into ack_event
    from private.operation_command_events
    where command_id = command_row.id
      and event_type = command_row.state
      and actor_type = 'worker'
    order by occurred_at desc, id desc
    limit 1;
    if ack_event.id is null
       or ack_event.service_principal !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
       or coalesce(ack_event.event_summary->>'release_sha', command_row.target_release_sha, '')
         !~ '^[0-9a-f]{40}([0-9a-f]{24})?$' then
      raise exception 'worker_ack_contract_incomplete' using errcode = '23514';
    end if;
    ack_payload := jsonb_build_object(
      'schema_version', 1,
      'ack_id', ack_event.id,
      'command_id', command_row.id,
      'worker_instance_id', ack_event.service_principal::uuid,
      'worker_release_sha', coalesce(
        ack_event.event_summary->>'release_sha', command_row.target_release_sha
      ),
      'claimed_at', command_row.claimed_at,
      'state', command_row.state,
      'applied_at', case
        when command_row.state = 'claimed' then null
        else command_row.applied_at
      end,
      'post_state_version', case
        when command_row.state = 'claimed' then null
        when command_row.result_summary->>'post_control_epoch' ~ '^[0-9]+$'
          then (command_row.result_summary->>'post_control_epoch')::bigint
        else null
      end,
      'failure_code', case
        when command_row.state = 'failed' then command_row.failure_code
        else null
      end
    );
  else
    ack_payload := null;
  end if;

  return jsonb_build_object(
    'schema_version', 1,
    'command_id', command_row.id,
    'command_type', external_type,
    'environment', command_row.requested_change->>'environment',
    'state', command_row.state,
    'requested_by', private.actor_ref_v1(command_row.requester_user_id),
    'requested_at', command_row.requested_at,
    'expires_at', command_row.expires_at,
    'strategy_version_id', case
      when command_row.requested_change->>'qualification_id' is null then null
      else (command_row.requested_change->>'strategy_version_id')::uuid
    end,
    'risk_policy_version_id', case
      when command_row.requested_change->>'qualification_id' is null then null
      else (command_row.requested_change->>'risk_policy_version_id')::uuid
    end,
    'release_sha', case
      when command_row.requested_change->>'qualification_id' is null then null
      else command_row.requested_change->>'release_sha'
    end,
    'ledger_checkpoint', case
      when command_row.requested_change->>'qualification_id' is null then null
      else command_row.requested_change->>'ledger_checkpoint'
    end,
    'qualification_id', case
      when command_row.requested_change->>'qualification_id' is null then null
      else (command_row.requested_change->>'qualification_id')::uuid
    end,
    'command_hash', command_row.command_sha256,
    'control_plane_receipt', jsonb_build_object(
      'schema_version', 1,
      'receipt_id', command_row.id,
      'command_id', command_row.id,
      'state', control_state,
      'revision', command_row.revision,
      'persisted_at', coalesce(command_row.reviewed_at, command_row.requested_at),
      'approved_at', case when control_state = 'approved' then command_row.reviewed_at else null end,
      'approved_by', case when approver_id is null then null else private.actor_ref_v1(approver_id) end
    ),
    'worker_ack', ack_payload
  );
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'operation_command_receipt_contract_invalid' using errcode = '23514';
end;
$$;

create or replace function private.request_operation_command_v1_impl(
  p_request_payload jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid;
  request_id uuid;
  command_type_external text;
  command_type_internal text;
  environment_value text;
  requested_time timestamptz;
  expiry_time timestamptz;
  expected_epoch bigint;
  account_key text;
  command_hash text;
  draft_payload jsonb;
  qualification_row private.qualifications%rowtype;
  evidence_ref uuid;
  target_release text;
  command_change jsonb;
  existing_row private.operation_commands%rowtype;
  action_time timestamptz := clock_timestamp();
  resulting_epoch bigint;
begin
  if coalesce(jsonb_typeof(p_request_payload), '') <> 'object' then
    raise exception 'operation_command_request_object_required' using errcode = '22023';
  end if;
  command_type_external := p_request_payload->>'command_type';
  command_type_internal := private.internal_command_type_v1(command_type_external);
  if command_type_internal is null then
    raise exception 'operation_command_type_invalid' using errcode = '22023';
  end if;

  if command_type_external = 'emergency_stop' then
    perform private.assert_exact_json_keys(p_request_payload, array[
      'schema_version', 'request_id', 'environment', 'idempotency_key',
      'expected_state_version', 'requested_at', 'expires_at', 'command_type',
      'reason_code', 'assurance_level', 'actor_role', 'single_actor'
    ]);
  else
    draft_payload := private.command_draft_v1(p_request_payload);
    perform private.validate_command_draft_v1(
      'request', command_type_external, draft_payload
    );
    if command_type_external = 'pause_paper' then
      perform private.assert_exact_json_keys(p_request_payload, array[
        'schema_version', 'request_id', 'environment', 'idempotency_key',
        'expected_state_version', 'requested_at', 'expires_at', 'command_type',
        'reason_code', 'step_up_grant_id', 'command_hash',
        'step_up_grant_issued_at', 'step_up_grant_expires_at',
        'step_up_grant_one_time', 'step_up_grant_consumed_at',
        'bound_action', 'bound_command_type'
      ]);
    else
      perform private.assert_exact_json_keys(p_request_payload, array[
        'schema_version', 'request_id', 'environment', 'idempotency_key',
        'expected_state_version', 'requested_at', 'expires_at', 'command_type',
        'qualification_id', 'strategy_version_id', 'risk_policy_version_id',
        'release_sha', 'ledger_checkpoint', 'reason_code',
        'step_up_grant_id', 'command_hash', 'step_up_grant_issued_at',
        'step_up_grant_expires_at', 'step_up_grant_one_time',
        'step_up_grant_consumed_at', 'bound_action', 'bound_command_type'
      ]);
    end if;
  end if;

  if coalesce((p_request_payload->>'schema_version')::integer, -1) <> 1 then
    raise exception 'operation_command_schema_version_invalid' using errcode = '22023';
  end if;
  if jsonb_typeof(p_request_payload->'schema_version') <> 'number'
     or jsonb_typeof(p_request_payload->'expected_state_version') <> 'number'
     or (
       command_type_external = 'emergency_stop'
       and jsonb_typeof(p_request_payload->'single_actor') <> 'boolean'
     )
     or (
       command_type_external <> 'emergency_stop'
       and (
         jsonb_typeof(p_request_payload->'step_up_grant_one_time') <> 'boolean'
         or jsonb_typeof(p_request_payload->'step_up_grant_consumed_at') <> 'null'
       )
     ) then
    raise exception 'operation_command_json_type_invalid' using errcode = '22023';
  end if;
  request_id := (p_request_payload->>'request_id')::uuid;
  environment_value := p_request_payload->>'environment';
  requested_time := (p_request_payload->>'requested_at')::timestamptz;
  expiry_time := (p_request_payload->>'expires_at')::timestamptz;
  expected_epoch := (p_request_payload->>'expected_state_version')::bigint;
  if environment_value not in ('paper', 'contract_test')
     or expected_epoch < 0
     or requested_time < action_time - interval '5 minutes'
     or requested_time > action_time + interval '30 seconds'
     or expiry_time <= requested_time
     or expiry_time > requested_time + interval '24 hours'
     or p_request_payload->>'idempotency_key'
       !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$' then
    raise exception 'operation_command_request_values_invalid' using errcode = '22023';
  end if;
  select account_id into account_key
  from private.trading_accounts
  where environment = environment_value
  order by account_id
  limit 1;
  if account_key is null then
    raise exception 'operation_command_account_not_found' using errcode = 'P0002';
  end if;

  if command_type_external = 'emergency_stop' then
    actor := private.require_human_roles(array['operator'], true);
    perform private.require_recent_aal2();
    if p_request_payload->>'reason_code' not in ('operator_safety_stop', 'incident_response')
       or p_request_payload->>'assurance_level' <> 'aal2'
       or p_request_payload->>'actor_role' <> 'operator'
       or coalesce((p_request_payload->>'single_actor')::boolean, false) is not true then
      raise exception 'emergency_stop_contract_invalid' using errcode = '22023';
    end if;
    if private.has_active_role(actor, 'platform_admin') then
      raise exception 'platform_admin_trading_action_forbidden' using errcode = '42501';
    end if;
    command_change := p_request_payload || jsonb_build_object('account_id', account_key);
    insert into private.operation_commands (
      id, command_type, state, requested_change, revision,
      requester_user_id, reviewer_user_id, requested_at, reviewed_at,
      expires_at, idempotency_key
    ) values (
      request_id, 'emergency_stop', 'approved', command_change, 1,
      actor, null, requested_time, action_time, expiry_time,
      p_request_payload->>'idempotency_key'
    ) on conflict (idempotency_key) do nothing;
    if not found then
      select * into existing_row from private.operation_commands
      where idempotency_key = p_request_payload->>'idempotency_key';
      if existing_row.id <> request_id
         or existing_row.command_type <> 'emergency_stop'
         or existing_row.requested_change <> command_change then
        raise exception 'operation_command_idempotency_conflict' using errcode = '23505';
      end if;
      return private.operation_command_receipt_v1(existing_row.id);
    end if;
    update private.execution_controls
    set execution_enabled = false,
        control_epoch = control_epoch + 1,
        effective_at = action_time,
        expires_at = greatest(expires_at, action_time + interval '1 second'),
        last_command_id = request_id,
        updated_reason_code = p_request_payload->>'reason_code',
        updated_at = action_time
    where account_id = account_key and control_epoch = expected_epoch
    returning control_epoch into resulting_epoch;
    if resulting_epoch is null then
      raise exception 'operation_command_stale_state_version' using errcode = '40001';
    end if;
    update private.operation_commands
    set result_summary = jsonb_build_object('post_control_epoch', resulting_epoch)
    where id = request_id;
    insert into private.operation_command_events (
      command_id, event_type, actor_type, actor_user_id, event_summary, occurred_at
    ) values
      (request_id, 'requested', 'human', actor,
       jsonb_build_object('single_actor', true), requested_time),
      (request_id, 'approved', 'human', actor,
       jsonb_build_object('immediate_emergency_stop', true, 'post_control_epoch', resulting_epoch), action_time);
    perform private.write_audit_event(
      'human', actor, 'operator', null, null, null,
      'command_requested', 'command', request_id::text, request_id,
      request_id, p_request_payload->>'reason_code', null,
      array['execution_enabled', 'control_epoch'], null, null, null
    );
    return private.operation_command_receipt_v1(request_id);
  end if;

  actor := private.require_human_roles(array['operator'], true);
  if private.has_active_role(actor, 'platform_admin') then
    raise exception 'platform_admin_trading_action_forbidden' using errcode = '42501';
  end if;
  command_hash := private.compute_command_sha256(
    'operation_v1:request', draft_payload
  );
  perform private.consume_bound_step_up_v1(
    p_request_payload, 'request', command_type_external,
    requested_time, 'request_operation_command_v1'
  );
  if p_request_payload->>'command_hash' <> command_hash then
    raise exception 'operation_command_hash_mismatch' using errcode = '42501';
  end if;

  if command_type_external = 'pause_paper' then
    if environment_value <> 'paper'
       or p_request_payload->>'reason_code' not in (
         'operator_pause', 'maintenance_window', 'risk_review'
       ) then
      raise exception 'pause_paper_request_invalid' using errcode = '22023';
    end if;
    command_change := draft_payload || jsonb_build_object('account_id', account_key);
  else
    select * into qualification_row
    from private.qualifications
    where id = (p_request_payload->>'qualification_id')::uuid
      and environment = environment_value
      and status = 'qualified'
      and release_sha = p_request_payload->>'release_sha'
      and ledger_checkpoint = p_request_payload->>'ledger_checkpoint'
      and strategy_version_id = (p_request_payload->>'strategy_version_id')::uuid
      and risk_policy_version_id = (p_request_payload->>'risk_policy_version_id')::uuid
      and valid_from <= requested_time
      and valid_until > requested_time
      and g1_status = 'pass'
      and g2_status = 'pass';
    if not found then
      raise exception 'qualified_evidence_bundle_required' using errcode = '23514';
    end if;
    if (command_type_external = 'resume_paper' and environment_value <> 'paper')
       or (command_type_external = 'start_contract_test' and environment_value <> 'contract_test')
       or p_request_payload->>'reason_code' not in (
         'qualified_resume', 'approved_change', 'boundary_verification'
       ) then
      raise exception 'qualified_command_request_invalid' using errcode = '22023';
    end if;
    evidence_ref := qualification_row.g2_evidence_id;
    target_release := qualification_row.release_sha;
    command_change := draft_payload || jsonb_build_object(
      'account_id', account_key,
      'execution_policy_version', qualification_row.execution_policy_version,
      'execution_policy_sha256', qualification_row.execution_policy_sha256,
      'risk_policy_sha256', qualification_row.risk_policy_sha256,
      'provider_contract_version', qualification_row.provider_contract_version,
      'provider_openapi_sha256', qualification_row.provider_openapi_sha256
    );
  end if;
  if not exists (
    select 1 from private.execution_controls
    where account_id = account_key and control_epoch = expected_epoch
  ) then
    raise exception 'operation_command_stale_state_version' using errcode = '40001';
  end if;

  insert into private.operation_commands (
    id, command_type, state, requested_change, command_sha256, revision,
    evidence_id, target_release_sha, requester_user_id, requested_at,
    expires_at, idempotency_key
  ) values (
    request_id, command_type_internal, 'requested', command_change,
    command_hash, 0, evidence_ref, target_release, actor, requested_time,
    expiry_time, p_request_payload->>'idempotency_key'
  ) on conflict (idempotency_key) do nothing;
  if not found then
    select * into existing_row from private.operation_commands
    where idempotency_key = p_request_payload->>'idempotency_key';
    if existing_row.id <> request_id
       or existing_row.command_type <> command_type_internal
       or existing_row.command_sha256 <> command_hash
       or existing_row.requested_change <> command_change then
      raise exception 'operation_command_idempotency_conflict' using errcode = '23505';
    end if;
    return private.operation_command_receipt_v1(existing_row.id);
  end if;
  insert into private.operation_command_events (
    command_id, event_type, actor_type, actor_user_id, event_summary, occurred_at
  ) values (
    request_id, 'requested', 'human', actor,
    jsonb_build_object('command_hash', command_hash), requested_time
  );
  perform private.write_audit_event(
    'human', actor, 'operator', null, null, target_release,
    'command_requested', 'command', request_id::text, request_id,
    request_id, p_request_payload->>'reason_code', null,
    array['state', 'command_sha256'], null, command_hash, evidence_ref
  );
  return private.operation_command_receipt_v1(request_id);
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'operation_command_request_values_invalid' using errcode = '22023';
end;
$$;

create or replace function private.external_command_type_v1(p_internal_type text)
returns text
language sql
immutable
security definer
set search_path = ''
as $$
  select case p_internal_type
    when 'emergency_stop' then 'emergency_stop'
    when 'pause_paper' then 'pause_paper'
    when 'paper_resume' then 'resume_paper'
    when 'strategy_promotion' then 'activate_paper_strategy'
    when 'contract_test_enable' then 'start_contract_test'
    when 'risk_policy_change' then 'apply_risk_policy_version'
    when 'unknown_resolution' then 'resolve_unknown_execution'
    else null
  end;
$$;

create or replace function private.internal_command_type_v1(p_external_type text)
returns text
language sql
immutable
security definer
set search_path = ''
as $$
  select case p_external_type
    when 'emergency_stop' then 'emergency_stop'
    when 'pause_paper' then 'pause_paper'
    when 'resume_paper' then 'paper_resume'
    when 'activate_paper_strategy' then 'strategy_promotion'
    when 'start_contract_test' then 'contract_test_enable'
    when 'apply_risk_policy_version' then 'risk_policy_change'
    when 'resolve_unknown_execution' then 'unknown_resolution'
    else null
  end;
$$;

create or replace function private.actor_ref_v1(p_user_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  actor_roles text[];
begin
  if p_user_id is null then
    return null;
  end if;
  select coalesce(array_agg(role order by role), array[]::text[])
  into actor_roles
  from private.role_assignments
  where user_id = p_user_id
    and revoked_at is null
    and valid_from <= clock_timestamp()
    and (valid_until is null or valid_until > clock_timestamp());
  if cardinality(actor_roles) = 0 then
    select coalesce(array_agg(distinct role order by role), array['viewer']::text[])
    into actor_roles
    from private.role_assignments
    where user_id = p_user_id;
  end if;
  return jsonb_build_object(
    'actor_id', p_user_id,
    'display_name', 'user-' || left(p_user_id::text, 8),
    'roles', to_jsonb(actor_roles)
  );
end;
$$;

create or replace function private.command_draft_v1(p_payload jsonb)
returns jsonb
language sql
immutable
security definer
set search_path = ''
as $$
  select p_payload - array[
    'step_up_grant_id', 'command_hash', 'step_up_grant_issued_at',
    'step_up_grant_expires_at', 'step_up_grant_one_time',
    'step_up_grant_consumed_at', 'bound_action', 'bound_command_type'
  ];
$$;

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
       and jsonb_typeof(p_payload->'expected_receipt_revision') <> 'number'
     )
     or (
       p_action = 'request'
       and p_command_type = 'resolve_unknown_execution'
       and jsonb_typeof(p_payload->'expected_break_revision') <> 'number'
     )
     or (
       p_action = 'request'
       and p_command_type <> 'resolve_unknown_execution'
       and jsonb_typeof(p_payload->'expected_state_version') <> 'number'
     )
     or (
       p_action = 'review'
       and p_command_type = 'resolve_unknown_execution'
       and jsonb_typeof(p_payload->'expected_break_revision') <> 'number'
     ) then
    raise exception 'step_up_draft_json_type_invalid' using errcode = '22023';
  end if;
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'step_up_draft_schema_invalid' using errcode = '22023';
end;
$$;

create or replace function private.issue_step_up_grant_v1_impl(p_request_payload jsonb)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  result_row record;
  command_payload jsonb;
  command_hash text;
  action_name text;
  command_type_name text;
  issued_time timestamptz;
begin
  perform private.assert_exact_json_keys(p_request_payload, array[
    'schema_version', 'bound_action', 'bound_command_type', 'command_payload'
  ]);
  if jsonb_typeof(p_request_payload->'schema_version') <> 'number' then
    raise exception 'step_up_request_json_type_invalid' using errcode = '22023';
  end if;
  if coalesce((p_request_payload->>'schema_version')::integer, -1) <> 1 then
    raise exception 'step_up_request_schema_version_invalid' using errcode = '22023';
  end if;
  action_name := p_request_payload->>'bound_action';
  command_type_name := p_request_payload->>'bound_command_type';
  command_payload := p_request_payload->'command_payload';
  perform private.validate_command_draft_v1(
    action_name, command_type_name, command_payload
  );
  command_hash := private.compute_command_sha256(
    'operation_v1:' || action_name, command_payload
  );
  select * into result_row from private.issue_step_up_grant(command_hash);
  update private.step_up_grants
  set bound_action = action_name,
      bound_command_type = command_type_name
  where id = result_row.grant_id
  returning issued_at into issued_time;
  return jsonb_build_object(
    'schema_version', 1,
    'step_up_grant_id', result_row.grant_id,
    'command_hash', command_hash,
    'step_up_grant_issued_at', issued_time,
    'step_up_grant_expires_at', result_row.expires_at,
    'step_up_grant_one_time', true,
    'step_up_grant_consumed_at', null,
    'bound_action', action_name,
    'bound_command_type', command_type_name
  );
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'step_up_request_schema_version_invalid' using errcode = '22023';
end;
$$;

create or replace function private.consume_bound_step_up_v1(
  p_payload jsonb,
  p_action text,
  p_command_type text,
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
    'operation_v1:' || p_action,
    private.command_draft_v1(p_payload)
  );
  grant_id := (p_payload->>'step_up_grant_id')::uuid;
  if p_payload->>'command_hash' is distinct from expected_hash
     or p_payload->>'bound_action' is distinct from p_action
     or p_payload->>'bound_command_type' is distinct from p_command_type
     or jsonb_typeof(p_payload->'step_up_grant_one_time') <> 'boolean'
     or (p_payload->'step_up_grant_one_time')::boolean is not true
     or jsonb_typeof(p_payload->'step_up_grant_consumed_at') <> 'null' then
    raise exception 'step_up_binding_payload_invalid' using errcode = '42501';
  end if;
  update private.step_up_grants
  set consumed_at = clock_timestamp(), consumed_for = p_consumed_for
  where id = grant_id
    and user_id = actor
    and command_sha256 = expected_hash
    and bound_action = p_action
    and bound_command_type = p_command_type
    and issued_at = (p_payload->>'step_up_grant_issued_at')::timestamptz
    and expires_at = (p_payload->>'step_up_grant_expires_at')::timestamptz
    and expires_at > clock_timestamp()
    and session_binding_sha256 = private.current_session_binding_sha256()
    and consumed_at is null;
  if not found then
    raise exception 'step_up_grant_invalid_expired_or_consumed' using errcode = '42501';
  end if;
  return actor;
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'step_up_binding_payload_invalid' using errcode = '42501';
end;
$$;

create or replace function api.issue_step_up_grant_v1(request_payload jsonb)
returns jsonb
language sql
security invoker
set search_path = ''
as $$ select private.issue_step_up_grant_v1_impl(request_payload); $$;

create or replace function api.request_operation_command_v1(request_payload jsonb)
returns jsonb
language sql
security invoker
set search_path = ''
as $$ select private.request_operation_command_v1_impl(request_payload); $$;

create or replace function api.review_operation_command_v1(review_payload jsonb)
returns jsonb
language sql
security invoker
set search_path = ''
as $$ select private.review_operation_command_v1_impl(review_payload); $$;

create or replace function api.act_on_operation_incident_v1(action_payload jsonb)
returns jsonb
language sql
security invoker
set search_path = ''
as $$ select private.act_on_operation_incident_v1_impl(action_payload); $$;

revoke execute on function
  private.assert_exact_json_keys(jsonb, text[]),
  private.external_command_type_v1(text),
  private.internal_command_type_v1(text),
  private.actor_ref_v1(uuid),
  private.command_draft_v1(jsonb),
  private.validate_command_draft_v1(text, text, jsonb),
  private.consume_bound_step_up_v1(jsonb, text, text, timestamptz, text),
  private.operation_command_receipt_v1(uuid),
  private.command_review_v1(uuid),
  private.issue_step_up_grant_v1_impl(jsonb),
  private.request_operation_command_v1_impl(jsonb),
  private.review_operation_command_v1_impl(jsonb),
  private.act_on_operation_incident_v1_impl(jsonb)
from public, anon, authenticated, service_role;

grant execute on function
  private.issue_step_up_grant_v1_impl(jsonb),
  private.request_operation_command_v1_impl(jsonb),
  private.review_operation_command_v1_impl(jsonb),
  private.act_on_operation_incident_v1_impl(jsonb)
to authenticated;

grant execute on function
  api.issue_step_up_grant_v1(jsonb),
  api.request_operation_command_v1(jsonb),
  api.review_operation_command_v1(jsonb),
  api.act_on_operation_incident_v1(jsonb)
to authenticated;

do $$
begin
  if exists (
    select 1
    from pg_proc as proc
    join pg_namespace as namespace on namespace.oid = proc.pronamespace
    where namespace.nspname in ('api', 'worker_api')
      and proc.prosecdef is true
  ) then
    raise exception 'exposed_schema_security_definer_forbidden';
  end if;
end $$;

create or replace function private.get_desktop_operations_snapshot_v1_impl()
returns jsonb
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  actor uuid;
  roles text[];
  permissions text[] := array[]::text[];
  now_value timestamptz := clock_timestamp();
  environment_value text;
  control_row private.execution_controls%rowtype;
  heartbeat_time timestamptz;
  heartbeat_release text;
  heartbeat_status text;
  heartbeat_worker_id text;
  heartbeat_component text;
  heartbeat_checkpoint text;
  heartbeat_completed_at text;
  active_lease_holder text;
  active_lease_release text;
  active_lease_expires_at timestamptz;
  runtime_state text;
  worker_state text;
  contract_state text;
  grant_rows jsonb;
  qualification_value jsonb;
  commands_value jsonb;
  pending_reviews_value jsonb;
  reviews_value jsonb;
  access_changes_value jsonb;
  incidents_value jsonb;
  orders_value jsonb;
  positions_value jsonb;
  audit_value jsonb;
  reconciliation_value jsonb;
begin
  actor := private.require_human_roles(
    array[
      'platform_admin', 'operator', 'risk_approver', 'strategy_reviewer',
      'auditor', 'release_manager', 'viewer'
    ],
    true
  );
  select coalesce(array_agg(role order by role), array[]::text[])
  into roles
  from private.role_assignments
  where user_id = actor
    and revoked_at is null
    and valid_from <= now_value
    and (valid_until is null or valid_until > now_value);
  if roles && array['operator']::text[]
     and not roles && array['platform_admin']::text[] then
    permissions := permissions || array['request_command', 'acknowledge_incident'];
  end if;
  if roles && array['risk_approver', 'strategy_reviewer', 'release_manager']::text[]
     and not roles && array['platform_admin']::text[] then
    permissions := permissions || array['review_command'];
  end if;
  if roles && array['risk_approver']::text[]
     and not roles && array['platform_admin']::text[] then
    permissions := permissions || array['resolve_incident'];
  end if;
  if roles && array['auditor']::text[] then
    permissions := permissions || array['view_audit', 'view_reconciliation'];
  end if;

  select environment into environment_value
  from private.environment_policy where id = 'singleton';
  select * into control_row
  from private.execution_controls
  where environment = environment_value
  order by account_id
  limit 1;
  select lease.holder_id, lease.release_sha, lease.expires_at
  into active_lease_holder, active_lease_release, active_lease_expires_at
  from private.worker_leases as lease
  where lease.account_id = control_row.account_id
    and lease.expires_at > now_value
  limit 1;
  select
    heartbeat.created_at,
    case
      when heartbeat.details->>'release_sha' ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
        then heartbeat.details->>'release_sha'
      else null
    end,
    heartbeat.status,
    case
      when heartbeat.worker_name like 'trading-worker:%'
        then substr(heartbeat.worker_name, length('trading-worker:') + 1)
      else null
    end,
    heartbeat.details->>'component',
    heartbeat.details->>'checkpoint',
    heartbeat.details->>'completed_at'
  into heartbeat_time, heartbeat_release, heartbeat_status,
    heartbeat_worker_id, heartbeat_component, heartbeat_checkpoint,
    heartbeat_completed_at
  from public.worker_heartbeats as heartbeat
  order by heartbeat.created_at desc
  limit 1;
  worker_state := case
    when heartbeat_time is null then 'offline'
    when heartbeat_status <> 'ok' then 'degraded'
    when active_lease_holder is null then 'offline'
    when heartbeat_worker_id is distinct from active_lease_holder
      or heartbeat_release is distinct from active_lease_release
      or heartbeat_component not in ('operations_v2', 'trading_cycle')
      or heartbeat_checkpoint not in ('cycle_completed', 'operations_completed')
      or nullif(heartbeat_completed_at, '') is null then 'degraded'
    when active_lease_expires_at <= now_value then 'offline'
    when heartbeat_time < now_value - interval '120 seconds' then 'stale'
    else 'fresh'
  end;
  contract_state := case
    when environment_value = 'paper' then 'fresh'
    when control_row.provider_contract_version is null
      or control_row.provider_openapi_sha256 is null then 'contract_error'
    when exists (
      select 1 from private.provider_contract_registry as contract
      where contract.provider = 'toss'
        and contract.qualification_environment = 'contract_test'
        and contract.execution_transport = 'local_contract_simulator'
        and contract.contract_version = control_row.provider_contract_version
        and contract.openapi_sha256 = control_row.provider_openapi_sha256
        and contract.status = 'approved'
        and contract.effective_from <= now_value
        and contract.effective_until > now_value
    ) then 'fresh'
    else 'contract_error'
  end;
  runtime_state := case
    when control_row.account_id is null then 'contract_error'
    when contract_state = 'contract_error' then 'contract_error'
    when worker_state in ('offline', 'stale') then worker_state
    when worker_state = 'degraded'
      or exists (select 1 from private.incidents where status <> 'resolved')
      or exists (select 1 from private.reconciliation_breaks where state <> 'resolved')
      then 'degraded'
    else 'fresh'
  end;

  select coalesce(jsonb_agg(jsonb_build_object(
    'step_up_grant_id', grant_row.id,
    'command_hash', grant_row.command_sha256,
    'step_up_grant_issued_at', grant_row.issued_at,
    'step_up_grant_expires_at', grant_row.expires_at,
    'step_up_grant_one_time', true,
    'step_up_grant_consumed_at', null,
    'bound_action', grant_row.bound_action,
    'bound_command_type', grant_row.bound_command_type
  ) order by grant_row.expires_at), '[]'::jsonb)
  into grant_rows
  from private.step_up_grants as grant_row
  where grant_row.user_id = actor
    and grant_row.consumed_at is null
    and grant_row.expires_at > now_value
    and grant_row.bound_action in ('request', 'review')
    and grant_row.bound_command_type in (
      'pause_paper', 'resume_paper', 'activate_paper_strategy',
      'start_contract_test', 'apply_risk_policy_version'
    );

  select jsonb_build_object(
    'schema_version', 1,
    'qualification_id', qualification.id,
    'environment', qualification.environment,
    'status', qualification.status,
    'release_sha', qualification.release_sha,
    'ledger_checkpoint', qualification.ledger_checkpoint,
    'dataset_version', qualification.dataset_version,
    'strategy_version_id', qualification.strategy_version_id,
    'risk_policy_version_id', qualification.risk_policy_version_id,
    'valid_from', qualification.valid_from,
    'valid_until', qualification.valid_until,
    'g1', jsonb_build_object(
      'status', qualification.g1_status,
      'checked_at', qualification.g1_checked_at,
      'evidence_ref', qualification.g1_evidence_id::text
    ),
    'g2', jsonb_build_object(
      'status', qualification.g2_status,
      'checked_at', qualification.g2_checked_at,
      'evidence_ref', qualification.g2_evidence_id::text
    )
  ) into qualification_value
  from private.qualifications as qualification
  where qualification.environment = environment_value
  order by
    (qualification.status = 'qualified' and qualification.valid_until > now_value) desc,
    qualification.valid_until desc,
    qualification.created_at desc
  limit 1;

  select coalesce(jsonb_agg(
    private.operation_command_receipt_v1(command.id)
    order by command.requested_at desc, command.id desc
  ), '[]'::jsonb)
  into commands_value
  from (
    select * from private.operation_commands
    where private.external_command_type_v1(command_type) is not null
    order by requested_at desc, id desc
    limit 100
  ) as command;
  select coalesce(jsonb_agg(
    private.operation_command_receipt_v1(command.id)
    order by command.requested_at, command.id
  ), '[]'::jsonb)
  into pending_reviews_value
  from private.operation_commands as command
  where command.state = 'requested'
    and private.external_command_type_v1(command.command_type) is not null;
  select coalesce(jsonb_agg(
    private.command_review_v1(review.id)
    order by review.reviewed_at desc, review.id desc
  ), '[]'::jsonb)
  into reviews_value
  from (
    select review.*
    from private.operation_command_reviews as review
    join private.operation_commands as command on command.id = review.command_id
    where private.external_command_type_v1(command.command_type) is not null
    order by review.reviewed_at desc, review.id desc
    limit 100
  ) as review;

  select coalesce(jsonb_agg(jsonb_build_object(
    'schema_version', 1,
    'request_id', change_request.id,
    'subject_user_id', change_request.subject_user_id,
    'requested_role', change_request.requested_role,
    'change_type', change_request.change_type,
    'state', case change_request.state
      when 'approved' then 'applied'
      else change_request.state
    end,
    'requested_by', private.actor_ref_v1(change_request.requester_user_id),
    'reviewed_by', private.actor_ref_v1(change_request.reviewer_user_id),
    'requested_at', change_request.requested_at,
    'reviewed_at', change_request.reviewed_at,
    'applied_at', change_request.applied_at,
    'expires_at', change_request.expires_at,
    'reason_code', change_request.reason_code
  ) order by change_request.requested_at desc, change_request.id desc), '[]'::jsonb)
  into access_changes_value
  from (
    select * from private.access_change_requests
    order by requested_at desc, id desc
    limit 100
  ) as change_request;

  select coalesce(jsonb_agg(jsonb_build_object(
    'schema_version', 1,
    'incident_id', incident.id,
    'severity', case incident.severity
      when 'critical' then 'sev1' when 'high' then 'sev2' else 'sev3' end,
    'status', incident.status,
    'kind', case
      when incident.incident_type like '%reconcil%' or incident.incident_type like '%execution%'
        then 'reconciliation_required'
      when incident.incident_type like '%access%' then 'access_violation'
      when incident.incident_type like '%worker%' then 'worker_offline'
      when incident.incident_type like '%timeout%' then 'command_timeout'
      when incident.incident_type like '%stale%' then 'stale_data'
      else 'contract_error'
    end,
    'title', left(replace(incident.incident_type, '_', ' '), 160),
    'summary', left(incident.summary_code, 500),
    'detected_at', incident.opened_at,
    'acknowledged_at', incident.acknowledged_at,
    'resolved_at', incident.resolved_at,
    'owner', case when incident.acknowledged_by is null then null
      else private.actor_ref_v1(incident.acknowledged_by) end,
    'evidence_refs', jsonb_build_array('incident:' || incident.id::text),
    'ack_due_at', incident.ack_due_at,
    'escalation_status', incident.escalation_status
  ) order by incident.opened_at desc, incident.id desc), '[]'::jsonb)
  into incidents_value
  from (
    select * from private.incidents
    order by opened_at desc, id desc
    limit 100
  ) as incident;

  select coalesce(jsonb_agg(jsonb_build_object(
    'schema_version', 1,
    'order_id', intent.id,
    'command_id', null,
    'environment', intent.environment,
    'symbol', intent.symbol,
    'side', intent.side,
    'order_type', 'limit',
    'requested_quantity', intent.quantity,
    'filled_quantity', coalesce(observation.cumulative_quantity, 0),
    'requested_price_krw', intent.limit_price_krw,
    'average_fill_price_krw', case
      when coalesce(observation.cumulative_quantity, 0) = 0 then null
      else observation.cumulative_gross_krw::numeric / observation.cumulative_quantity
    end,
    'status', case
      when observation.event_type = 'partial_filled' then 'partial_filled'
      when observation.event_type = 'filled' then 'filled'
      when observation.event_type = 'canceled' then 'canceled'
      when observation.event_type = 'rejected' then 'rejected'
      when observation.event_type = 'expired' then 'expired'
      when observation.event_type = 'failed_pre_dispatch' then 'failed'
      when observation.event_type = 'unknown_requires_manual_check' then 'reconciliation_required'
      when observation.id is null then 'proposed'
      when observation.event_type = 'open' and intent.environment = 'paper' then 'paper_simulated'
      when observation.event_type = 'open' and intent.environment = 'contract_test' then 'contract_simulated'
      else 'reconciliation_required'
    end,
    'strategy_version_id', intent.strategy_version_id::uuid,
    'risk_policy_version_id', qualification.risk_policy_version_id,
    'idempotency_key', intent.semantic_key_sha256,
    'created_at', intent.created_at,
    'updated_at', coalesce(observation.observed_at, intent.created_at)
  ) order by intent.created_at desc, intent.id desc), '[]'::jsonb)
  into orders_value
  from private.order_intents as intent
  join private.execution_controls as control on control.account_id = intent.account_id
  join lateral (
    select q.risk_policy_version_id
    from private.qualifications as q
    where q.environment = intent.environment
      and q.strategy_version_id::text = intent.strategy_version_id
      and q.risk_policy_sha256 = intent.risk_policy_sha256
      and q.release_sha = intent.release_sha
    order by q.created_at desc
    limit 1
  ) as qualification on true
  left join lateral (
    select * from private.execution_observations as latest
    where latest.intent_id = intent.id
    order by latest.sequence desc
    limit 1
  ) as observation on true
  where intent.strategy_version_id
    ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$';

  select coalesce(jsonb_agg(jsonb_build_object(
    'schema_version', 1,
    'position_id', md5(position.account_id || ':' || position.symbol)::uuid,
    'environment', account.environment,
    'symbol', position.symbol,
    'quantity', position.quantity,
    'average_price_krw', position.average_cost_krw,
    'market_price_krw', null,
    'market_value_krw', null,
    'unrealized_pnl_krw', null,
    'as_of', position.projected_at,
    'market_data_status', 'unavailable',
    'market_data_source', null,
    'market_data_as_of', null
  ) order by position.symbol), '[]'::jsonb)
  into positions_value
  from private.position_projection as position
  join private.trading_accounts as account using (account_id)
  where account.environment = environment_value and position.quantity > 0;

  select coalesce(jsonb_agg(jsonb_build_object(
    'schema_version', 1,
    'audit_id', event.id,
    'occurred_at', event.occurred_at,
    'actor', case when event.actor_type = 'human'
      then private.actor_ref_v1(event.actor_user_id) else null end,
    'action', case
      when event.action in ('command_requested', 'operation_command_requested') then 'command_requested'
      when event.action like '%review%' or event.action like '%approved%' or event.action like '%rejected%' then 'command_reviewed'
      when event.action like '%claimed%' then 'command_claimed'
      when event.action like '%applied%' then 'command_applied'
      when event.action like '%failed%' then 'command_failed'
      when event.action = 'incident_acknowledged' then 'incident_acknowledged'
      when event.action = 'incident_resolved' then 'incident_resolved'
      when event.action like '%reconciliation%resolved%' then 'reconciliation_resolved'
      when event.action like '%reconciliation%' or event.action like '%quarantined%' then 'reconciliation_opened'
      else null
    end,
    'resource_type', case
      when event.resource_type like '%command%' then 'command'
      when event.resource_type = 'incident' then 'incident'
      when event.resource_type like '%order%' or event.resource_type like '%execution%' then 'order'
      when event.resource_type like '%position%' then 'position'
      when event.resource_type like '%qualification%' then 'qualification'
      else null
    end,
    'resource_id', event.resource_id::uuid,
    'outcome', case
      when event.action like '%failed%' then 'failed'
      when event.action like '%denied%' then 'denied'
      else 'success'
    end,
    'reason_code', left(coalesce(nullif(event.reason_code, ''), 'recorded'), 120),
    'correlation_id', coalesce(event.correlation_id, event.id)
  ) order by event.occurred_at desc, event.id desc), '[]'::jsonb)
  into audit_value
  from (
    select * from private.audit_events
    where resource_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
      and (
        action in ('command_requested', 'operation_command_requested',
          'incident_acknowledged', 'incident_resolved')
        or action like '%review%'
        or action like '%approved%'
        or action like '%rejected%'
        or action like '%claimed%'
        or action like '%applied%'
        or action like '%failed%'
        or action like '%reconciliation%'
        or action like '%quarantined%'
      )
      and (
        resource_type like '%command%'
        or resource_type = 'incident'
        or resource_type like '%order%'
        or resource_type like '%execution%'
        or resource_type like '%position%'
        or resource_type like '%qualification%'
      )
    order by occurred_at desc, id desc
    limit 100
  ) as event;

  select coalesce(jsonb_agg(jsonb_build_object(
    'schema_version', 1,
    'case_id', break_row.id,
    'order_id', event.intent_id,
    'environment', intent.environment,
    'status', case break_row.state
      when 'open' then 'open' when 'resolution_requested' then 'investigating'
      else 'resolved' end,
    'reason_code', case
      when break_row.summary_code like '%fill%' then 'fill_mismatch'
      when break_row.summary_code like '%timeout%' then 'ack_timeout'
      when break_row.summary_code like '%operator%' then 'operator_escalation'
      else 'ambiguous_order_state' end,
    'opened_at', break_row.detected_at,
    'updated_at', coalesce(break_row.resolved_at, break_row.detected_at),
    'owner', null,
    'evidence_refs', jsonb_build_array('reconciliation-run:' || break_row.run_id::text),
    'resolution_code', case when break_row.state = 'resolved'
      then 'manual_follow_up' else null end
  ) order by break_row.detected_at desc, break_row.id desc), '[]'::jsonb)
  into reconciliation_value
  from private.reconciliation_breaks as break_row
  join private.order_events as event
    on event.event_summary->>'reconciliation_break_id' = break_row.id::text
  join private.order_intents as intent on intent.id = event.intent_id;

  return jsonb_build_object(
    'schema_version', 1,
    'generated_at', now_value,
    'runtime_health', jsonb_build_object(
      'schema_version', 1,
      'environment', environment_value,
      'live_permitted', false,
      'overall_state', runtime_state,
      'as_of', now_value,
      'state_version', coalesce(control_row.control_epoch, 0),
      'worker_release_sha', heartbeat_release,
      'worker_heartbeat_at', heartbeat_time,
      'realtime_connected', false,
      'realtime_last_seen_at', null,
      'execution_enabled', coalesce(control_row.execution_enabled, false),
      'active_strategy_version_id', case
        when control_row.active_strategy_version_id
          ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
          then control_row.active_strategy_version_id::uuid else null end,
      'active_risk_policy_version_id', control_row.active_risk_policy_version_id,
      'execution_policy_version', control_row.execution_policy_version,
      'execution_policy_sha256', control_row.execution_policy_sha256,
      'risk_policy_sha256', control_row.risk_policy_sha256,
      'provider_contract_version', control_row.provider_contract_version,
      'provider_openapi_sha256', control_row.provider_openapi_sha256,
      'components', jsonb_build_array(
        jsonb_build_object(
          'schema_version', 1, 'component', 'control_plane',
          'state', case when control_row.account_id is null then 'contract_error' else 'fresh' end,
          'observed_at', now_value,
          'detail_code', case when control_row.account_id is null
            then 'execution_control_missing' else 'control_projection_loaded' end
        ),
        jsonb_build_object(
          'schema_version', 1, 'component', 'worker',
          'state', worker_state,
          'observed_at', coalesce(heartbeat_time, now_value),
          'detail_code', case worker_state
            when 'fresh' then 'heartbeat_fresh'
            when 'stale' then 'heartbeat_stale'
            when 'offline' then 'heartbeat_missing'
            else 'heartbeat_degraded' end
        ),
        jsonb_build_object(
          'schema_version', 1,
          'component', 'realtime',
          'state', 'offline',
          'observed_at', now_value,
          'detail_code', 'canonical_realtime_feed_not_configured'
        ),
        jsonb_build_object(
          'schema_version', 1,
          'component', case when environment_value = 'contract_test'
            then 'contract_test' else 'market_data' end,
          'state', contract_state,
          'observed_at', now_value,
          'detail_code', case when contract_state = 'fresh'
            then 'evidence_boundary_satisfied' else 'provider_contract_pin_invalid' end
        )
      ),
      'freshness_policy', jsonb_build_object(
        'snapshot_max_age_seconds', 60,
        'worker_heartbeat_max_age_seconds', 120,
        'realtime_max_age_seconds', 120
      )
    ),
    'access', jsonb_build_object(
      'schema_version', 1,
      'signed_in', true,
      'actor', private.actor_ref_v1(actor),
      'session_state', 'active',
      'assurance_level', private.current_aal(),
      'active_step_up_grants', grant_rows,
      'permissions', to_jsonb(permissions)
    ),
    'qualification', qualification_value,
    'commands', commands_value,
    'pending_reviews', pending_reviews_value,
    'reviews', reviews_value,
    'access_changes', access_changes_value,
    'incidents', incidents_value,
    'orders', orders_value,
    'positions', positions_value,
    'audit_events', audit_value,
    'reconciliation_cases', reconciliation_value
  );
end;
$$;
