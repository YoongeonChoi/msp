-- G1+G2 private platform foundation.
--
-- The Supabase CLI is unavailable in the approved local workspace. This
-- migration therefore uses the repository's established sequential SQL-file
-- convention. It is additive and must be applied by the normal reviewed
-- migration path; do not paste only selected statements into a hosted project.

create schema if not exists private;
create schema if not exists api;
create schema if not exists worker_api;

revoke all on schema private from public, anon, authenticated, service_role;
revoke all on schema api from public, anon, authenticated, service_role;
revoke all on schema worker_api from public, anon, authenticated, service_role;

alter default privileges in schema private
  revoke select, insert, update, delete on tables from public, anon, authenticated, service_role;
alter default privileges in schema private
  revoke usage, select, update on sequences from public, anon, authenticated, service_role;
alter default privileges in schema private
  revoke execute on functions from public, anon, authenticated, service_role;
alter default privileges in schema api
  revoke select, insert, update, delete on tables from public, anon, authenticated, service_role;
alter default privileges in schema api
  revoke execute on functions from public, anon, authenticated, service_role;
alter default privileges in schema worker_api
  revoke select, insert, update, delete on tables from public, anon, authenticated, service_role;
alter default privileges in schema worker_api
  revoke execute on functions from public, anon, authenticated, service_role;

create table private.environment_policy (
  id text primary key default 'singleton' check (id = 'singleton'),
  account_scope text not null default 'single_entity_proprietary'
    check (account_scope = 'single_entity_proprietary'),
  environment text not null default 'paper'
    check (environment in ('paper', 'contract_test')),
  provider_connectivity text not null default 'disabled'
    check (provider_connectivity in ('disabled', 'local_contract_simulator')),
  production_live_enabled boolean not null default false
    check (production_live_enabled is false),
  production_order_credentials_present boolean not null default false
    check (production_order_credentials_present is false),
  updated_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  constraint environment_policy_environment_pair_check check (
    (environment = 'paper' and provider_connectivity = 'disabled')
    or (
      environment = 'contract_test'
      and provider_connectivity = 'local_contract_simulator'
    )
  )
);

insert into private.environment_policy (id)
values ('singleton')
on conflict (id) do nothing;

create table private.role_assignments (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  role text not null check (
    role in (
      'platform_admin',
      'operator',
      'risk_approver',
      'strategy_reviewer',
      'auditor',
      'release_manager',
      'viewer'
    )
  ),
  valid_from timestamptz not null default now(),
  valid_until timestamptz,
  granted_by uuid references auth.users(id),
  approved_by uuid references auth.users(id),
  reason text not null default 'legacy_backfill',
  ticket_ref text,
  revoked_at timestamptz,
  revoked_by uuid references auth.users(id),
  created_at timestamptz not null default now(),
  constraint user_role_assignment_window_check check (
    valid_until is null or valid_until > valid_from
  ),
  constraint user_role_assignment_revoke_check check (
    (revoked_at is null and revoked_by is null)
    or (revoked_at is not null and revoked_by is not null and revoked_at >= valid_from)
  ),
  constraint user_role_assignment_approval_check check (
    approved_by is null or granted_by is null or approved_by <> granted_by
  )
);

create unique index idx_role_assignments_active_unique
  on private.role_assignments (user_id, role)
  where revoked_at is null;
create index idx_role_assignments_lookup
  on private.role_assignments (user_id, role, valid_from, valid_until)
  where revoked_at is null;

insert into private.role_assignments (
  user_id,
  role,
  reason
)
select
  roles.user_id,
  case roles.role
    when 'admin' then 'platform_admin'
    else 'viewer'
  end,
  'legacy_user_roles_backfill'
from public.user_roles as roles
on conflict (user_id, role) where revoked_at is null do nothing;

create table private.control_evidence (
  id uuid primary key default gen_random_uuid(),
  evidence_type text not null check (
    evidence_type in (
      'account_opening',
      'contract_test_contract',
      'risk_review',
      'release',
      'incident',
      'restore_drill'
    )
  ),
  environment text not null check (environment in ('paper', 'contract_test')),
  artifact_uri text not null check (
    artifact_uri ~ '^https://[^/?#[:space:]]+/'
    and artifact_uri !~ '[?#]'
  ),
  artifact_sha256 text not null check (artifact_sha256 ~ '^[0-9a-f]{64}$'),
  captured_at timestamptz not null,
  verified_at timestamptz not null,
  verified_by uuid not null references auth.users(id),
  metadata_summary jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  constraint control_evidence_time_check check (
    verified_at >= captured_at
    and captured_at <= created_at + interval '5 minutes'
  ),
  unique (artifact_uri),
  unique (artifact_sha256)
);

create table private.operation_commands (
  id uuid primary key default gen_random_uuid(),
  command_type text not null check (
    command_type in (
      'emergency_stop',
      'account_opening',
      'contract_test_enable',
      'pause_paper',
      'paper_resume',
      'strategy_promotion',
      'risk_policy_change',
      'release_promotion',
      'unknown_resolution'
    )
  ),
  state text not null default 'requested' check (
    state in (
      'requested',
      'approved',
      'claimed',
      'applied',
      'rejected',
      'failed',
      'expired',
      'canceled'
    )
  ),
  requested_change jsonb not null default '{}'::jsonb,
  command_sha256 text check (command_sha256 is null or command_sha256 ~ '^[0-9a-f]{64}$'),
  revision bigint not null default 0 check (revision >= 0),
  evidence_id uuid references private.control_evidence(id),
  target_release_sha text,
  requester_user_id uuid not null references auth.users(id),
  reviewer_user_id uuid references auth.users(id),
  claimed_by_service text,
  requested_at timestamptz not null default now(),
  reviewed_at timestamptz,
  claimed_at timestamptz,
  claim_expires_at timestamptz,
  applied_at timestamptz,
  expires_at timestamptz not null,
  review_reason text,
  failure_code text,
  result_summary jsonb,
  idempotency_key text not null unique,
  created_at timestamptz not null default now(),
  constraint operation_commands_release_sha_check check (
    target_release_sha is null
    or target_release_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
  ),
  constraint operation_commands_expiry_check check (
    expires_at > requested_at
    and expires_at <= requested_at + interval '24 hours'
  ),
  constraint operation_commands_review_separation_check check (
    reviewer_user_id is null or reviewer_user_id <> requester_user_id
  ),
  constraint operation_commands_state_shape_check check (
    (state = 'requested'
      and command_type <> 'emergency_stop'
      and reviewer_user_id is null
      and reviewed_at is null
      and claimed_by_service is null
      and claimed_at is null
      and claim_expires_at is null
      and applied_at is null
      and failure_code is null
      and result_summary is null)
    or (state = 'approved'
      and (
        (command_type = 'emergency_stop'
          and reviewer_user_id is null
          and reviewed_at is not null)
        or (command_type <> 'emergency_stop'
          and reviewer_user_id is not null
          and reviewed_at is not null)
      )
      and claimed_by_service is null
      and claimed_at is null
      and claim_expires_at is null
      and applied_at is null
      and failure_code is null)
    or (state = 'claimed'
      and (
        (command_type = 'emergency_stop' and reviewer_user_id is null)
        or (command_type <> 'emergency_stop' and reviewer_user_id is not null)
      )
      and reviewed_at is not null
      and nullif(btrim(claimed_by_service), '') is not null
      and claimed_at is not null
      and claim_expires_at is not null
      and claim_expires_at > claimed_at
      and applied_at is null
      and failure_code is null)
    or (state = 'applied'
      and (
        (command_type = 'emergency_stop' and reviewer_user_id is null)
        or (command_type <> 'emergency_stop' and reviewer_user_id is not null)
      )
      and reviewed_at is not null
      and nullif(btrim(claimed_by_service), '') is not null
      and claimed_at is not null
      and claim_expires_at is not null
      and claim_expires_at > claimed_at
      and applied_at is not null
      and applied_at >= claimed_at
      and failure_code is null
      and result_summary is not null)
    or (state = 'rejected'
      and command_type <> 'emergency_stop'
      and reviewer_user_id is not null
      and reviewed_at is not null
      and nullif(btrim(review_reason), '') is not null
      and claimed_by_service is null
      and claimed_at is null
      and claim_expires_at is null
      and applied_at is null
      and failure_code is null)
    or (state = 'failed'
      and (
        (command_type = 'emergency_stop' and reviewer_user_id is null)
        or (command_type <> 'emergency_stop' and reviewer_user_id is not null)
      )
      and reviewed_at is not null
      and nullif(btrim(claimed_by_service), '') is not null
      and claimed_at is not null
      and claim_expires_at is not null
      and claim_expires_at > claimed_at
      and applied_at is not null
      and applied_at >= claimed_at
      and nullif(btrim(failure_code), '') is not null
      and result_summary is not null)
    or (state in ('expired', 'canceled')
      and claimed_by_service is null
      and claimed_at is null
      and claim_expires_at is null
      and applied_at is null
      and failure_code is null)
  )
);

create index idx_operation_commands_requested
  on private.operation_commands (command_type, requested_at)
  where state = 'requested';
create index idx_operation_commands_approved
  on private.operation_commands (command_type, expires_at)
  where state = 'approved';

create table private.audit_events (
  id uuid primary key default gen_random_uuid(),
  occurred_at timestamptz not null default clock_timestamp(),
  transaction_id bigint not null default txid_current(),
  actor_type text not null check (actor_type in ('human', 'worker', 'system')),
  actor_user_id uuid references auth.users(id),
  actor_role text,
  actor_session_hash text check (
    actor_session_hash is null or actor_session_hash ~ '^[0-9a-f]{64}$'
  ),
  service_principal text,
  worker_instance_id text,
  release_sha text check (
    release_sha is null or release_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
  ),
  action text not null,
  resource_type text not null,
  resource_id text,
  correlation_id uuid,
  control_command_id uuid references private.operation_commands(id),
  reason_code text,
  ticket_ref text,
  changed_fields text[] not null default array[]::text[],
  before_digest text check (before_digest is null or before_digest ~ '^[0-9a-f]{64}$'),
  after_digest text check (after_digest is null or after_digest ~ '^[0-9a-f]{64}$'),
  evidence_id uuid references private.control_evidence(id),
  previous_event_hash text check (
    previous_event_hash is null or previous_event_hash ~ '^[0-9a-f]{64}$'
  ),
  event_hash text not null unique check (event_hash ~ '^[0-9a-f]{64}$'),
  constraint audit_event_actor_shape_check check (
    (actor_type = 'human' and actor_user_id is not null and service_principal is null)
    or (actor_type = 'worker' and actor_user_id is null and service_principal is not null)
    or (actor_type = 'system' and actor_user_id is null)
  )
);

create index idx_audit_events_occurred_at
  on private.audit_events (occurred_at desc);
create index idx_audit_events_resource
  on private.audit_events (resource_type, resource_id, occurred_at desc);

create table private.delivery_outbox (
  id uuid primary key default gen_random_uuid(),
  event_type text not null,
  payload_version integer not null default 1 check (payload_version > 0),
  aggregate_type text not null,
  aggregate_id text not null,
  dedupe_key text not null unique,
  payload jsonb not null default '{}'::jsonb,
  destination_type text not null check (
    destination_type in ('audit_archive', 'incident_alert', 'operations_metric')
  ),
  status text not null default 'pending' check (
    status in ('pending', 'leased', 'delivered', 'dead_letter')
  ),
  available_at timestamptz not null default now(),
  lease_owner text,
  lease_expires_at timestamptz,
  attempt_count integer not null default 0 check (attempt_count >= 0),
  max_attempts integer not null default 8 check (max_attempts between 1 and 100),
  last_error_code text,
  external_receipt_id text,
  external_receipt_digest text check (
    external_receipt_digest is null or external_receipt_digest ~ '^[0-9a-f]{64}$'
  ),
  created_at timestamptz not null default now(),
  delivered_at timestamptz,
  constraint delivery_outbox_lease_shape_check check (
    (status = 'leased' and lease_owner is not null and lease_expires_at is not null)
    or (status <> 'leased' and lease_owner is null and lease_expires_at is null)
  ),
  constraint delivery_outbox_delivery_shape_check check (
    (status = 'delivered' and delivered_at is not null and external_receipt_id is not null)
    or (status <> 'delivered' and delivered_at is null)
  )
);

create index idx_delivery_outbox_claim
  on private.delivery_outbox (available_at, created_at)
  where status in ('pending', 'leased');
create index idx_delivery_outbox_dead_letter
  on private.delivery_outbox (created_at desc)
  where status = 'dead_letter';

create table private.incidents (
  id uuid primary key default gen_random_uuid(),
  severity text not null check (severity in ('warning', 'high', 'critical')),
  status text not null default 'open' check (
    status in ('open', 'acknowledged', 'resolved')
  ),
  incident_type text not null,
  summary_code text not null,
  correlation_id uuid,
  source_event_id uuid references private.audit_events(id),
  opened_at timestamptz not null default now(),
  acknowledged_at timestamptz,
  acknowledged_by uuid references auth.users(id),
  resolved_at timestamptz,
  resolved_by uuid references auth.users(id),
  resolution_code text,
  created_at timestamptz not null default now(),
  constraint incident_state_shape_check check (
    (status = 'open'
      and acknowledged_at is null
      and acknowledged_by is null
      and resolved_at is null
      and resolved_by is null)
    or (status = 'acknowledged'
      and acknowledged_at is not null
      and acknowledged_by is not null
      and resolved_at is null
      and resolved_by is null)
    or (status = 'resolved'
      and acknowledged_at is not null
      and acknowledged_by is not null
      and resolved_at is not null
      and resolved_by is not null
      and resolved_at >= acknowledged_at
      and nullif(btrim(resolution_code), '') is not null)
  )
);

create index idx_incidents_opened_at
  on private.incidents (status, opened_at desc);

create or replace function private.reject_append_only_mutation()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  raise exception 'append_only_table_mutation_forbidden'
    using errcode = '42501';
end;
$$;

drop trigger if exists reject_audit_event_mutation on private.audit_events;
create trigger reject_audit_event_mutation
  before update or delete on private.audit_events
  for each row execute function private.reject_append_only_mutation();

drop trigger if exists reject_control_evidence_mutation on private.control_evidence;
create trigger reject_control_evidence_mutation
  before update or delete on private.control_evidence
  for each row execute function private.reject_append_only_mutation();

alter table private.environment_policy enable row level security;
alter table private.role_assignments enable row level security;
alter table private.control_evidence enable row level security;
alter table private.operation_commands enable row level security;
alter table private.audit_events enable row level security;
alter table private.delivery_outbox enable row level security;
alter table private.incidents enable row level security;

revoke all on all tables in schema private
  from public, anon, authenticated, service_role;
revoke all on all sequences in schema private
  from public, anon, authenticated, service_role;
revoke execute on all functions in schema private
  from public, anon, authenticated, service_role;
