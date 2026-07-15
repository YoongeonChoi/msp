-- Strict Desktop read projection for the reviewed unknown-execution V2
-- workflow.  The source tables remain private and immutable; the Desktop
-- receives only the provider identity, hashes, CAS revisions, proposed fill
-- manifest, and request/review/application receipts needed for maker/checker
-- operation.  Raw provider payloads and account identifiers are not exposed.

begin;

create or replace function private.get_unknown_resolution_cases_v2_impl()
returns jsonb
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  actor uuid;
  now_value timestamptz := clock_timestamp();
  cases_value jsonb := '[]'::jsonb;
begin
  actor := private.require_human_roles(
    array[
      'platform_admin', 'operator', 'risk_approver', 'strategy_reviewer',
      'auditor', 'release_manager', 'viewer'
    ],
    true
  );

  -- Manual execution evidence is available only to the three roles that can
  -- request, review, or audit the workflow.  Other signed-in roles receive an
  -- explicit empty projection rather than source-table access.
  if not exists (
    select 1
    from private.role_assignments as assignment
    where assignment.user_id = actor
      and assignment.role in ('operator', 'risk_approver', 'auditor')
      and assignment.revoked_at is null
      and assignment.valid_from <= now_value
      and (assignment.valid_until is null or assignment.valid_until > now_value)
  ) then
    return jsonb_build_object(
      'schema_version', 2,
      'generated_at', now_value,
      'cases', '[]'::jsonb
    );
  end if;

  -- Do not silently omit a broken manual case.  A missing source projection
  -- makes the whole RPC fail so the Desktop blocks every mutation.
  if exists (
    select 1
    from private.reconciliation_breaks as reconciliation_break
    join private.order_events as unknown_event
      on unknown_event.event_summary->>'reconciliation_break_id'
        = reconciliation_break.id::text
    join private.execution_observations as unknown_observation
      on unknown_observation.id = unknown_event.observation_id
    left join private.order_intents as intent
      on intent.id = unknown_event.intent_id
    left join private.order_attempts as attempt
      on attempt.id = unknown_observation.attempt_id
    left join private.provider_order_bindings as provider_binding
      on provider_binding.attempt_id = attempt.id
    left join private.execution_reconciliation_state as reconciliation
      on reconciliation.intent_id = intent.id
    left join private.execution_controls as control
      on control.account_id = intent.account_id
    left join private.cash_balance_projection as cash_projection
      on cash_projection.account_id = intent.account_id
    left join private.order_reservations as reservation
      on reservation.intent_id = intent.id
    left join lateral (
      select event_sequence
      from private.reservation_events as reservation_event
      where reservation_event.reservation_id = reservation.id
      order by reservation_event.event_sequence desc
      limit 1
    ) as latest_reservation_event on true
    where reconciliation_break.break_type = 'execution'
      and unknown_observation.event_type = 'unknown_requires_manual_check'
      and (
        intent.id is null
        or attempt.id is null
        or provider_binding.attempt_id is null
        or reconciliation.intent_id is null
        or control.account_id is null
        or cash_projection.account_id is null
        or reservation.id is null
        or latest_reservation_event.event_sequence is null
      )
  ) then
    raise exception 'unknown_resolution_desktop_projection_incomplete'
      using errcode = '23514';
  end if;

  select coalesce(
    jsonb_agg(case_row.payload order by case_row.detected_at desc, case_row.break_id desc),
    '[]'::jsonb
  )
  into cases_value
  from (
    select
      reconciliation_break.id as break_id,
      reconciliation_break.detected_at,
      jsonb_build_object(
        'schema_version', 2,
        'break_id', reconciliation_break.id,
        'intent_id', intent.id,
        'environment', intent.environment,
        'symbol', intent.symbol,
        'side', intent.side,
        'requested_quantity', intent.quantity,
        'limit_price_krw', intent.limit_price_krw,
        'break_state', reconciliation_break.state,
        'break_revision', reconciliation_break.revision,
        'break_reason_code', reconciliation_break.summary_code,
        'detected_at', reconciliation_break.detected_at,
        'resolved_at', reconciliation_break.resolved_at,
        'reconciliation_state', reconciliation.state,
        'reconciliation_updated_at', reconciliation.updated_at,
        'cash_projection_version', cash_projection.projection_version,
        'position_projection_version', position_projection.projection_version,
        'reservation_event_sequence', latest_reservation_event.event_sequence,
        'control_epoch', control.control_epoch,
        'provider_identity', jsonb_build_object(
          'schema_version', 2,
          'broker', attempt.broker,
          'provider_order_id', unknown_observation.provider_order_id,
          'provider_execution_id', unknown_observation.provider_execution_id,
          'provider_binding_sha256', provider_binding.binding_sha256,
          'provider_contract_version', intent.provider_contract_version,
          'provider_openapi_sha256', intent.provider_openapi_sha256
        ),
        'unknown_observation', jsonb_build_object(
          'schema_version', 2,
          'observation_id', unknown_observation.id,
          'sequence', unknown_observation.sequence,
          'event_type', unknown_observation.event_type,
          'observed_at', unknown_observation.observed_at,
          'cumulative_quantity', unknown_observation.cumulative_quantity,
          'cumulative_gross_krw', unknown_observation.cumulative_gross_krw,
          'cumulative_commission_krw',
            unknown_observation.cumulative_commission_krw,
          'cumulative_tax_krw', unknown_observation.cumulative_tax_krw,
          'reason_code', unknown_observation.reason_code,
          'observation_sha256', unknown_observation.observation_sha256,
          'provider_observation_sha256',
            unknown_observation.provider_observation_sha256
        ),
        'request', case
          when resolution_request.id is null then null
          else jsonb_build_object(
            'schema_version', 2,
            'request_id', resolution_request.id,
            'state', resolution_command.state,
            'receipt_revision', resolution_command.revision,
            'requested_by',
              private.actor_ref_v1(resolution_request.requester_user_id),
            'requested_at', resolution_request.requested_at,
            'expires_at', resolution_request.expires_at,
            'terminal_status', resolution_request.terminal_status,
            'evidence_artifact_uri',
              resolution_request.evidence_artifact_uri,
            'evidence_sha256', resolution_request.evidence_sha256,
            'evidence_captured_at',
              resolution_request.evidence_captured_at,
            'request_digest_sha256', resolution_request.payload_sha256,
            'expected_break_revision',
              resolution_request.expected_break_revision,
            'expected_cash_projection_version',
              resolution_request.expected_cash_projection_version,
            'expected_position_projection_version',
              resolution_request.expected_position_projection_version,
            'expected_reservation_event_sequence',
              resolution_request.expected_reservation_event_sequence,
            'expected_control_epoch',
              resolution_request.expected_control_epoch,
            'missing_fills', proposed_fills.items
          )
        end,
        'review', case
          when resolution_review.id is null then null
          else jsonb_build_object(
            'schema_version', 2,
            'review_id', resolution_review.id,
            'decision', resolution_review.decision,
            'reason_code', resolution_review.reason_code,
            'reviewed_by',
              private.actor_ref_v1(resolution_review.reviewer_user_id),
            'reviewed_at', resolution_review.reviewed_at,
            'request_digest_sha256',
              resolution_review.expected_request_payload_sha256,
            'review_digest_sha256', resolution_review.payload_sha256,
            'evidence_sha256', resolution_review.evidence_sha256
          )
        end,
        'work_receipt', case
          when work_item.command_id is null then null
          else jsonb_build_object(
            'schema_version', 2,
            'state', work_item.state,
            'work_revision', work_item.revision,
            'claim_token', work_item.claim_token,
            'claimed_at', work_item.claimed_at,
            'claim_expires_at', work_item.claim_expires_at,
            'applied_at', work_item.applied_at,
            'worker_release_sha', work_item.claim_release_sha,
            'fencing_token', work_item.claim_fencing_token
          )
        end,
        'application_receipt', case
          when application.id is null then null
          else jsonb_build_object(
            'schema_version', 2,
            'application_id', application.id,
            'terminal_observation_id', application.terminal_observation_id,
            'terminal_status', application.terminal_status,
            'final_cumulative_quantity',
              application.final_cumulative_quantity,
            'final_cumulative_gross_krw',
              application.final_cumulative_gross_krw,
            'final_cumulative_commission_krw',
              application.final_cumulative_commission_krw,
            'final_cumulative_tax_krw',
              application.final_cumulative_tax_krw,
            'command_revision', application.command_revision,
            'work_revision', application.work_revision,
            'applied_at', application.applied_at,
            'application_sha256', application.application_sha256
          )
        end,
        'postcondition', jsonb_build_object(
          'schema_version', 2,
          'resolution_complete',
            reconciliation_break.state = 'resolved'
            and reconciliation.state = 'complete'
            and resolution_command.state = 'applied'
            and application.id is not null,
          'accounting_application_recorded', application.id is not null
        )
      ) as payload
    from private.reconciliation_breaks as reconciliation_break
    join lateral (
      select event.*
      from private.order_events as event
      join private.execution_observations as observation
        on observation.id = event.observation_id
      where event.event_summary->>'reconciliation_break_id'
          = reconciliation_break.id::text
        and observation.event_type = 'unknown_requires_manual_check'
      order by event.occurred_at desc, event.id desc
      limit 1
    ) as unknown_event on true
    join private.execution_observations as unknown_observation
      on unknown_observation.id = unknown_event.observation_id
    join private.order_intents as intent on intent.id = unknown_event.intent_id
    join private.order_attempts as attempt
      on attempt.id = unknown_observation.attempt_id
    join private.provider_order_bindings as provider_binding
      on provider_binding.attempt_id = attempt.id
    join private.execution_reconciliation_state as reconciliation
      on reconciliation.intent_id = intent.id
    join private.execution_controls as control
      on control.account_id = intent.account_id
    join private.cash_balance_projection as cash_projection
      on cash_projection.account_id = intent.account_id
    left join private.position_projection as position_projection
      on position_projection.account_id = intent.account_id
      and position_projection.symbol = intent.symbol
    join private.order_reservations as reservation
      on reservation.intent_id = intent.id
    join lateral (
      select reservation_event.event_sequence
      from private.reservation_events as reservation_event
      where reservation_event.reservation_id = reservation.id
      order by reservation_event.event_sequence desc
      limit 1
    ) as latest_reservation_event on true
    left join lateral (
      select request.*
      from private.unknown_execution_resolution_requests_v2 as request
      where request.break_id = reconciliation_break.id
      order by request.requested_at desc, request.id desc
      limit 1
    ) as resolution_request on true
    left join private.operation_commands as resolution_command
      on resolution_command.id = resolution_request.command_id
    left join private.unknown_execution_resolution_reviews_v2
      as resolution_review
      on resolution_review.request_id = resolution_request.id
    left join private.unknown_execution_resolution_work_items_v2 as work_item
      on work_item.request_id = resolution_request.id
    left join private.unknown_execution_resolution_applications_v2
      as application
      on application.request_id = resolution_request.id
    left join lateral (
      select coalesce(jsonb_agg(jsonb_build_object(
        'schema_version', 2,
        'fill_sequence', proposal.fill_sequence,
        'provider_order_id', proposal.provider_order_id,
        'provider_execution_id', proposal.provider_execution_id,
        'quantity', proposal.quantity,
        'price_krw', proposal.price_krw,
        'commission_krw', proposal.commission_krw,
        'tax_krw', proposal.tax_krw,
        'filled_at', proposal.filled_at,
        'settlement_date', proposal.settlement_date,
        'evidence_sha256', proposal.evidence_sha256,
        'proposal_sha256', proposal.proposal_sha256
      ) order by proposal.fill_sequence), '[]'::jsonb) as items
      from private.unknown_execution_resolution_fill_proposals_v2 as proposal
      where proposal.request_id = resolution_request.id
    ) as proposed_fills on true
    where reconciliation_break.break_type = 'execution'
    order by reconciliation_break.detected_at desc, reconciliation_break.id desc
    limit 100
  ) as case_row;

  return jsonb_build_object(
    'schema_version', 2,
    'generated_at', now_value,
    'cases', cases_value
  );
end;
$$;

create or replace function api.get_unknown_resolution_cases_v2()
returns jsonb
language sql
security invoker
set search_path = ''
as $$
  select private.get_unknown_resolution_cases_v2_impl();
$$;

revoke execute on function
  private.get_unknown_resolution_cases_v2_impl(),
  api.get_unknown_resolution_cases_v2()
from public, anon, authenticated, service_role;

grant execute on function private.get_unknown_resolution_cases_v2_impl()
to authenticated;
grant execute on function api.get_unknown_resolution_cases_v2()
to authenticated;

do $$
begin
  if exists (
    select 1
    from pg_catalog.pg_proc as procedure
    join pg_catalog.pg_namespace as namespace
      on namespace.oid = procedure.pronamespace
    where namespace.nspname = 'api'
      and procedure.proname = 'get_unknown_resolution_cases_v2'
      and procedure.prosecdef is true
  ) then
    raise exception 'unknown_resolution_projection_api_must_be_invoker';
  end if;
end;
$$;

commit;
