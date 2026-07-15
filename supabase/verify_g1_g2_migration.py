#!/usr/bin/env python3
"""Disposable PostgreSQL/PostgREST verifier for the complete local migration set.

The verifier never connects to a hosted project. It creates isolated Docker
containers, applies every migration and the non-secret development seed, runs
catalog and behavioral assertions, then destroys its containers/network.
"""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4


ROOT = Path(__file__).resolve().parent
MIGRATIONS = ROOT / "migrations"
SEED = ROOT / "seed.sql"
POSTGRES_IMAGE = "postgres:16-alpine"
POSTGREST_IMAGE = "postgrest/postgrest:v12.2.8"
DB_PASSWORD = "g1-g2-disposable-only"
JWT_SECRET = "g1-g2-disposable-jwt-secret-32-bytes-minimum"

ADMIN_1 = "11111111-1111-4111-8111-111111111111"
ADMIN_2 = "22222222-2222-4222-8222-222222222222"
OPERATOR = "33333333-3333-4333-8333-333333333333"
RISK = "44444444-4444-4444-8444-444444444444"
SUBJECT = "55555555-5555-4555-8555-555555555555"
VIEWER = "66666666-6666-4666-8666-666666666666"
STRATEGY = "67676767-6767-4767-8767-676767676767"
AUDITOR = "68686868-6868-4868-8868-686868686868"
RELEASE_MANAGER = "69696969-6969-4969-8969-696969696969"
LEGACY_USER = "77777777-7777-4777-8777-777777777778"
EVIDENCE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CONTRACT_EVIDENCE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
ACCOUNT_COMMAND = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
ACCESS_REQUEST = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
ACCESS_REVIEW = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
RELEASE_SHA = "a" * 40
NEXT_RELEASE_SHA = "b" * 40
OPENAPI_SHA256 = "2c54ebfd038a8c135f4b7f9036c42934d8ab9906c026251a7ae827b81e8e6aa8"


class VerificationError(RuntimeError):
    pass


def run(args: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        input=input_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise VerificationError(
            f"command failed ({result.returncode}): {' '.join(args)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def psql(container: str, sql: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(
        [
            "docker", "exec", "-i", container, "psql", "-X", "-q",
            "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1",
            "-At",
        ],
        input_text=sql,
        check=check,
    )


def wait_for_postgres(container: str) -> None:
    """Wait past the image's temporary init server and for the final server."""
    for _ in range(120):
        probe = run(
            ["docker", "exec", container, "psql", "-U", "postgres", "-Atc", "select 1"],
            check=False,
        )
        logs = run(["docker", "logs", container], check=False)
        ready_count = (logs.stdout + logs.stderr).count(
            "database system is ready to accept connections"
        )
        if probe.returncode == 0 and ready_count >= 2:
            return
        time.sleep(0.25)
    raise VerificationError(f"PostgreSQL did not become ready: {container}")


def expect_failure(container: str, sql: str, *fragments: str) -> None:
    result = psql(container, sql, check=False)
    if result.returncode == 0:
        raise VerificationError("negative assertion unexpectedly succeeded")
    output = (result.stdout + "\n" + result.stderr).lower()
    missing = [item for item in fragments if item.lower() not in output]
    if missing:
        raise VerificationError(f"negative assertion missed {missing}:\n{output}")


def bootstrap_sql() -> str:
    return """
create schema auth;
create table auth.users (id uuid primary key, email text);
create role anon nologin;
create role authenticated nologin;
create role service_role nologin bypassrls;
create role authenticator noinherit login password 'g1-g2-disposable-only';
grant anon, authenticated, service_role to authenticator;
create publication supabase_realtime;
create function auth.uid() returns uuid language sql stable as $$
  select coalesce(
    nullif(current_setting('request.jwt.claim.sub', true), '')::uuid,
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb->>'sub')::uuid
  );
$$;
create function auth.role() returns text language sql stable as $$
  select coalesce(
    nullif(current_setting('request.jwt.claim.role', true), ''),
    nullif(current_setting('request.jwt.claims', true), '')::jsonb->>'role',
    current_user
  );
$$;
create function auth.jwt() returns jsonb language sql stable as $$
  select coalesce(
    nullif(current_setting('request.jwt.claims', true), '')::jsonb,
    '{}'::jsonb
  );
$$;
"""


def apply_repository(container: str) -> None:
    psql(container, bootstrap_sql())
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        psql(container, migration.read_text(encoding="utf-8"))
        print(f"PASS migration {migration.name}")
    psql(container, SEED.read_text(encoding="utf-8"))
    print("PASS seed non-live defaults")


def verify_populated_0015_upgrade(container: str) -> None:
    psql(container, bootstrap_sql())
    migrations = sorted(MIGRATIONS.glob("*.sql"))
    for migration in migrations:
        if migration.name > "0015_paper_order_execution_details.sql":
            break
        psql(container, migration.read_text(encoding="utf-8"))
    psql(container, SEED.read_text(encoding="utf-8"))
    psql(container, f"""
insert into auth.users (id,email) values ('{LEGACY_USER}','legacy@example.invalid');
insert into public.user_roles (user_id,role) values ('{LEGACY_USER}','admin');
insert into public.positions (
  symbol,quantity,avg_price_krw,current_price_krw,market_value_krw,
  unrealized_pnl_krw,unrealized_pnl_pct,sector,synced_at
) values ('005930',3,70000,71000,213000,3000,0.014285,'legacy',clock_timestamp());
with strategy as (
  select id from public.strategy_versions where version_name='weighted_factor_v1_seed'
), decision as (
  insert into public.decision_snapshots (
    cycle_id,symbol,action,final_score,confidence,strategy_version_id
  ) select gen_random_uuid(),'005930','buy',0.8,0.8,id from strategy returning id
)
insert into public.orders (
  decision_id,symbol,side,mode,status,amount_krw,idempotency_key,quantity,price_krw
) select id,'005930','buy','paper','paper',100000,'legacy-order-001',1,100000
from decision;
""")
    for migration in migrations:
        if migration.name <= "0015_paper_order_execution_details.sql":
            continue
        psql(container, migration.read_text(encoding="utf-8"))
    result = psql(container, f"""
select concat_ws('|',
  (select count(*) from public.orders where idempotency_key='legacy-order-001'),
  (select count(*) from public.positions where symbol='005930'),
  (select count(*) from private.order_intents),
  (select count(*) from private.position_projection),
  (select count(*) from private.trading_accounts where state='pending_open'),
  (select sum(settled_cash_krw)::bigint from private.cash_balance_projection),
  (select count(*) from private.role_assignments
    where user_id='{LEGACY_USER}' and role='platform_admin'),
  (select count(*) from information_schema.role_table_grants
    where table_schema in ('private','public')
      and grantee in ('anon','authenticated','service_role')),
  (select count(*) from pg_publication_rel pr
    join pg_publication pub on pub.oid=pr.prpubid
    join pg_class c on c.oid=pr.prrelid
    join pg_namespace n on n.oid=c.relnamespace
    where pub.pubname='supabase_realtime' and n.nspname='public')
);
""").stdout.strip()
    if result != "1|1|0|0|2|0|1|0|0":
        raise VerificationError(f"populated 0015 upgrade isolation mismatch: {result}")
    print("PASS populated 0015 upgrade: legacy frozen, no accounting aggregation")


def verify_populated_0023_operational_upgrade(container: str) -> None:
    psql(container, bootstrap_sql())
    migrations = sorted(MIGRATIONS.glob("*.sql"))
    for migration in migrations:
        if migration.name >= "0024_operational_upgrade_convergence.sql":
            break
        psql(container, migration.read_text(encoding="utf-8"))
    psql(container, SEED.read_text(encoding="utf-8"))
    psql(container, fixture_sql())
    valid_account = "paper-upgrade-valid"
    valid_qualification = "24242424-2424-4424-8424-242424242424"
    valid_command = "25252525-2525-4525-8525-252525252525"
    valid_risk = "26262626-2626-4626-8626-262626262626"
    upgrade_intent = "27272727-2727-4727-8727-272727272727"
    psql(container, f"""
-- Invalid enabled control: no applied qualification command exists.
update private.execution_controls
set execution_enabled=true,
    control_epoch=control_epoch+1,
    effective_at=clock_timestamp()-interval '1 minute',
    expires_at=clock_timestamp()+interval '2 days',
    updated_reason_code='pre_upgrade_unverified_enable',
    updated_at=clock_timestamp()+interval '1 microsecond'
where account_id='paper-primary';

-- Valid enabled control whose expiry was extended after 0023's application
-- guard; 0024 must retain it but clamp it back to qualification validity.
insert into private.trading_accounts (
 account_id,environment,broker,state,opening_capital_krw,opened_at
) values (
 '{valid_account}','paper','internal_paper','open',10000000,clock_timestamp()
);
insert into private.execution_controls (
 account_id,environment,execution_policy_version,execution_policy_sha256,
 risk_policy_sha256,effective_at,expires_at
)
select '{valid_account}','paper',execution_policy_version,
 execution_policy_sha256,risk_policy_sha256,
 clock_timestamp()-interval '5 minutes',clock_timestamp()+interval '4 hours'
from private.execution_controls where account_id='paper-primary';
insert into private.qualifications (
 id,environment,status,release_sha,ledger_checkpoint,dataset_version,
 execution_policy_version,execution_policy_sha256,risk_policy_sha256,
 strategy_version_id,risk_policy_version_id,valid_from,valid_until,
 g1_status,g1_checked_at,g1_evidence_id,g2_status,g2_checked_at,g2_evidence_id
)
select '{valid_qualification}','paper','qualified','{RELEASE_SHA}',
 'upgrade-ledger','upgrade-dataset',control.execution_policy_version,
 control.execution_policy_sha256,control.risk_policy_sha256,strategy.id,
 '{valid_risk}',clock_timestamp()-interval '5 minutes',
 clock_timestamp()+interval '1 hour','pass',clock_timestamp(),'{EVIDENCE}',
 'pass',clock_timestamp(),'{EVIDENCE}'
from private.execution_controls as control
cross join lateral (
 select id from public.strategy_versions order by created_at limit 1
) as strategy
where control.account_id='{valid_account}';
insert into private.operation_commands (
 id,command_type,state,requested_change,revision,evidence_id,target_release_sha,
 requester_user_id,reviewer_user_id,claimed_by_service,requested_at,reviewed_at,
 claimed_at,claim_expires_at,applied_at,expires_at,result_summary,
 idempotency_key
)
select '{valid_command}','paper_resume','applied',jsonb_build_object(
 'account_id','{valid_account}','environment','paper',
 'expected_state_version',control.control_epoch,
 'qualification_id','{valid_qualification}',
 'strategy_version_id',qualification.strategy_version_id::text,
 'risk_policy_version_id','{valid_risk}','release_sha','{RELEASE_SHA}',
 'ledger_checkpoint','upgrade-ledger',
 'execution_policy_version',control.execution_policy_version,
 'execution_policy_sha256',control.execution_policy_sha256,
 'risk_policy_sha256',control.risk_policy_sha256,
 'reason_code','upgrade_valid_resume'
),1,'{EVIDENCE}','{RELEASE_SHA}','{OPERATOR}','{RISK}','upgrade-worker',
 clock_timestamp()-interval '5 minutes',clock_timestamp()-interval '4 minutes',
 clock_timestamp()-interval '3 minutes',clock_timestamp()+interval '5 minutes',
 clock_timestamp()-interval '2 minutes',clock_timestamp()+interval '1 hour',
 '{{}}'::jsonb,'upgrade-valid-command'
from private.execution_controls as control
join private.qualifications as qualification
  on qualification.id='{valid_qualification}'
where control.account_id='{valid_account}';
update private.execution_controls as control
set execution_enabled=true,
    control_epoch=control.control_epoch+1,
    active_strategy_version_id=qualification.strategy_version_id::text,
    active_risk_policy_version_id=qualification.risk_policy_version_id,
    last_command_id='{valid_command}',
    effective_at=clock_timestamp()-interval '1 minute',
    expires_at=clock_timestamp()+interval '4 hours',
    updated_reason_code='qualified_resume',
    updated_at=clock_timestamp()+interval '1 microsecond'
from private.qualifications as qualification
where control.account_id='{valid_account}'
  and qualification.id='{valid_qualification}';
update private.execution_controls
set control_epoch=control_epoch+1,
    expires_at=clock_timestamp()+interval '4 hours',
    updated_reason_code='pre_upgrade_expiry_extension',
    updated_at=clock_timestamp()+interval '1 microsecond'
where account_id='{valid_account}';

-- Simulate a break inserted before the 0023 stop trigger was deployed.
alter table private.reconciliation_breaks
  disable trigger stop_execution_for_reconciliation_break_v1;
with run as (
  insert into private.reconciliation_runs (
    id,account_id,environment,started_at,completed_at,result,release_sha
  ) values (
    '28282828-2828-4828-8828-282828282828','contract-test-primary',
    'contract_test',clock_timestamp()-interval '1 hour',
    clock_timestamp()-interval '1 hour','breaks_found','{RELEASE_SHA}'
  ) returning id
)
insert into private.reconciliation_breaks (
 id,run_id,account_id,break_type,state,detected_at,summary_code
)
select '29292929-2929-4929-8929-292929292929',id,
 'contract-test-primary','execution','open',
 clock_timestamp()-interval '1 hour','pre_upgrade_open_break'
from run;
alter table private.reconciliation_breaks
  enable trigger stop_execution_for_reconciliation_break_v1;
update private.execution_controls
set execution_enabled=true,
    control_epoch=control_epoch+1,
    effective_at=clock_timestamp()-interval '1 minute',
    expires_at=clock_timestamp()+interval '2 days',
    updated_reason_code='pre_upgrade_break_not_enforced',
    updated_at=clock_timestamp()+interval '1 microsecond'
where account_id='contract-test-primary';

-- Tokenless reconciliation lease from a 0023 worker.
insert into private.execution_decisions (
 id,account_id,environment,strategy_version_id,symbol,action,decision_at,
 signal_valid_from,signal_valid_until,feature_snapshot_sha256,
 decision_sha256,release_sha
) values (
 '30303030-3030-4030-8030-303030303030','paper-primary','paper',
 'upgrade-strategy','005930','buy',clock_timestamp()-interval '2 minutes',
 clock_timestamp()-interval '3 minutes',clock_timestamp()+interval '1 hour',
 '{'3' * 64}','{'4' * 64}','{RELEASE_SHA}'
);
insert into private.risk_results (
 id,decision_id,account_id,environment,strategy_version_id,
 risk_policy_sha256,control_epoch,allowed,reason_codes,result_sha256,
 evaluated_at,expires_at,release_sha
)
select '31313131-3131-4131-8131-313131313131',
 '30303030-3030-4030-8030-303030303030','paper-primary','paper',
 'upgrade-strategy',risk_policy_sha256,control_epoch,true,array[]::text[],
 '{'5' * 64}',clock_timestamp()-interval '1 minute',
 clock_timestamp()+interval '1 hour','{RELEASE_SHA}'
from private.execution_controls where account_id='paper-primary';
insert into private.order_intents (
 id,semantic_key_sha256,account_id,environment,strategy_version_id,
 decision_id,risk_result_id,correlation_id,symbol,side,quantity,limit_price_krw,
 decision_at,signal_valid_from,signal_valid_until,eligible_at,expires_at,
 execution_policy_version,execution_policy_sha256,cost_schedule_version,
 cost_schedule_evidence_sha256,cash_commitment_krw,risk_policy_sha256,
 control_epoch,release_sha
)
select '{upgrade_intent}','{'6' * 64}','paper-primary','paper',
 'upgrade-strategy','30303030-3030-4030-8030-303030303030',
 '31313131-3131-4131-8131-313131313131','{upgrade_intent}',
 '005930','buy',1,10000,clock_timestamp()-interval '2 minutes',
 clock_timestamp()-interval '3 minutes',clock_timestamp()+interval '1 hour',
 clock_timestamp()-interval '1 minute',clock_timestamp()+interval '1 hour',
 execution_policy_version,execution_policy_sha256,'upgrade-cost','{'7' * 64}',
 10000,risk_policy_sha256,control_epoch,'{RELEASE_SHA}'
from private.execution_controls where account_id='paper-primary';
insert into private.execution_reconciliation_state (
 intent_id,priority,state,next_reconcile_at,lease_owner,lease_expires_at,
 attempt_count,last_reason_code
) values (
 '{upgrade_intent}',30,'leased',clock_timestamp()-interval '1 minute',
 'old-worker',clock_timestamp()+interval '1 hour',1,'pre_upgrade_claim'
);

insert into private.delivery_outbox (
 id,event_type,aggregate_type,aggregate_id,dedupe_key,payload,
 destination_type,status,available_at,lease_owner,lease_expires_at,
 attempt_count,max_attempts
) values
 ('32323232-3232-4232-8232-323232323232','verification','upgrade','retry',
  'upgrade-tokenless-retry','{{}}','operations_metric','leased',
  clock_timestamp()+interval '30 days','old-worker',
  clock_timestamp()+interval '30 days',1,3),
 ('33333333-3333-4333-8333-333333333334','verification','upgrade','final',
  'upgrade-final-crash','{{}}','operations_metric','leased',
  clock_timestamp()+interval '30 days','old-worker',
  clock_timestamp()+interval '30 days',3,3),
 ('34343434-3434-4434-8434-343434343434','verification','upgrade','clock',
  'upgrade-future-clock','{{}}','operations_metric','pending',
  clock_timestamp()+interval '30 days',null,null,0,3);
""")

    migration = MIGRATIONS / "0024_operational_upgrade_convergence.sql"
    psql(container, migration.read_text(encoding="utf-8"))
    result = psql(container, f"""
select concat_ws('|',
  (select execution_enabled from private.execution_controls
    where account_id='paper-primary'),
  (select updated_reason_code from private.execution_controls
    where account_id='paper-primary'),
  (select execution_enabled from private.execution_controls
    where account_id='{valid_account}'),
  (select updated_reason_code from private.execution_controls
    where account_id='{valid_account}'),
  (select control.expires_at=qualification.valid_until
    from private.execution_controls as control
    join private.qualifications as qualification
      on qualification.id='{valid_qualification}'
    where control.account_id='{valid_account}'),
  (select execution_enabled from private.execution_controls
    where account_id='contract-test-primary'),
  (select updated_reason_code from private.execution_controls
    where account_id='contract-test-primary'),
  (select state from private.execution_reconciliation_state
    where intent_id='{upgrade_intent}'),
  (select claim_release_sha is null and claim_fencing_token is null
    from private.execution_reconciliation_state
    where intent_id='{upgrade_intent}'),
  (select status from private.delivery_outbox
    where dedupe_key='upgrade-tokenless-retry'),
  (select lease_token is null from private.delivery_outbox
    where dedupe_key='upgrade-tokenless-retry'),
  (select status from private.delivery_outbox
    where dedupe_key='upgrade-final-crash'),
  (select count(*) from private.incidents
    where incident_type='delivery_dead_letter'
      and correlation_id='33333333-3333-4333-8333-333333333334'),
  (select available_at <= clock_timestamp() from private.delivery_outbox
    where dedupe_key='upgrade-future-clock')
);
""").stdout.strip()
    expected = (
        "f|operational_upgrade_qualification_invalid|"
        "t|operational_upgrade_qualification_revalidated|t|"
        "f|unresolved_reconciliation_break|pending|t|pending|t|"
        "dead_letter|1|t"
    )
    if result != expected:
        raise VerificationError(f"populated 0023 operational upgrade mismatch: {result}")
    print(
        "PASS populated 0023->0024 upgrade: controls, claims and caller-clock "
        "delivery rows converge fail closed"
    )


def fixture_sql() -> str:
    return f"""
insert into auth.users (id, email) values
  ('{ADMIN_1}', 'admin1@example.invalid'),
  ('{ADMIN_2}', 'admin2@example.invalid'),
  ('{OPERATOR}', 'operator@example.invalid'),
  ('{RISK}', 'risk@example.invalid'),
  ('{SUBJECT}', 'subject@example.invalid'),
  ('{VIEWER}', 'viewer@example.invalid'),
  ('{STRATEGY}', 'strategy@example.invalid'),
  ('{AUDITOR}', 'auditor@example.invalid'),
  ('{RELEASE_MANAGER}', 'release-manager@example.invalid');
insert into private.role_assignments (user_id, role, reason) values
  ('{ADMIN_1}', 'platform_admin', 'verifier_fixture'),
  ('{ADMIN_2}', 'platform_admin', 'verifier_fixture'),
  ('{OPERATOR}', 'operator', 'verifier_fixture'),
  ('{RISK}', 'risk_approver', 'verifier_fixture'),
  ('{VIEWER}', 'viewer', 'verifier_fixture'),
  ('{STRATEGY}', 'strategy_reviewer', 'verifier_fixture'),
  ('{AUDITOR}', 'auditor', 'verifier_fixture'),
  ('{RELEASE_MANAGER}', 'release_manager', 'verifier_fixture');
insert into private.control_evidence (
  id, evidence_type, environment, artifact_uri, artifact_sha256,
  captured_at, verified_at, verified_by, metadata_summary
) values
  ('{EVIDENCE}', 'account_opening', 'paper',
   'https://evidence.example.invalid/account-opening.json', '{'1' * 64}',
   clock_timestamp() - interval '1 minute', clock_timestamp(), '{ADMIN_1}',
   '{{"fixture":true}}'::jsonb),
  ('{CONTRACT_EVIDENCE}', 'contract_test_contract', 'contract_test',
   'https://openapi.tossinvest.com/openapi-docs/latest/openapi.json',
   '{OPENAPI_SHA256}', '2026-07-14T12:11:03.1057242Z', clock_timestamp(),
   '{ADMIN_2}', '{{"bytes":340381,"disposable_verifier":true}}'::jsonb);
insert into private.provider_contract_registry (
  provider, qualification_environment, execution_transport, contract_version,
  openapi_sha256, official_artifact_uri, retrieved_at, evidence_id,
  release_sha, status, requested_by, reviewed_by, effective_from, effective_until
) values (
  'toss', 'contract_test', 'local_contract_simulator', 'latest-2026-07-14',
  '{OPENAPI_SHA256}',
  'https://openapi.tossinvest.com/openapi-docs/latest/openapi.json',
  '2026-07-14T12:11:03.1057242Z', '{CONTRACT_EVIDENCE}', '{RELEASE_SHA}',
  'approved', '{ADMIN_1}', '{ADMIN_2}',
  clock_timestamp() - interval '1 minute', clock_timestamp() + interval '1 day'
);
"""


def jwt_claim_sql(user_id: str, *, role: str = "authenticated", totp: bool = True) -> str:
    method = "totp" if totp else "password"
    return f"""
select set_config('request.jwt.claim.sub', '{user_id}', false);
select set_config('request.jwt.claim.role', '{role}', false);
select set_config('request.jwt.claim.aal', 'aal2', false);
select set_config(
  'request.jwt.claims',
  jsonb_build_object(
    'sub', '{user_id}', 'role', '{role}', 'aal', 'aal2',
    'session_id', 'session-{user_id}',
    'amr', jsonb_build_array(jsonb_build_object(
      'method', '{method}', 'timestamp', extract(epoch from clock_timestamp())
    ))
  )::text,
  false
);
set role {role};
"""


def verify_catalog(container: str) -> None:
    result = psql(container, """
select concat_ws('|',
  (select count(*) from pg_proc p join pg_namespace n on n.oid=p.pronamespace
    where n.nspname='worker_api'),
  (select count(*) from pg_proc p join pg_namespace n on n.oid=p.pronamespace
    where n.nspname in ('api','worker_api') and p.prosecdef),
  (select count(*) from information_schema.role_table_grants
    where table_schema in ('private','public')
      and grantee in ('anon','authenticated','service_role')),
  (select string_agg(n.nspname||'.'||c.relname, ',' order by n.nspname,c.relname)
   from pg_publication_rel pr join pg_publication pub on pub.oid=pr.prpubid
   join pg_class c on c.oid=pr.prrelid join pg_namespace n on n.oid=c.relnamespace
   where pub.pubname='supabase_realtime'),
  (select count(*)
   from pg_proc p join pg_namespace n on n.oid=p.pronamespace
   cross join lateral aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
   where n.nspname='private' and a.grantee=0 and a.privilege_type='EXECUTE'),
  (select count(*) from pg_proc p join pg_namespace n on n.oid=p.pronamespace
   where n.nspname='private' and has_function_privilege('anon',p.oid,'EXECUTE')),
  (select count(*) from pg_proc p join pg_namespace n on n.oid=p.pronamespace
   where n.nspname='private' and has_function_privilege('authenticated',p.oid,'EXECUTE')),
  (select count(*) from pg_proc p join pg_namespace n on n.oid=p.pronamespace
   where n.nspname='private' and has_function_privilege('service_role',p.oid,'EXECUTE')),
  has_function_privilege(
    'authenticated',
    'private.quarantine_execution_observation(uuid,uuid,integer,text,text,text,text,text,timestamptz,text)',
    'EXECUTE'
  ),
  has_function_privilege(
    'service_role',
    'private.quarantine_execution_observation(uuid,uuid,integer,text,text,text,text,text,timestamptz,text)',
    'EXECUTE'
  ),
  to_regprocedure(
    'worker_api.claim_paper_execution_v1(text,uuid,text,timestamptz,integer)'
  ) is not null,
  to_regprocedure(
    'worker_api.claim_cash_settlement_batch(text,text,text,bigint,timestamptz,integer)'
  ) is not null,
  to_regprocedure(
    'worker_api.complete_cash_settlement(uuid,bigint,uuid,text,text,bigint,timestamptz)'
  ) is not null,
  to_regprocedure(
    'worker_api.fail_cash_settlement_attempt(uuid,bigint,uuid,text,text,bigint,timestamptz,text)'
  ) is not null,
  to_regprocedure(
    'worker_api.claim_unknown_resolution_v2(uuid,text,text,bigint,bigint,bigint,timestamptz)'
  ) is not null,
  to_regprocedure(
    'worker_api.apply_unknown_resolution_v2(uuid,uuid,text,text,bigint,bigint,bigint,bigint,timestamptz)'
  ) is not null,
  to_regprocedure('api.request_unknown_resolution_v2(jsonb)') is not null,
  to_regprocedure('api.review_unknown_resolution_v2(jsonb)') is not null,
  to_regclass('private.paper_execution_work_items') is not null,
  to_regclass('private.cash_settlement_obligations') is not null,
  to_regclass('private.unknown_execution_resolution_applications_v2') is not null
);
""").stdout.strip()
    parts = result.split("|")
    if len(parts) != 21:
        raise VerificationError(f"catalog boundary shape mismatch: {result}")
    worker_rpc_count = int(parts[0])
    stable_boundary = parts[1:6]
    quarantine_acl = parts[8:10]
    explicit_contracts = parts[10:]
    if (
        worker_rpc_count < 35
        or stable_boundary != ["0", "0", "api.control_plane_signal", "0", "0"]
        or quarantine_acl != ["f", "f"]
        or explicit_contracts != ["t"] * len(explicit_contracts)
    ):
        raise VerificationError(f"catalog boundary mismatch: {result}")
    print(
        "PASS catalog/API allowlists, explicit source/settlement/unknown contracts, "
        "private definer ACLs and signal-only Realtime"
    )


def verify_strict_auth(container: str) -> None:
    draft = f"jsonb_build_object('schema_version',1,'request_id','{ACCESS_REQUEST}'," \
        f"'subject_user_id','{SUBJECT}','requested_role','viewer','change_type','grant'," \
        f"'evidence_id','{EVIDENCE}','reason_code','role_required'," \
        "'requested_at',clock_timestamp(),'expires_at',clock_timestamp()+interval '1 hour')"
    expect_failure(
        container,
        jwt_claim_sql(ADMIN_1, totp=False)
        + f"select api.issue_access_step_up_grant_v1(jsonb_build_object(" \
          f"'schema_version',1,'bound_action','request','access_change_payload',{draft}));",
        "recent_totp_verification_required",
    )
    expect_failure(
        container,
        jwt_claim_sql(ADMIN_1)
        + f"select api.issue_access_step_up_grant_v1(jsonb_build_object(" \
          f"'schema_version','1','bound_action','request','access_change_payload',{draft}));",
        "access_step_up_request_schema_invalid",
    )
    boolean_sql = jwt_claim_sql(ADMIN_1) + f"""
with draft(value) as (select {draft}),
grant_value(value) as (
  select api.issue_access_step_up_grant_v1(jsonb_build_object(
    'schema_version',1,'bound_action','request','access_change_payload',draft.value
  )) from draft
)
select api.request_access_change_v1(
  draft.value || grant_value.value || jsonb_build_object('step_up_grant_one_time','true')
) from draft, grant_value;
"""
    expect_failure(container, boolean_sql, "access_change_request_json_type_invalid")
    revision_draft = "jsonb_build_object('schema_version',1,'review_id',gen_random_uuid()," \
        "'command_id',gen_random_uuid(),'command_type','pause_paper'," \
        "'reviewer_role','risk_approver','decision','reject'," \
        "'reason_code','evidence_incomplete','expected_receipt_revision','0'," \
        "'reviewed_at',clock_timestamp())"
    expect_failure(
        container,
        jwt_claim_sql(RISK)
        + f"select api.issue_step_up_grant_v1(jsonb_build_object(" \
          f"'schema_version',1,'bound_action','review','bound_command_type','pause_paper'," \
          f"'command_payload',{revision_draft}));",
        "step_up_draft_json_type_invalid",
    )
    print("PASS recent TOTP and strict numeric/boolean JSON types")


def verify_access_maker_checker(container: str) -> None:
    request_draft = f"jsonb_build_object('schema_version',1,'request_id','{ACCESS_REQUEST}'," \
        f"'subject_user_id','{SUBJECT}','requested_role','viewer','change_type','grant'," \
        f"'evidence_id','{EVIDENCE}','reason_code','role_required'," \
        "'requested_at',clock_timestamp(),'expires_at',clock_timestamp()+interval '1 hour')"
    result = psql(container, jwt_claim_sql(ADMIN_1) + f"""
with draft(value) as (select {request_draft}),
grant_value(value) as (
  select api.issue_access_step_up_grant_v1(jsonb_build_object(
    'schema_version',1,'bound_action','request','access_change_payload',draft.value
  )) from draft
)
select api.request_access_change_v1(draft.value || grant_value.value)->>'state'
from draft, grant_value;
""").stdout.strip().splitlines()[-1]
    if result != "requested":
        raise VerificationError(f"access request state mismatch: {result}")
    review_draft = f"jsonb_build_object('schema_version',1,'review_id','{ACCESS_REVIEW}'," \
        f"'request_id','{ACCESS_REQUEST}','decision','approve'," \
        "'reason_code','policy_satisfied','expected_state','requested'," \
        "'reviewed_at',clock_timestamp())"
    result = psql(container, jwt_claim_sql(ADMIN_2) + f"""
with draft(value) as (select {review_draft}),
grant_value(value) as (
  select api.issue_access_step_up_grant_v1(jsonb_build_object(
    'schema_version',1,'bound_action','review','access_change_payload',draft.value
  )) from draft
)
select api.review_access_change_v1(draft.value || grant_value.value)->>'state'
from draft, grant_value;
""").stdout.strip().splitlines()[-1]
    if result != "applied":
        raise VerificationError(f"access review state mismatch: {result}")
    state = psql(container, f"""
select concat_ws('|',
  (select count(*) from private.role_assignments where user_id='{SUBJECT}' and role='viewer' and revoked_at is null),
  (select count(*) from private.step_up_grants where consumed_at is not null),
  (select count(*) from private.audit_events where resource_id='{ACCESS_REQUEST}'),
  (select count(*) from private.delivery_outbox where aggregate_id='{ACCESS_REQUEST}')
);
""").stdout.strip()
    if state != "1|2|2|2":
        raise VerificationError(f"access maker-checker evidence mismatch: {state}")
    print("PASS access maker-checker, one-time grants, audit and outbox")


def verify_account_opening(container: str) -> None:
    draft = f"jsonb_build_object('schema_version',1,'request_id','{ACCOUNT_COMMAND}'," \
        "'environment','paper','account_id','paper-primary','opening_capital_krw',10000000," \
        f"'idempotency_key','77777777-7777-4777-8777-777777777777'," \
        "'requested_at',clock_timestamp(),'expires_at',clock_timestamp()+interval '1 hour'," \
        f"'command_type','account_opening','evidence_id','{EVIDENCE}'," \
        "'reason_code','approved_account_opening')"
    psql(container, jwt_claim_sql(OPERATOR) + f"""
with draft(value) as (select {draft}), grant_value(value) as (
  select api.issue_account_opening_step_up_grant_v1(jsonb_build_object(
    'schema_version',1,'bound_action','request','bound_command_type','account_opening',
    'command_payload',draft.value)) from draft)
select api.request_account_opening_v1(draft.value || grant_value.value)
from draft, grant_value;
""")
    review = f"jsonb_build_object('schema_version',1,'review_id',gen_random_uuid()," \
        f"'command_id','{ACCOUNT_COMMAND}','command_type','account_opening'," \
        "'reviewer_role','risk_approver','decision','approve'," \
        "'reason_code','policy_satisfied','expected_receipt_revision',0," \
        "'reviewed_at',clock_timestamp())"
    psql(container, jwt_claim_sql(RISK) + f"""
with draft(value) as (select {review}), grant_value(value) as (
  select api.issue_account_opening_step_up_grant_v1(jsonb_build_object(
    'schema_version',1,'bound_action','review','bound_command_type','account_opening',
    'command_payload',draft.value)) from draft)
select api.review_account_opening_v1(draft.value || grant_value.value)
from draft, grant_value;
""")
    holder = "88888888-8888-4888-8888-888888888888"
    result = psql(container, jwt_claim_sql(holder, role="service_role") + f"""
select command_type from worker_api.claim_operation_command_batch(
  '{holder}','{RELEASE_SHA}',clock_timestamp(),25);
select state from worker_api.acknowledge_operation_command(
  '{ACCOUNT_COMMAND}','applied','{holder}','{RELEASE_SHA}',clock_timestamp(),
  '{{}}'::jsonb,null);
reset role;
select concat_ws('|',
  (select state from private.trading_accounts where account_id='paper-primary'),
  (select count(*) from private.accounting_transactions where control_command_id='{ACCOUNT_COMMAND}'),
  (select count(*) from private.accounting_postings p join private.accounting_transactions t
    on t.id=p.journal_entry_id where t.control_command_id='{ACCOUNT_COMMAND}'),
  (select settled_cash_krw::bigint from private.cash_balance_projection where account_id='paper-primary')
);
""").stdout.strip().splitlines()
    if result[-3:] != ["account_opening", "applied", "open|1|2|10000000"]:
        raise VerificationError(f"account opening mismatch: {result}")
    print("PASS account opening request/review/claim/exact-once journal")


def verify_lease_and_outbox(container: str) -> None:
    holder = "99999999-9999-4999-8999-999999999999"
    other = "aaaaaaaa-0000-4000-8000-000000000000"
    result = psql(container, jwt_claim_sql(holder, role="service_role") + f"""
select heartbeat_id is not null from worker_api.record_worker_heartbeat(
 '{holder}','ok',jsonb_build_object(
   'release_sha','{RELEASE_SHA}','mock_providers',true,
   'component','operations_v2',
   'checkpoint','operations_completed','completed_at',clock_timestamp()
 ),
 clock_timestamp(),'{RELEASE_SHA}');
select fencing_token from worker_api.acquire_worker_lease(
 'paper-primary','{holder}',clock_timestamp(),30,'{RELEASE_SHA}');
""").stdout.strip().splitlines()[-2:]
    token = result[-1]
    releases = psql(container, jwt_claim_sql(holder, role="service_role") + f"""
select idempotent from worker_api.release_worker_lease(
 'paper-primary','{holder}',{token},clock_timestamp(),'{RELEASE_SHA}');
select idempotent from worker_api.release_worker_lease(
 'paper-primary','{holder}',{token},clock_timestamp(),'{RELEASE_SHA}');
""").stdout.strip().splitlines()[-2:]
    if result[0] != "t" or releases != ["f", "t"]:
        raise VerificationError(f"lease release mismatch: {result}")
    expect_failure(
        container,
        jwt_claim_sql(other, role="service_role")
        + f"select * from worker_api.release_worker_lease(" \
          f"'paper-primary','{other}',{token},clock_timestamp(),'{RELEASE_SHA}');",
        "worker_lease_release_identity_conflict",
    )
    outbox = psql(container, f"""
insert into private.delivery_outbox (
 event_type,aggregate_type,aggregate_id,dedupe_key,payload,destination_type,available_at
) values (
 'verification','verification','one','stable-dedupe-key','{{}}','operations_metric',
 clock_timestamp()-interval '1 day'
);
{jwt_claim_sql(holder, role='service_role')}
create temp table first_claim as select * from worker_api.claim_delivery_outbox(
 '{holder}',clock_timestamp(),1,5);
create temp table second_claim as select * from worker_api.claim_delivery_outbox(
 '{other}',clock_timestamp()+interval '6 seconds',1,5);
reset role;
update private.delivery_outbox
set lease_expires_at=clock_timestamp()-interval '1 second'
where id=(select outbox_id from first_claim);
set role service_role;
create temp table reclaimed_claim as select * from worker_api.claim_delivery_outbox(
 '{other}',clock_timestamp(),1,5);
select concat_ws('|',
  (select dedupe_key from first_claim),
  (select count(*) from second_claim where dedupe_key='stable-dedupe-key'),
  (select dedupe_key from reclaimed_claim)
);
select status from worker_api.complete_outbox_delivery(
  (select outbox_id from reclaimed_claim),'{other}',
  (select lease_token from reclaimed_claim),clock_timestamp(),
  'receiver-validation','{'a' * 64}'
);
reset role;
insert into private.delivery_outbox (
 event_type,aggregate_type,aggregate_id,dedupe_key,payload,destination_type,available_at
) values (
 'verification','verification','two','failure-dedupe-key','{{}}','operations_metric',
 clock_timestamp()-interval '1 day'
);
set role service_role;
create temp table failure_claim as select * from worker_api.claim_delivery_outbox(
 '{holder}',clock_timestamp(),1,30);
select status from worker_api.fail_outbox_delivery(
  (select outbox_id from failure_claim),'{holder}',
  (select lease_token from failure_claim),clock_timestamp(),
  'receiver_unavailable',5
);
reset role;
select concat_ws('|',
  (select status from private.delivery_outbox where dedupe_key='stable-dedupe-key'),
  (select status from private.delivery_outbox where dedupe_key='failure-dedupe-key'),
  (select count(*) from private.delivery_outbox
    where dedupe_key='failure-dedupe-key' and available_at > clock_timestamp())
);
""").stdout.strip().splitlines()[-4:]
    if outbox != [
        "stable-dedupe-key|0|stable-dedupe-key",
        "delivered",
        "pending",
        "delivered|pending|1",
    ]:
        raise VerificationError(f"outbox DB-clock/ACK mismatch: {outbox}")

    aba = psql(container, f"""
insert into private.delivery_outbox (
 event_type,aggregate_type,aggregate_id,dedupe_key,payload,destination_type,
 available_at
) values (
 'verification','verification','aba','aba-dedupe-key','{{}}','operations_metric',
 clock_timestamp()-interval '3 days'
);
{jwt_claim_sql(holder, role='service_role')}
select concat_ws('|',outbox_id,lease_token)
from worker_api.claim_delivery_outbox('{holder}',clock_timestamp(),1,5)
where dedupe_key='aba-dedupe-key';
""").stdout.strip().splitlines()[-1].split("|")
    aba_id, first_lease_token = aba
    psql(container, f"""
reset role;
update private.delivery_outbox
set lease_expires_at=clock_timestamp()-interval '1 second'
where id='{aba_id}';
{jwt_claim_sql(holder, role='service_role')}
select concat_ws('|',outbox_id,lease_token)
from worker_api.claim_delivery_outbox('{holder}',clock_timestamp(),1,30)
where outbox_id='{aba_id}';
""")
    second_lease_token = psql(container, f"""
select lease_token from private.delivery_outbox where id='{aba_id}';
""").stdout.strip()
    if first_lease_token == second_lease_token:
        raise VerificationError("outbox reclaim reused the prior lease token")
    expect_failure(
        container,
        jwt_claim_sql(holder, role="service_role") + f"""
select * from worker_api.complete_outbox_delivery(
  '{aba_id}','{holder}','{first_lease_token}',clock_timestamp(),
  'stale-receipt','{'b' * 64}'
);
""",
        "outbox_lease_not_owned_current_or_expired",
    )
    expect_failure(
        container,
        jwt_claim_sql(holder, role="service_role") + f"""
select * from worker_api.fail_outbox_delivery(
  '{aba_id}','{holder}','{first_lease_token}',clock_timestamp(),
  'stale_attempt',5
);
""",
        "outbox_lease_not_owned_current_or_expired",
    )
    status = psql(container, jwt_claim_sql(holder, role="service_role") + f"""
select status from worker_api.complete_outbox_delivery(
  '{aba_id}','{holder}','{second_lease_token}',clock_timestamp(),
  'current-receipt','{'c' * 64}'
);
""").stdout.strip().splitlines()[-1]
    if status != "delivered":
        raise VerificationError(f"current outbox attempt did not complete: {status}")

    expect_failure(
        container,
        jwt_claim_sql(holder, role="service_role") + f"""
select * from worker_api.claim_delivery_outbox(
  '{holder}',clock_timestamp(),null,30
);
""",
        "outbox_claim_parameters_invalid",
    )
    expect_failure(
        container,
        jwt_claim_sql(holder, role="service_role") + f"""
select * from worker_api.complete_outbox_delivery(
  '{aba_id}','{holder}',null::uuid,clock_timestamp(),
  'missing-token','{'d' * 64}'
);
""",
        "outbox_receipt_invalid",
    )
    expect_failure(
        container,
        jwt_claim_sql(holder, role="service_role") + f"""
select * from worker_api.fail_outbox_delivery(
  '{aba_id}','{holder}',null::uuid,clock_timestamp(),'missing_token',5
);
""",
        "outbox_failure_parameters_invalid",
    )
    expect_failure(
        container,
        jwt_claim_sql(holder, role="service_role") + f"""
select * from worker_api.complete_outbox_delivery(
  '{aba_id}','{holder}',clock_timestamp(),'legacy-receipt','{'e' * 64}'
);
""",
        "worker_upgrade_required",
    )
    expect_failure(
        container,
        jwt_claim_sql(holder, role="service_role") + f"""
select * from worker_api.fail_outbox_delivery(
  '{aba_id}','{holder}',clock_timestamp(),'legacy_failure',5
);
""",
        "worker_upgrade_required",
    )

    final_attempt = psql(container, f"""
reset role;
insert into private.delivery_outbox (
 event_type,aggregate_type,aggregate_id,dedupe_key,payload,destination_type,
 available_at,max_attempts
) values (
 'verification','verification','final-crash','final-crash-dedupe-key',
 '{{}}','operations_metric',clock_timestamp()-interval '4 days',1
);
{jwt_claim_sql(other, role='service_role')}
select outbox_id from worker_api.claim_delivery_outbox(
 '{other}',clock_timestamp(),1,5
) where dedupe_key='final-crash-dedupe-key';
""").stdout.strip().splitlines()[-1]
    final_state = psql(container, f"""
reset role;
update private.delivery_outbox
set lease_expires_at=clock_timestamp()-interval '1 second'
where id='{final_attempt}';
{jwt_claim_sql(other, role='service_role')}
select count(*) from worker_api.claim_delivery_outbox(
 '{other}',clock_timestamp(),1,30
);
reset role;
select concat_ws('|',
  (select status from private.delivery_outbox where id='{final_attempt}'),
  (select last_error_code from private.delivery_outbox where id='{final_attempt}'),
  (select count(*) from private.incidents
    where incident_type='delivery_dead_letter'
      and correlation_id='{final_attempt}')
);
""").stdout.strip().splitlines()[-1]
    if final_state != (
        "dead_letter|delivery_attempt_lease_expired_at_max_attempts|1"
    ):
        raise VerificationError(f"final-attempt crash mismatch: {final_state}")
    print(
        "PASS lease CAS, outbox attempt fencing, NULL rejection and "
        "final-crash dead letter"
    )


def verify_command_claim_allowlist(container: str) -> None:
    holder = "abababab-abab-4bab-8bab-abababababab"
    psql(container, f"""
insert into private.operation_commands (
  command_type,state,requested_change,revision,requester_user_id,
  reviewer_user_id,requested_at,reviewed_at,expires_at,idempotency_key
)
select 'unknown_resolution','approved',
  jsonb_build_object('account_id','paper-primary','environment','paper'),1,
  '{OPERATOR}','{RISK}',clock_timestamp()-interval '1 minute',clock_timestamp(),
  clock_timestamp()+interval '1 hour','unsupported-'||value::text
from generate_series(1,30) as value;
insert into private.operation_commands (
  command_type,state,requested_change,revision,requester_user_id,
  reviewer_user_id,requested_at,reviewed_at,expires_at,idempotency_key
) values (
  'pause_paper','approved',
  jsonb_build_object('account_id','paper-primary','environment','paper',
    'expected_state_version',1,'reason_code','operator_pause'),1,
  '{OPERATOR}','{RISK}',clock_timestamp(),clock_timestamp(),
  clock_timestamp()+interval '1 hour','supported-after-unsupported'
);
""")
    result = psql(container, jwt_claim_sql(holder, role="service_role") + f"""
select command_type from worker_api.claim_operation_command_batch(
  '{holder}','{RELEASE_SHA}',clock_timestamp(),25);
reset role;
select count(*) from private.operation_commands
where command_type='unknown_resolution' and state='approved';
""").stdout.strip().splitlines()[-2:]
    if result != ["pause_paper", "30"]:
        raise VerificationError(f"unsupported command queue starvation: {result}")
    print("PASS worker claim excludes unsupported commands before LIMIT")


def verify_command_ack_expiry(container: str) -> None:
    command_id = "acacacac-acac-4cac-8cac-acacacacacac"
    worker = "adadadad-adad-4dad-8dad-adadadadadad"
    expect_failure(
        container,
        f"""
insert into private.operation_commands (
  id,command_type,state,requested_change,revision,requester_user_id,
  reviewer_user_id,requested_at,reviewed_at,expires_at,idempotency_key
) values (
  '{command_id}','pause_paper','approved',
  jsonb_build_object('account_id','paper-primary','environment','paper',
    'expected_state_version',2,'reason_code','operator_pause'),1,
  '{OPERATOR}','{RISK}',clock_timestamp(),clock_timestamp(),
  clock_timestamp()+interval '500 milliseconds','ack-expiry-regression'
);
""" + jwt_claim_sql(worker, role="service_role") + f"""
select state from worker_api.acknowledge_operation_command(
  '{command_id}','claimed','{worker}','{RELEASE_SHA}',clock_timestamp(),
  '{{}}'::jsonb,null
);
select pg_sleep(0.7);
select state from worker_api.acknowledge_operation_command(
  '{command_id}','applied','{worker}','{RELEASE_SHA}',clock_timestamp(),
  '{{}}'::jsonb,null
);
""",
        "operation_command_expired_before_application",
    )
    state = psql(container, f"""
select concat_ws('|',state,claimed_by_service,applied_at is null)
from private.operation_commands where id='{command_id}';
""").stdout.strip()
    if state != f"claimed|{worker}|t":
        raise VerificationError(f"expired ACK mutated command: {state}")
    print("PASS applied ACK rechecks command expiry against database time")


def verify_qualification_expiry_at_application(container: str) -> None:
    qualification_id = "b0b0b0b0-b0b0-40b0-80b0-b0b0b0b0b0b0"
    command_id = "b1b1b1b1-b1b1-41b1-81b1-b1b1b1b1b1b1"
    risk_policy_version = "b2b2b2b2-b2b2-42b2-82b2-b2b2b2b2b2b2"
    worker = "b3b3b3b3-b3b3-43b3-83b3-b3b3b3b3b3b3"
    strategy = psql(
        container,
        "select id from public.strategy_versions order by created_at limit 1;",
    ).stdout.strip()
    expect_failure(
        container,
        f"""
insert into private.qualifications (
  id,environment,status,release_sha,ledger_checkpoint,dataset_version,
  execution_policy_version,execution_policy_sha256,risk_policy_sha256,
  strategy_version_id,risk_policy_version_id,valid_from,valid_until,
  g1_status,g1_checked_at,g1_evidence_id,g2_status,g2_checked_at,g2_evidence_id
)
select '{qualification_id}','paper','expired','{RELEASE_SHA}',
  'expired-checkpoint','expired-dataset',execution_policy_version,
  execution_policy_sha256,risk_policy_sha256,'{strategy}','{risk_policy_version}',
  clock_timestamp()-interval '3 minutes',clock_timestamp()-interval '1 minute',
  'pass',clock_timestamp()-interval '2 minutes','{EVIDENCE}',
  'pass',clock_timestamp()-interval '2 minutes','{EVIDENCE}'
from private.execution_controls where account_id='paper-primary';
alter table private.operation_commands
  disable trigger guard_operation_command_v1_qualification;
insert into private.operation_commands (
  id,command_type,state,requested_change,revision,evidence_id,target_release_sha,
  requester_user_id,reviewer_user_id,requested_at,reviewed_at,expires_at,
  idempotency_key
)
select '{command_id}','paper_resume','approved',jsonb_build_object(
  'account_id','paper-primary','environment','paper',
  'expected_state_version',control_epoch,
  'qualification_id','{qualification_id}',
  'strategy_version_id','{strategy}',
  'risk_policy_version_id','{risk_policy_version}',
  'release_sha','{RELEASE_SHA}','ledger_checkpoint','expired-checkpoint',
  'execution_policy_version',execution_policy_version,
  'execution_policy_sha256',execution_policy_sha256,
  'risk_policy_sha256',risk_policy_sha256,
  'reason_code','qualified_resume'
),1,'{EVIDENCE}','{RELEASE_SHA}','{OPERATOR}','{RISK}',
  clock_timestamp()-interval '2 minutes',clock_timestamp()-interval '90 seconds',
  clock_timestamp()+interval '1 hour','expired-qualification-at-apply'
from private.execution_controls where account_id='paper-primary';
alter table private.operation_commands
  enable trigger guard_operation_command_v1_qualification;
{jwt_claim_sql(worker, role='service_role')}
select command_id from worker_api.claim_operation_command_batch(
  '{worker}','{RELEASE_SHA}',clock_timestamp(),25
) where command_id='{command_id}';
select state from worker_api.acknowledge_operation_command(
  '{command_id}','applied','{worker}','{RELEASE_SHA}',clock_timestamp(),
  '{{}}'::jsonb,null
);
""",
        "qualified_evidence_bundle_expired_or_mismatched_at_application",
    )
    state = psql(container, f"""
select concat_ws('|',
  (select state from private.operation_commands where id='{command_id}'),
  (select execution_enabled from private.execution_controls
    where account_id='paper-primary'),
  (select control_epoch from private.execution_controls
    where account_id='paper-primary')
);
""").stdout.strip()
    if state != "claimed|f|1":
        raise VerificationError(f"expired qualification changed control: {state}")

    print("PASS non-v1/expired qualification is rejected again at Worker apply")


def verify_reconciliation_keyset(container: str) -> None:
    worker = "cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd"
    decision = "10101010-1010-4010-8010-101010101010"
    risk = "20202020-2020-4020-8020-202020202020"
    psql(container, f"""
insert into private.execution_decisions (
  id,account_id,environment,strategy_version_id,symbol,action,
  decision_at,signal_valid_from,signal_valid_until,
  feature_snapshot_sha256,decision_sha256,release_sha
) values (
  '{decision}','paper-primary','paper','verifier-strategy','005930','buy',
  clock_timestamp()-interval '2 minutes',clock_timestamp()-interval '3 minutes',
  clock_timestamp()+interval '1 hour','{'3' * 64}','{'4' * 64}','{RELEASE_SHA}'
);
insert into private.risk_results (
  id,decision_id,account_id,environment,strategy_version_id,
  risk_policy_sha256,control_epoch,allowed,reason_codes,result_sha256,
  evaluated_at,expires_at,release_sha
) values (
  '{risk}','{decision}','paper-primary','paper','verifier-strategy',
  (select risk_policy_sha256 from private.execution_controls where account_id='paper-primary'),
  1,true,array[]::text[],'{'5' * 64}',clock_timestamp()-interval '1 minute',
  clock_timestamp()+interval '1 hour','{RELEASE_SHA}'
);
insert into private.order_intents (
  id,semantic_key_sha256,account_id,environment,strategy_version_id,
  decision_id,risk_result_id,correlation_id,symbol,side,quantity,limit_price_krw,
  decision_at,signal_valid_from,signal_valid_until,eligible_at,expires_at,
  execution_policy_version,execution_policy_sha256,cost_schedule_version,
  cost_schedule_evidence_sha256,cash_commitment_krw,risk_policy_sha256,
  control_epoch,release_sha
)
select
  md5('recon-intent-'||value::text)::uuid,
  encode(digest('recon-semantic-'||value::text,'sha256'),'hex'),
  'paper-primary','paper','verifier-strategy','{decision}','{risk}',
  md5('recon-intent-'||value::text)::uuid,'005930','buy',1,10000,
  clock_timestamp()-interval '2 minutes',clock_timestamp()-interval '3 minutes',
  clock_timestamp()+interval '1 hour',clock_timestamp()-interval '1 minute',
  clock_timestamp()+interval '1 hour','unapproved',
  (select execution_policy_sha256 from private.execution_controls where account_id='paper-primary'),
  'verifier-cost','{'6' * 64}',10000,
  (select risk_policy_sha256 from private.execution_controls where account_id='paper-primary'),
  1,'{RELEASE_SHA}'
from generate_series(1,60) as value;
insert into private.execution_reconciliation_state (
  intent_id,priority,state,next_reconcile_at
)
select id,30,'pending',clock_timestamp()-interval '1 minute'
from private.order_intents where decision_id='{decision}';
""")
    result = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
create temp table first_batch as
select * from worker_api.claim_execution_reconciliation_batch(
  '{worker}','{RELEASE_SHA}',clock_timestamp(),50,null,null,30);
create temp table second_batch as
select * from worker_api.claim_execution_reconciliation_batch(
  '{worker}','{RELEASE_SHA}',clock_timestamp(),50,30,
  (select intent_id from first_batch order by intent_id desc limit 1),30);
select concat_ws('|',
  (select count(*) from first_batch),
  (select count(*) from second_batch),
  (select count(distinct intent_id) from (
    select intent_id from first_batch union all select intent_id from second_batch
  ) as all_claims)
);
select state from worker_api.complete_execution_reconciliation(
  (select intent_id from first_batch order by intent_id limit 1),
  '{worker}','{RELEASE_SHA}',
  (select lease_fencing_token from first_batch order by intent_id limit 1),
  clock_timestamp(),'reschedule',clock_timestamp()+interval '1 minute',
  'reconciliation_positive_control'
);
""").stdout.strip().splitlines()[-2:]
    if result != ["50|10|60", "pending"]:
        raise VerificationError(f"reconciliation keyset/starvation mismatch: {result}")
    old_token = psql(container, f"""
select fencing_token from private.worker_leases where account_id='paper-primary';
""").stdout.strip()
    psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select idempotent from worker_api.release_worker_lease(
  'paper-primary','{worker}',{old_token},clock_timestamp(),'{RELEASE_SHA}'
);
""")
    new_token = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select fencing_token from worker_api.acquire_worker_lease(
  'paper-primary','{worker}',clock_timestamp(),30,'{RELEASE_SHA}'
);
""").stdout.strip().splitlines()[-1]
    remaining = psql(container, """
select intent_id from private.execution_reconciliation_state
where state='leased' order by intent_id limit 1;
""").stdout.strip()
    legacy_signature_present = psql(container, """
select to_regprocedure(
  'worker_api.complete_execution_reconciliation(uuid,text,timestamptz,text,timestamptz,text)'
) is not null;
""").stdout.strip()
    if legacy_signature_present != "t":
        raise VerificationError("legacy reconciliation upgrade overload is missing")
    expect_failure(
        container,
        jwt_claim_sql(worker, role="service_role") + f"""
select * from worker_api.complete_execution_reconciliation(
  '{remaining}','{worker}',clock_timestamp(),'reschedule',
  clock_timestamp()+interval '1 minute','legacy_tokenless_completion'
);
""",
        "worker_upgrade_required",
    )
    state_after_legacy = psql(container, f"""
select state from private.execution_reconciliation_state
where intent_id='{remaining}';
""").stdout.strip()
    if state_after_legacy != "leased":
        raise VerificationError("legacy reconciliation overload mutated state")
    expect_failure(
        container,
        jwt_claim_sql(worker, role="service_role") + f"""
select * from worker_api.complete_execution_reconciliation(
  '{remaining}','{worker}','{RELEASE_SHA}',{old_token},clock_timestamp(),
  'reschedule',clock_timestamp()+interval '1 minute','stale_fencing_token'
);
""",
        "reconciliation_lease_not_owned_current_or_fenced",
    )
    expect_failure(
        container,
        jwt_claim_sql(worker, role="service_role") + f"""
select * from worker_api.complete_execution_reconciliation(
  '{remaining}','{worker}','{RELEASE_SHA}',{new_token},clock_timestamp(),
  'reschedule',clock_timestamp()+interval '1 minute','new_token_old_claim'
);
""",
        "reconciliation_lease_not_owned_current_or_fenced",
    )
    expect_failure(
        container,
        jwt_claim_sql(worker, role="service_role") + f"""
select * from worker_api.complete_execution_reconciliation(
  '{remaining}','{worker}','{NEXT_RELEASE_SHA}',{new_token},clock_timestamp(),
  'reschedule',clock_timestamp()+interval '1 minute','wrong_release_sha'
);
""",
        "reconciliation_lease_not_owned_current_or_fenced",
    )
    psql(container, f"""
update private.execution_reconciliation_state
set lease_expires_at=clock_timestamp()-interval '1 second'
where intent_id='{remaining}';
""")
    expect_failure(
        container,
        jwt_claim_sql(worker, role="service_role") + f"""
select * from worker_api.complete_execution_reconciliation(
  '{remaining}','{worker}','{RELEASE_SHA}',{new_token},
  clock_timestamp()-interval '1 minute','reschedule',
  clock_timestamp()+interval '1 minute','expired_state_lease'
);
""",
        "reconciliation_lease_not_owned_current_or_fenced",
    )
    psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select idempotent from worker_api.release_worker_lease(
  'paper-primary','{worker}',{new_token},clock_timestamp(),'{RELEASE_SHA}'
);
""")
    print(
        "PASS reconciliation keyset, exact claim-token fencing and "
        "legacy fail-closed completion"
    )


def verify_manual_reconciliation_atomicity(container: str) -> None:
    worker = "dededede-dede-4ede-8ede-dededededede"
    reason = "operator_reconciliation_required"
    baseline_epoch = int(psql(container, """
select control_epoch from private.execution_controls
where account_id='paper-primary';
""").stdout.strip())
    candidate = psql(container, """
select intent_id from private.execution_reconciliation_state
where state='leased'
order by intent_id
limit 1;
""").stdout.strip()
    if not candidate:
        raise VerificationError("manual reconciliation candidate is missing")
    claim = psql(container, f"""
update private.execution_reconciliation_state
set lease_expires_at=clock_timestamp()-interval '1 second',
    next_reconcile_at=clock_timestamp()-interval '1 second'
where intent_id='{candidate}';
{jwt_claim_sql(worker, role='service_role')}
select concat_ws('|',intent_id,lease_fencing_token)
from worker_api.claim_execution_reconciliation_batch(
  '{worker}','{RELEASE_SHA}',clock_timestamp(),1,null,null,30
)
where intent_id='{candidate}';
""").stdout.strip().splitlines()[-1].split("|")
    claimed_intent, token = claim
    if claimed_intent != candidate:
        raise VerificationError(f"manual reconciliation claim mismatch: {claim}")

    expect_failure(
        container,
        jwt_claim_sql(worker, role="service_role") + f"""
select * from worker_api.complete_execution_reconciliation(
  '{candidate}','{worker}','{RELEASE_SHA}',{int(token) + 1},
  clock_timestamp(),'manual',null,'{reason}'
);
""",
        "reconciliation_lease_not_owned_current_or_fenced",
    )
    negative = psql(container, f"""
select concat_ws('|',
  (select count(*) from private.reconciliation_breaks
    where summary_code='{reason}'),
  (select count(*) from private.incidents
    where incident_type='execution_reconciliation_manual_required'
      and summary_code='{reason}'),
  (select count(*) from private.delivery_outbox
    where event_type='execution_reconciliation_manual_required'
      and aggregate_id='{candidate}'),
  (select count(*) from private.audit_events
    where action='execution_reconciliation_manual_required'
      and resource_id='{candidate}')
);
""").stdout.strip()
    if negative != "0|0|0|0":
        raise VerificationError(f"stale manual completion emitted effects: {negative}")

    results = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select state from worker_api.complete_execution_reconciliation(
  '{candidate}','{worker}','{RELEASE_SHA}',{token},clock_timestamp(),
  'manual',null,'{reason}'
);
select state from worker_api.complete_execution_reconciliation(
  '{candidate}','{worker}','{RELEASE_SHA}',{token},clock_timestamp(),
  'manual',null,'{reason}'
);
reset role;
select concat_ws('|',
  (select state from private.execution_reconciliation_state
    where intent_id='{candidate}'),
  (select count(*) from private.reconciliation_breaks
    where summary_code='{reason}'),
  (select count(*) from private.incidents
    where incident_type='execution_reconciliation_manual_required'
      and summary_code='{reason}'),
  (select count(*) from private.delivery_outbox
    where event_type='execution_reconciliation_manual_required'
      and aggregate_id='{candidate}'),
  (select count(*) from private.audit_events
    where action='execution_reconciliation_manual_required'
      and resource_id='{candidate}'),
  (select count(*) from private.order_events
    where intent_id='{candidate}'
      and event_summary->>'reason_code'='{reason}'),
  (select execution_enabled from private.execution_controls
    where account_id='paper-primary'),
  (select control_epoch from private.execution_controls
    where account_id='paper-primary')
);
""").stdout.strip().splitlines()[-3:]
    expected = [
        "manual",
        "manual",
        f"manual|1|1|1|1|1|f|{baseline_epoch + 1}",
    ]
    if results != expected:
        raise VerificationError(f"manual reconciliation atomicity mismatch: {results}")
    print(
        "PASS manual reconciliation atomically stops execution and emits "
        "idempotent break/incident/outbox/audit evidence"
    )


def verify_semantic_dedupe_concurrency(container: str) -> dict[str, str]:
    decision_id = "40404040-4040-4040-8040-404040404040"
    risk_id = "50505050-5050-4050-8050-505050505050"
    feature_hash = "7" * 64
    cost_evidence_hash = "1" * 64
    policy_hash = "8" * 64
    risk_policy_hash = "9" * 64
    cost_schedule_hash = "a" * 64
    calendar_hash = "b" * 64
    tick_hash = "c" * 64
    volume_hash = "d" * 64
    corporate_action_hash = "e" * 64
    calendar_id = "60606060-6060-4060-8060-606060606060"
    worker = "70707070-7070-4070-8070-707070707070"
    fixture = psql(container, f"""
insert into private.market_calendars (
  id,environment,calendar_version,calendar_sha256,timezone_name,
  valid_from,valid_until,status,evidence_id,requested_by,reviewed_by
) values (
  '{calendar_id}','paper','dedupe-calendar','{calendar_hash}','Asia/Seoul',
  (clock_timestamp() at time zone 'Asia/Seoul')::date-1,
  (clock_timestamp() at time zone 'Asia/Seoul')::date+10,
  'approved','{EVIDENCE}','{ADMIN_1}','{ADMIN_2}'
);
insert into private.market_calendar_sessions (
  calendar_id,session_date,is_open,session_sha256
)
select
  '{calendar_id}',
  (clock_timestamp() at time zone 'Asia/Seoul')::date + offset_value,
  true,
  encode(digest(convert_to(
    '{calendar_id}:' || (
      (clock_timestamp() at time zone 'Asia/Seoul')::date + offset_value
    )::text,
    'UTF8'
  ),'sha256'),'hex')
from generate_series(0,3) as offsets(offset_value);
insert into private.paper_execution_model_registry (
  environment,model_version,tick_size_evidence_sha256,
  volume_model_evidence_sha256,corporate_action_evidence_sha256,
  market_calendar_id,status,evidence_id,requested_by,reviewed_by,
  effective_from,effective_until
) values (
  'paper','dedupe-model','{tick_hash}','{volume_hash}',
  '{corporate_action_hash}','{calendar_id}','approved','{EVIDENCE}',
  '{ADMIN_1}','{ADMIN_2}',clock_timestamp()-interval '1 day',
  clock_timestamp()+interval '1 day'
);
insert into private.paper_execution_policies (
  account_id,policy_version,policy_sha256,status,price_model,fill_model,
  parameters,evidence_id,requested_by,reviewed_by,effective_from,effective_until
) values (
  'paper-primary','dedupe-policy','{policy_hash}','approved',
  'next_executable_minute_v1','whole_share_volume_bounded_v1',
  jsonb_build_object(
    'corporate_action_evidence_sha256','{corporate_action_hash}',
    'execution_model_version','dedupe-model',
    'market_calendar_sha256','{calendar_hash}',
    'market_calendar_version','dedupe-calendar',
    'tick_size_evidence_sha256','{tick_hash}',
    'volume_model_evidence_sha256','{volume_hash}'
  ),'{EVIDENCE}','{ADMIN_1}','{ADMIN_2}',
  clock_timestamp()-interval '1 day',clock_timestamp()+interval '1 day'
);
insert into private.execution_cost_schedules (
  account_id,schedule_version,schedule_sha256,buy_commission_rate,
  sell_commission_rate,sell_tax_rate,settlement_days,status,evidence_id,
  requested_by,reviewed_by,effective_from,effective_until
) values (
  'paper-primary','dedupe-cost','{cost_schedule_hash}',0.001,0.001,0.002,0,
  'approved','{EVIDENCE}','{ADMIN_1}','{ADMIN_2}',
  clock_timestamp()-interval '1 day',clock_timestamp()+interval '1 day'
);
alter table private.execution_controls
  disable trigger guard_execution_control_qualification_freshness_v1;
update private.execution_controls
set execution_enabled=true,control_epoch=2,
    active_strategy_version_id='dedupe-strategy',
    execution_policy_version='dedupe-policy',
    execution_policy_sha256='{policy_hash}',risk_policy_sha256='{risk_policy_hash}',
    effective_at=clock_timestamp()-interval '1 minute',
    expires_at=clock_timestamp()+interval '1 day',
    updated_reason_code='verifier_semantic_race',updated_at=clock_timestamp()
where account_id='paper-primary';
alter table private.execution_controls
  enable trigger guard_execution_control_qualification_freshness_v1;
""" + jwt_claim_sql(worker, role="service_role") + f"""
select fencing_token from worker_api.acquire_worker_lease(
  'paper-primary','{worker}',clock_timestamp(),300,'{RELEASE_SHA}'
);
reset role;
with times as (
  select
    date_trunc('minute',clock_timestamp())-interval '1 minute' as decision_at,
    date_trunc('minute',clock_timestamp())-interval '2 minutes' as signal_from,
    date_trunc('minute',clock_timestamp())+interval '10 minutes' as signal_until,
    date_trunc('minute',clock_timestamp()) as eligible_at,
    date_trunc('minute',clock_timestamp())+interval '5 minutes' as expires_at,
    clock_timestamp()-interval '30 seconds' as risk_at,
    clock_timestamp()+interval '5 minutes' as risk_expires
)
select jsonb_build_object(
  'semantic_key',private.compute_order_semantic_key(
    'paper-primary','paper','dedupe-strategy','005930','buy',
    signal_from,signal_until,'dedupe-policy'
  ),
  'decision_at',decision_at,'signal_from',signal_from,
  'signal_until',signal_until,'eligible_at',eligible_at,
  'expires_at',expires_at,'risk_at',risk_at,'risk_expires',risk_expires,
  'fencing_token',(select fencing_token from private.worker_leases
    where account_id='paper-primary')
)
from times;
""").stdout.strip().splitlines()[-1]
    values = json.loads(fixture)

    def reserve_once(_: int) -> str:
        requested_id = str(uuid4())
        result = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select concat_ws('|',reserved,intent_id,reservation_id,reason_code)
from worker_api.reserve_order_intent(
  '{requested_id}','{values['semantic_key']}','paper-primary','paper',
  'dedupe-strategy','{decision_id}','{feature_hash}','{risk_id}',true,
  array[]::text[],'{values['risk_at']}','{values['risk_expires']}',
  '005930','buy',1,10000,'{values['decision_at']}','{values['signal_from']}',
  '{values['signal_until']}','dedupe-policy','dedupe-cost',
  '{cost_evidence_hash}',10010,'{values['eligible_at']}','{values['expires_at']}',
  2,'{worker}',{values['fencing_token']},'{RELEASE_SHA}'
);
""").stdout.strip().splitlines()[-1]
        return result

    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(reserve_once, range(100)))
    created = [row for row in results if row.startswith("t|")]
    duplicates = [row for row in results if row.startswith("f|")]
    if len(created) != 1 or len(duplicates) != 99:
        raise VerificationError(
            f"semantic first-create cardinality mismatch: created={len(created)} "
            f"duplicates={len(duplicates)} values={set(results)}"
        )
    _, intent_id, reservation_id, create_reason = created[0].split("|")
    if create_reason != "reserved":
        raise VerificationError(f"semantic create reason mismatch: {created[0]}")
    expected_duplicate = f"f|{intent_id}|{reservation_id}|duplicate_semantic_intent"
    if set(duplicates) != {expected_duplicate}:
        raise VerificationError(f"semantic canonical duplicate mismatch: {set(duplicates)}")
    counts = psql(container, f"""
select concat_ws('|',
  (select count(*) from private.order_intents where semantic_key_sha256='{values['semantic_key']}'),
  (select count(*) from private.order_reservations where intent_id='{intent_id}'),
  (select count(*) from private.reservation_events where intent_id='{intent_id}'),
  (select count(*) from private.execution_reconciliation_state where intent_id='{intent_id}'),
  (select count(*) from private.order_events where intent_id='{intent_id}'),
  (select count(*) from private.audit_events
    where action='order_intent_reserved' and correlation_id='{intent_id}'),
  (select count(*) from private.delivery_outbox
    where destination_type='audit_archive'
      and payload->'envelope'->>'correlation_id'='{intent_id}'),
  (select reserved_cash_krw from private.cash_balance_projection
    where account_id='paper-primary')
);
""").stdout.strip()
    if counts != "1|1|1|1|1|1|1|10010.0000":
        raise VerificationError(f"semantic first-create atomicity mismatch: {counts}")
    print("PASS 100-way first-create semantic dedupe and atomic reservation")
    return {
        "intent_id": intent_id,
        "reservation_id": reservation_id,
        "worker_id": worker,
        "fencing_token": str(values["fencing_token"]),
        "control_epoch": "2",
    }


def verify_execution_transition_guards(container: str) -> None:
    base = "private.execution_observation_transition_violation"
    valid = psql(container, f"""
select coalesce({base}(
  null,null,null,0,0,0,0,
  1,'open',clock_timestamp(),2,0,0,0,0,
  null,null,null,'[]'::jsonb
),'ok');
select coalesce({base}(
  1,'open',clock_timestamp()-interval '1 second',0,0,0,0,
  2,'partial_filled',clock_timestamp(),2,1,100,0,0,
  1,100,current_date,'[{{}},{{}}]'::jsonb
),'ok');
select coalesce({base}(
  2,'partial_filled',clock_timestamp()-interval '1 second',1,100,0,0,
  3,'canceled',clock_timestamp(),2,1,100,0,0,
  null,null,null,'[]'::jsonb
),'ok');
""").stdout.strip().splitlines()[-3:]
    if valid != ["ok", "ok", "ok"]:
        raise VerificationError(f"valid execution transitions rejected: {valid}")
    attacks = {
        "sequence_gap": f"""select {base}(
          1,'open',clock_timestamp(),0,0,0,0,
          3,'open',clock_timestamp(),2,0,0,0,0,
          null,null,null,'[]'::jsonb);""",
        "observed_at_regressed": f"""select {base}(
          1,'open',clock_timestamp(),0,0,0,0,
          2,'open',clock_timestamp()-interval '1 second',2,0,0,0,0,
          null,null,null,'[]'::jsonb);""",
        "terminal_delta_without_fill": f"""select {base}(
          1,'partial_filled',clock_timestamp(),1,100,0,0,
          2,'filled',clock_timestamp(),2,2,200,0,0,
          null,null,null,'[]'::jsonb);""",
        "zero_delta_with_fill": f"""select {base}(
          1,'partial_filled',clock_timestamp(),1,100,0,0,
          2,'partial_filled',clock_timestamp(),2,1,100,0,0,
          1,100,current_date,'[{{}},{{}}]'::jsonb);""",
        "rejected_after_fill": f"""select {base}(
          1,'partial_filled',clock_timestamp(),1,100,0,0,
          2,'rejected',clock_timestamp(),2,1,100,0,0,
          null,null,null,'[]'::jsonb);""",
    }
    expected = {
        "sequence_gap": "execution_observation_sequence_gap",
        "observed_at_regressed": "execution_observed_at_regressed",
        "terminal_delta_without_fill": "fill_delta_requires_complete_evidence",
        "zero_delta_with_fill": "non_fill_observation_delta_or_evidence_forbidden",
        "rejected_after_fill": "non_executed_terminal_observation_quantity_invalid",
    }
    for name, sql in attacks.items():
        value = psql(container, sql).stdout.strip().splitlines()[-1]
        if value != expected[name]:
            raise VerificationError(f"execution guard {name} mismatch: {value}")
    print("PASS exact sequence, status matrix and fill/accounting evidence guards")


def verify_pre_dispatch_recovery(container: str, canonical: dict[str, str]) -> None:
    intent_id = canonical["intent_id"]
    reservation_id = canonical["reservation_id"]
    crashed_worker = canonical["worker_id"]
    crashed_token = canonical["fencing_token"]
    worker = "71717171-7171-4171-8171-717171717171"
    epoch = canonical["control_epoch"]
    claim = psql(container, f"""
update private.execution_reconciliation_state
set priority=0,state='pending',next_reconcile_at=clock_timestamp()-interval '1 second',
    lease_owner=null,lease_expires_at=null
where intent_id='{intent_id}';
update private.worker_leases
set acquired_at=clock_timestamp()-interval '2 minutes',
    renewed_at=clock_timestamp()-interval '1 minute',
    expires_at=clock_timestamp()-interval '1 second'
where account_id='paper-primary' and holder_id='{crashed_worker}'
  and fencing_token={crashed_token};
""" + jwt_claim_sql(worker, role="service_role") + f"""
select concat_ws('|',intent_id,lease_fencing_token,reservation_fencing_token,
  control_epoch,reservation_control_epoch,intent_release_sha,lease_release_sha,
  recovery_disposition)
from worker_api.claim_execution_reconciliation_batch(
  '{worker}','{NEXT_RELEASE_SHA}',clock_timestamp(),1,null,null,120
);
""").stdout.strip().splitlines()[-1]
    claim_parts = claim.split("|")
    if claim_parts != [
        intent_id, str(int(crashed_token) + 1), crashed_token,
        epoch, epoch, RELEASE_SHA, NEXT_RELEASE_SHA,
        "pre_dispatch_release_takeover",
    ]:
        raise VerificationError(f"restart takeover claim mismatch: {claim}")
    token = claim_parts[1]
    expect_failure(
        container,
        jwt_claim_sql(crashed_worker, role="service_role") + f"""
select * from worker_api.fail_reserved_intent_pre_dispatch(
  '{intent_id}','{crashed_worker}',{crashed_token},{epoch},'{RELEASE_SHA}',
  clock_timestamp(),'pre_dispatch_crash_recovered'
);
""",
        "worker_fencing_token_stale",
    )
    expect_failure(container, f"""
select set_config('request.jwt.claim.role','service_role',false);
select set_config('request.jwt.claim.sub','{worker}',false);
select set_config('request.jwt.claims',jsonb_build_object(
  'role','service_role','sub','{worker}'
)::text,false);
begin;
insert into private.order_attempts (
  reservation_id,intent_id,account_id,environment,broker,lease_holder_id,
  fencing_token,control_epoch,client_order_key,request_sha256,prepared_at
) values (
  '{reservation_id}','{intent_id}','paper-primary','paper','internal_paper',
  '{worker}',{token},{epoch},'terminal-without-fill-attack','{'b' * 64}',
  clock_timestamp()
);
with attack as (
  select clock_timestamp() as observed_at
), payload as (
  select observed_at,encode(digest(convert_to(concat_ws('|',
    '{intent_id}','1','filled','provider-order-attack','',
    private.utc_iso8601(observed_at),'1','10000','0','0','','','',
    'terminal_without_fill_attack'
  ),'UTF8'),'sha256'),'hex') as observation_sha256
  from attack
)
select concat_ws('|',result.quarantined,result.reason_code)
from payload
cross join lateral worker_api.record_execution_observation(
  '{intent_id}',1,'filled','provider-order-attack',null,
  payload.observation_sha256,payload.observed_at,1,10000,0,0,
  null,null,null,'[]'::jsonb,'terminal_without_fill_attack','{worker}',{token}
) as result;
rollback;
""", "worker_fencing_token_stale")
    expect_failure(
        container,
        f"""
select set_config('request.jwt.claim.role','service_role',false);
select set_config('request.jwt.claim.sub','{worker}',false);
select set_config('request.jwt.claims',jsonb_build_object(
  'role','service_role','sub','{worker}'
)::text,false);
begin;
insert into private.order_attempts (
  reservation_id,intent_id,account_id,environment,broker,lease_holder_id,
  fencing_token,control_epoch,client_order_key,request_sha256,prepared_at
) values (
  '{reservation_id}','{intent_id}','paper-primary','paper','internal_paper',
  '{worker}',{token},{epoch},'attack-dispatch-started','{'a' * 64}',clock_timestamp()
);
select * from worker_api.fail_reserved_intent_pre_dispatch(
  '{intent_id}','{worker}',{token},{epoch},'{NEXT_RELEASE_SHA}',clock_timestamp(),
  'pre_dispatch_crash_recovered'
);
commit;
""",
        "pre_dispatch_failure_dispatch_already_started",
    )
    expect_failure(
        container,
        jwt_claim_sql(worker, role="service_role") + f"""
select * from worker_api.fail_reserved_intent_pre_dispatch(
  '{intent_id}','{worker}',{int(token) + 1},{epoch},'{NEXT_RELEASE_SHA}',
  clock_timestamp(),'pre_dispatch_crash_recovered'
);
""",
        "worker_fencing_token_stale",
    )
    result = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select concat_ws('|',state,reason_code,idempotent,observation_id)
from worker_api.fail_reserved_intent_pre_dispatch(
  '{intent_id}','{worker}',{token},{epoch},'{NEXT_RELEASE_SHA}',clock_timestamp(),
  'pre_dispatch_crash_recovered'
);
select concat_ws('|',state,reason_code,idempotent,observation_id)
from worker_api.fail_reserved_intent_pre_dispatch(
  '{intent_id}','{worker}',{token},{epoch},'{NEXT_RELEASE_SHA}',clock_timestamp(),
  'pre_dispatch_crash_recovered'
);
""").stdout.strip().splitlines()[-2:]
    first = result[0].split("|")
    second = result[1].split("|")
    if first[:3] != ["complete", "pre_dispatch_crash_recovered", "f"]:
        raise VerificationError(f"pre-dispatch recovery result mismatch: {result}")
    if second[:3] != ["complete", "pre_dispatch_crash_recovered", "t"] \
            or second[3] != first[3]:
        raise VerificationError(f"pre-dispatch recovery replay mismatch: {result}")
    state = psql(container, f"""
select concat_ws('|',
  (select count(*) from private.order_attempts where intent_id='{intent_id}'),
  (select count(*) from private.execution_observations
    where intent_id='{intent_id}' and event_type='failed_pre_dispatch'
      and provider_order_id is null),
  (select state from private.execution_reconciliation_state where intent_id='{intent_id}'),
  (select remaining_cash_krw from private.reservation_events
    where intent_id='{intent_id}' order by event_sequence desc limit 1),
  (select reserved_cash_krw from private.cash_balance_projection
    where account_id='paper-primary'),
  (select count(*) from private.audit_events
    where action='reserved_intent_failed_pre_dispatch' and correlation_id='{intent_id}'),
  (select count(*) from private.delivery_outbox
    where dedupe_key='pre-dispatch-failure:{intent_id}')
);
""").stdout.strip()
    if state != "0|1|complete|0|0.0000|1|1":
        raise VerificationError(f"pre-dispatch recovery atomicity mismatch: {state}")
    psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select idempotent from worker_api.release_worker_lease(
  'paper-primary','{worker}',{token},clock_timestamp(),'{NEXT_RELEASE_SHA}'
);
""")
    print("PASS reserve-only crash recovery is fenced, atomic and idempotent")


def verify_partial_resume_accounting_and_expiry(container: str) -> None:
    intent_id = "74747474-7474-4474-8474-747474747474"
    decision_id = "75757575-7575-4575-8575-757575757575"
    risk_id = "76767676-7676-4676-8676-767676767676"
    worker_a = "72727272-7272-4272-8272-727272727272"
    worker_b = "73737373-7373-4373-8373-737373737373"
    setup = psql(container, jwt_claim_sql(worker_a, role="service_role") + f"""
select fencing_token from worker_api.acquire_worker_lease(
  'paper-primary','{worker_a}',clock_timestamp(),120,'{RELEASE_SHA}'
);
reset role;
with times as (
  select
    date_trunc('minute',clock_timestamp())-interval '1 minute' as decision_at,
    date_trunc('minute',clock_timestamp())-interval '2 minutes' as signal_from,
    date_trunc('minute',clock_timestamp())+interval '10 minutes' as signal_until,
    date_trunc('minute',clock_timestamp()) as eligible_at,
    clock_timestamp()+interval '8 seconds' as expires_at,
    clock_timestamp()-interval '30 seconds' as risk_at,
    clock_timestamp()+interval '5 minutes' as risk_expires
)
select jsonb_build_object(
  'decision_at',decision_at,'signal_from',signal_from,
  'signal_until',signal_until,'eligible_at',eligible_at,
  'expires_at',expires_at,'risk_at',risk_at,'risk_expires',risk_expires,
  'semantic_key',private.compute_order_semantic_key(
    'paper-primary','paper','dedupe-strategy','000660','buy',
    signal_from,signal_until,'dedupe-policy'
  ),
  'fencing_token',(select fencing_token from private.worker_leases
    where account_id='paper-primary'),
  'control_epoch',(select control_epoch from private.execution_controls
    where account_id='paper-primary')
)
from times;
""").stdout.strip().splitlines()[-1]
    values = json.loads(setup)
    token_a = int(values["fencing_token"])
    reserved = psql(container, jwt_claim_sql(worker_a, role="service_role") + f"""
select concat_ws('|',reserved,intent_id,reservation_id,reason_code)
from worker_api.reserve_order_intent(
  '{intent_id}','{values['semantic_key']}','paper-primary','paper',
  'dedupe-strategy','{decision_id}','{'7' * 64}','{risk_id}',true,
  array[]::text[],'{values['risk_at']}','{values['risk_expires']}',
  '000660','buy',2,10000,'{values['decision_at']}','{values['signal_from']}',
  '{values['signal_until']}','dedupe-policy','dedupe-cost','{'1' * 64}',
  20020,'{values['eligible_at']}','{values['expires_at']}',2,
  '{worker_a}',{token_a},'{RELEASE_SHA}'
);
select concat_ws('|',attempt_id,reason_code)
from worker_api.mark_dispatch_started(
  '{intent_id}','paper-primary','paper','{worker_a}',{token_a},2,
  clock_timestamp(),'{'2' * 64}','paper:{intent_id}'
);
""").stdout.strip().splitlines()[-2:]
    if not reserved[0].startswith(f"t|{intent_id}|") or not reserved[1].endswith("|prepared"):
        raise VerificationError(f"partial fixture reserve/dispatch mismatch: {reserved}")

    observed = json.loads(psql(container, """
select jsonb_build_object(
  'observed_at',clock_timestamp(),
  'settlement_date',(clock_timestamp() at time zone 'Asia/Seoul')::date
);
""").stdout.strip())
    observation_hash = psql(container, f"""
select encode(digest(convert_to(concat_ws('|',
  '{intent_id}','1','partial_filled','paper:{intent_id}',
  'paper:{intent_id}:fill:1',private.utc_iso8601('{observed['observed_at']}'::timestamptz),
  '1','9000','9','0','1','9000','{observed['settlement_date']}',
  'partial_fill_verifier'
),'UTF8'),'sha256'),'hex');
""").stdout.strip()
    postings = "jsonb_build_array(" \
        "jsonb_build_object('account','POSITION_COST','debit_krw',9000,'credit_krw',0)," \
        "jsonb_build_object('account','FEES','debit_krw',9,'credit_krw',0)," \
        "jsonb_build_object('account','CASH','debit_krw',0,'credit_krw',9009))"
    record = psql(container, jwt_claim_sql(worker_a, role="service_role") + f"""
select concat_ws('|',observation_id,inserted,quarantined,reason_code)
from worker_api.record_execution_observation(
  '{intent_id}',1,'partial_filled','paper:{intent_id}',
  'paper:{intent_id}:fill:1','{observation_hash}',
  '{observed['observed_at']}',1,9000,9,0,1,9000,
  '{observed['settlement_date']}',{postings},'partial_fill_verifier',
  '{worker_a}',{token_a}
);
select concat_ws('|',observation_id,inserted,quarantined,reason_code)
from worker_api.record_execution_observation(
  '{intent_id}',1,'partial_filled','paper:{intent_id}',
  'paper:{intent_id}:fill:1','{observation_hash}',
  '{observed['observed_at']}',1,9000,9,0,1,9000,
  '{observed['settlement_date']}',{postings},'partial_fill_verifier',
  '{worker_a}',{token_a}
);
""").stdout.strip().splitlines()[-2:]
    first = record[0].split("|")
    duplicate = record[1].split("|")
    if first[1:] != ["t", "f", "recorded"] \
            or duplicate != [first[0], "f", "f", "duplicate_observation"]:
        raise VerificationError(f"partial fill/duplicate mismatch: {record}")
    partial_state = psql(container, f"""
select concat_ws('|',
  (select count(*) from private.fills where intent_id='{intent_id}'),
  (select count(*) from private.accounting_transactions
    where correlation_id='{intent_id}' and source_type='fill'),
  (select count(*) from private.accounting_postings as posting
    join private.accounting_transactions as transaction
      on transaction.id=posting.journal_entry_id
    where transaction.correlation_id='{intent_id}' and transaction.source_type='fill'),
  (select settled_cash_krw::bigint from private.cash_balance_projection
    where account_id='paper-primary'),
  (select reserved_cash_krw::bigint from private.cash_balance_projection
    where account_id='paper-primary'),
  (select pending_debit_cash_krw::bigint from private.cash_balance_projection
    where account_id='paper-primary'),
  (select quantity from private.position_projection
    where account_id='paper-primary' and symbol='000660'),
  (select average_cost_krw from private.position_projection
    where account_id='paper-primary' and symbol='000660'),
  (select remaining_cash_krw from private.reservation_events
    where intent_id='{intent_id}' order by event_sequence desc limit 1),
  (select state from private.execution_reconciliation_state where intent_id='{intent_id}')
);
""").stdout.strip()
    if partial_state != "1|1|3|10000000|10010|9009|1|9000.0000|10010|pending":
        raise VerificationError(f"partial fill ledger/projection mismatch: {partial_state}")

    expect_failure(
        container,
        f"""
begin;
update private.execution_controls
set execution_enabled=false,control_epoch=3,effective_at=clock_timestamp(),
    updated_at=clock_timestamp(),updated_reason_code='verifier_emergency_stop'
where account_id='paper-primary';
""" + jwt_claim_sql(worker_a, role="service_role") + f"""
select * from worker_api.mark_dispatch_started(
  '{intent_id}','paper-primary','paper','{worker_a}',{token_a},3,
  clock_timestamp(),'{'2' * 64}','paper:{intent_id}'
);
rollback;
""",
        "reservation_fencing_or_epoch_mismatch",
    )
    expect_failure(
        container,
        f"""
begin;
update private.execution_controls
set execution_enabled=false,control_epoch=3,effective_at=clock_timestamp(),
    updated_at=clock_timestamp(),updated_reason_code='verifier_emergency_stop'
where account_id='paper-primary';
""" + jwt_claim_sql(worker_a, role="service_role") + f"""
select * from worker_api.load_paper_execution_checkpoint(
  '{intent_id}','paper-primary','{worker_a}',{token_a},3,
  '{RELEASE_SHA}',clock_timestamp()
);
rollback;
""",
        "paper_checkpoint_control_revalidation_failed",
    )
    stopped_duplicate = psql(container, f"""
begin;
update private.execution_controls
set execution_enabled=false,control_epoch=3,effective_at=clock_timestamp(),
    updated_at=clock_timestamp(),updated_reason_code='verifier_emergency_stop'
where account_id='paper-primary';
""" + jwt_claim_sql(worker_a, role="service_role") + f"""
select concat_ws('|',inserted,quarantined,reason_code)
from worker_api.record_execution_observation(
  '{intent_id}',1,'partial_filled','paper:{intent_id}',
  'paper:{intent_id}:fill:1','{observation_hash}',
  '{observed['observed_at']}',1,9000,9,0,1,9000,
  '{observed['settlement_date']}',{postings},'partial_fill_verifier',
  '{worker_a}',{token_a}
);
rollback;
""").stdout.strip().splitlines()
    if "f|f|duplicate_observation" not in stopped_duplicate:
        raise VerificationError(
            f"pre-stop durable response was not accepted after stop: {stopped_duplicate}"
        )
    expect_failure(
        container,
        f"""
begin;
update private.worker_leases
set acquired_at=clock_timestamp()-interval '2 minutes',
    renewed_at=clock_timestamp()-interval '1 minute',
    expires_at=clock_timestamp()-interval '1 second'
where account_id='paper-primary' and holder_id='{worker_a}'
  and fencing_token={token_a};
""" + jwt_claim_sql(worker_b, role="service_role") + f"""
select fencing_token from worker_api.acquire_worker_lease(
  'paper-primary','{worker_b}',clock_timestamp(),30,'{NEXT_RELEASE_SHA}'
);
select * from worker_api.record_execution_observation(
  '{intent_id}',1,'partial_filled','paper:{intent_id}',
  'paper:{intent_id}:fill:1','{observation_hash}',
  '{observed['observed_at']}',1,9000,9,0,1,9000,
  '{observed['settlement_date']}',{postings},'partial_fill_verifier',
  '{worker_b}',{token_a + 1}
);
rollback;
""",
        "worker_fencing_token_stale",
    )
    unchanged_after_guards = psql(container, f"""
select concat_ws('|',
  (select count(*) from private.execution_observations where intent_id='{intent_id}'),
  (select count(*) from private.fills where intent_id='{intent_id}'),
  (select count(*) from private.accounting_transactions
    where correlation_id='{intent_id}' and source_type='fill'),
  (select control_epoch from private.execution_controls where account_id='paper-primary'),
  (select execution_enabled from private.execution_controls where account_id='paper-primary')
);
""").stdout.strip()
    if unchanged_after_guards != "1|1|1|2|t":
        raise VerificationError(
            f"stop/release guard mutated durable state: {unchanged_after_guards}"
        )

    claim = psql(container, f"""
update private.worker_leases
set acquired_at=clock_timestamp()-interval '2 minutes',
    renewed_at=clock_timestamp()-interval '1 minute',
    expires_at=clock_timestamp()-interval '1 second'
where account_id='paper-primary' and holder_id='{worker_a}'
  and fencing_token={token_a};
update private.execution_reconciliation_state
set state='pending',next_reconcile_at=clock_timestamp()-interval '1 second',
    lease_owner=null,lease_expires_at=null
where intent_id='{intent_id}';
""" + jwt_claim_sql(worker_b, role="service_role") + f"""
select concat_ws('|',intent_id,lease_fencing_token,reservation_fencing_token,
  latest_sequence,latest_status,latest_cumulative_quantity,
  observation_history_sha256,recovery_disposition,control_epoch)
from worker_api.claim_execution_reconciliation_batch(
  '{worker_b}','{RELEASE_SHA}',clock_timestamp(),1,null,null,120
);
""").stdout.strip().splitlines()[-1].split("|")
    if claim[:6] != [
        intent_id, str(token_a + 1), str(token_a), "1", "partial_filled", "1",
    ] or claim[7:] != ["same_release", "2"]:
        raise VerificationError(f"partial restart claim mismatch: {claim}")
    token_b = int(claim[1])
    expected_history_hash = hashlib.sha256(f"1:{observation_hash}".encode()).hexdigest()
    if claim[6] != expected_history_hash:
        raise VerificationError(f"partial history hash mismatch: {claim[6]}")
    expect_failure(
        container,
        jwt_claim_sql(worker_a, role="service_role") + f"""
select * from worker_api.load_paper_execution_checkpoint(
  '{intent_id}','paper-primary','{worker_a}',{token_a},2,
  '{RELEASE_SHA}',clock_timestamp()
);
""",
        "worker_fencing_token_stale",
    )
    checkpoint = psql(container, jwt_claim_sql(worker_b, role="service_role") + f"""
select concat_ws('|',intent_id,latest_sequence,latest_status,
  latest_cumulative_quantity,latest_cumulative_gross_krw,
  latest_cumulative_commission_krw,latest_cumulative_tax_krw,
  observation_history_sha256,intent_release_sha,lease_release_sha)
from worker_api.load_paper_execution_checkpoint(
  '{intent_id}','paper-primary','{worker_b}',{token_b},2,
  '{RELEASE_SHA}',clock_timestamp()
);
""").stdout.strip().splitlines()[-1]
    expected_checkpoint = (
        f"{intent_id}|1|partial_filled|1|9000|9|0|{expected_history_hash}|"
        f"{RELEASE_SHA}|{RELEASE_SHA}"
    )
    if checkpoint != expected_checkpoint:
        raise VerificationError(f"durable checkpoint mismatch: {checkpoint}")

    psql(container, f"""
select pg_sleep(greatest(
  extract(epoch from ('{values['expires_at']}'::timestamptz-clock_timestamp())),0
)+0.1);
""")
    expiry = psql(container, jwt_claim_sql(worker_b, role="service_role") + f"""
select concat_ws('|',observation_id,sequence,state,reason_code,idempotent)
from worker_api.expire_paper_intent_remainder(
  '{intent_id}','{worker_b}',{token_b},2,'{RELEASE_SHA}',clock_timestamp(),
  'paper_day_expired'
);
select concat_ws('|',observation_id,sequence,state,reason_code,idempotent)
from worker_api.expire_paper_intent_remainder(
  '{intent_id}','{worker_b}',{token_b},2,'{RELEASE_SHA}',clock_timestamp(),
  'paper_day_expired'
);
""").stdout.strip().splitlines()[-2:]
    expiry_first = expiry[0].split("|")
    expiry_replay = expiry[1].split("|")
    if expiry_first[1:] != ["2", "complete", "paper_day_expired", "f"] \
            or expiry_replay != [
                expiry_first[0], "2", "complete", "paper_day_expired", "t",
            ]:
        raise VerificationError(f"paper expiry/replay mismatch: {expiry}")
    final_state = psql(container, f"""
select concat_ws('|',
  (select count(*) from private.execution_observations where intent_id='{intent_id}'),
  (select count(*) from private.fills where intent_id='{intent_id}'),
  (select count(*) from private.accounting_transactions
    where correlation_id='{intent_id}' and source_type='fill'),
  (select count(*) from private.accounting_postings as posting
    join private.accounting_transactions as transaction
      on transaction.id=posting.journal_entry_id
    where transaction.correlation_id='{intent_id}' and transaction.source_type='fill'),
  (select settled_cash_krw::bigint from private.cash_balance_projection
    where account_id='paper-primary'),
  (select reserved_cash_krw::bigint from private.cash_balance_projection
    where account_id='paper-primary'),
  (select pending_debit_cash_krw::bigint from private.cash_balance_projection
    where account_id='paper-primary'),
  (select quantity from private.position_projection
    where account_id='paper-primary' and symbol='000660'),
  (select remaining_cash_krw from private.reservation_events
    where intent_id='{intent_id}' order by event_sequence desc limit 1),
  (select state from private.execution_reconciliation_state where intent_id='{intent_id}'),
  (select count(*) from private.delivery_outbox
    where dedupe_key='paper-expiry:{intent_id}'),
  (select count(*) from private.audit_events
    where action='paper_intent_remainder_expired' and correlation_id='{intent_id}')
);
""").stdout.strip()
    if final_state != "2|1|1|3|10000000|0|9009|1|0|complete|1|1":
        raise VerificationError(f"paper expiry atomicity mismatch: {final_state}")
    psql(container, jwt_claim_sql(worker_b, role="service_role") + f"""
select idempotent from worker_api.release_worker_lease(
  'paper-primary','{worker_b}',{token_b},clock_timestamp(),'{RELEASE_SHA}'
);
""")
    print("PASS partial fill ledger, durable restart checkpoint and DAY expiry")


def verify_cash_settlement_maturity(container: str) -> None:
    due_intent = "74747474-7474-4474-8474-747474747474"
    future_intent = "91919191-9191-4191-8191-919191919191"
    future_decision = "92929292-9292-4292-8292-929292929292"
    future_risk = "93939393-9393-4393-8393-939393939393"
    dead_intent = "94949494-9494-4494-8494-949494949494"
    dead_decision = "95959595-9595-4595-8595-959595959595"
    dead_risk = "96969696-9696-4696-8696-969696969696"
    worker = "97979797-9797-4797-8797-979797979797"
    future_schedule_hash = "6" * 64

    lease = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select fencing_token from worker_api.acquire_worker_lease(
  'paper-primary','{worker}',clock_timestamp(),300,'{RELEASE_SHA}'
);
""").stdout.strip().splitlines()[-1]
    fencing_token = int(lease)
    control_epoch = int(psql(container, """
select control_epoch from private.execution_controls
where account_id='paper-primary';
""").stdout.strip())

    psql(container, f"""
insert into private.execution_cost_schedules (
  account_id,schedule_version,schedule_sha256,buy_commission_rate,
  sell_commission_rate,sell_tax_rate,settlement_days,status,evidence_id,
  requested_by,reviewed_by,effective_from,effective_until
) values (
  'paper-primary','settlement-next-session','{future_schedule_hash}',
  0.001,0.001,0.002,1,'approved','{EVIDENCE}','{ADMIN_1}','{ADMIN_2}',
  clock_timestamp()-interval '1 day',clock_timestamp()+interval '1 day'
);
""")

    def create_buy_fill(
        intent_id: str,
        decision_id: str,
        risk_id: str,
        symbol: str,
        schedule_version: str,
        settlement_offset: int,
    ) -> str:
        values = json.loads(psql(container, f"""
with times as (
  select
    date_trunc('minute',clock_timestamp())-interval '1 minute' as decision_at,
    date_trunc('minute',clock_timestamp())-interval '2 minutes' as signal_from,
    date_trunc('minute',clock_timestamp())+interval '10 minutes' as signal_until,
    date_trunc('minute',clock_timestamp()) as eligible_at,
    clock_timestamp()+interval '5 minutes' as expires_at,
    clock_timestamp()-interval '30 seconds' as risk_at,
    clock_timestamp()+interval '5 minutes' as risk_expires
)
select jsonb_build_object(
  'decision_at',decision_at,'signal_from',signal_from,
  'signal_until',signal_until,'eligible_at',eligible_at,
  'expires_at',expires_at,'risk_at',risk_at,'risk_expires',risk_expires,
  'semantic_key',private.compute_order_semantic_key(
    'paper-primary','paper','dedupe-strategy','{symbol}','buy',
    signal_from,signal_until,'dedupe-policy'
  )
)
from times;
""").stdout.strip())
        prepared = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select concat_ws('|',reserved,intent_id,reason_code)
from worker_api.reserve_order_intent(
  '{intent_id}','{values['semantic_key']}','paper-primary','paper',
  'dedupe-strategy','{decision_id}','{'7' * 64}','{risk_id}',true,
  array[]::text[],'{values['risk_at']}','{values['risk_expires']}',
  '{symbol}','buy',1,10000,'{values['decision_at']}','{values['signal_from']}',
  '{values['signal_until']}','dedupe-policy','{schedule_version}','{'1' * 64}',
  10010,'{values['eligible_at']}','{values['expires_at']}',{control_epoch},
  '{worker}',{fencing_token},'{RELEASE_SHA}'
);
select concat_ws('|',attempt_id,reason_code)
from worker_api.mark_dispatch_started(
  '{intent_id}','paper-primary','paper','{worker}',{fencing_token},
  {control_epoch},clock_timestamp(),'{'2' * 64}','paper:{intent_id}'
);
""").stdout.strip().splitlines()[-2:]
        if not prepared[0].startswith(f"t|{intent_id}|") \
                or not prepared[1].endswith("|prepared"):
            raise VerificationError(
                f"cash settlement fixture reserve/dispatch mismatch: {prepared}"
            )

        observed = json.loads(psql(container, f"""
select jsonb_build_object(
  'observed_at',clock_timestamp(),
  'settlement_date',(
    select session_date
    from private.market_calendar_sessions
    where calendar_id='60606060-6060-4060-8060-606060606060'
      and session_date >= (clock_timestamp() at time zone 'Asia/Seoul')::date
      and is_open
    order by session_date
    offset {settlement_offset}
    limit 1
  )
);
""").stdout.strip())
        observation_hash = psql(container, f"""
select encode(digest(convert_to(concat_ws('|',
  '{intent_id}','1','filled','paper:{intent_id}',
  'paper:{intent_id}:fill:1',
  private.utc_iso8601('{observed['observed_at']}'::timestamptz),
  '1','9000','9','0','1','9000','{observed['settlement_date']}',
  'cash_settlement_verifier'
),'UTF8'),'sha256'),'hex');
""").stdout.strip()
        postings = (
            "jsonb_build_array("
            "jsonb_build_object('account','POSITION_COST','debit_krw',9000,"
            "'credit_krw',0),"
            "jsonb_build_object('account','FEES','debit_krw',9,'credit_krw',0),"
            "jsonb_build_object('account','CASH','debit_krw',0,'credit_krw',9009))"
        )
        recorded = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select concat_ws('|',inserted,quarantined,reason_code)
from worker_api.record_execution_observation(
  '{intent_id}',1,'filled','paper:{intent_id}',
  'paper:{intent_id}:fill:1','{observation_hash}',
  '{observed['observed_at']}',1,9000,9,0,1,9000,
  '{observed['settlement_date']}',{postings},'cash_settlement_verifier',
  '{worker}',{fencing_token}
);
""").stdout.strip().splitlines()[-1]
        if recorded != "t|f|recorded":
            raise VerificationError(
                f"cash settlement fixture fill mismatch: {recorded}"
            )
        obligation_id = psql(container, f"""
select obligation.id
from private.cash_settlement_obligations as obligation
where obligation.intent_id='{intent_id}';
""").stdout.strip()
        if not obligation_id:
            raise VerificationError("cash settlement obligation was not created")
        return obligation_id

    future_obligation = create_buy_fill(
        future_intent,
        future_decision,
        future_risk,
        "068270",
        "settlement-next-session",
        1,
    )
    due_obligation = psql(container, f"""
select id from private.cash_settlement_obligations
where intent_id='{due_intent}';
""").stdout.strip()
    if not due_obligation:
        raise VerificationError("partial-fill due settlement obligation is missing")

    due_count = psql(container, jwt_claim_sql(worker, role="service_role") + """
select due_count from worker_api.list_due_cash_settlement_accounts(
  clock_timestamp(),20
) where account_id='paper-primary';
""").stdout.strip().splitlines()[-1]
    boundary_state = psql(container, f"""
select concat_ws('|',
  (select settlement_date=(trade_at at time zone 'Asia/Seoul')::date
   from private.cash_settlement_obligations where id='{due_obligation}'),
  (select settlement_date>(clock_timestamp() at time zone 'Asia/Seoul')::date
   from private.cash_settlement_obligations where id='{future_obligation}'),
  (select pending_debit_cash_krw from private.account_snapshots
   where account_id='paper-primary' order by sequence desc limit 1),
  (select pending_debit_cash_krw from private.cash_balance_projection
   where account_id='paper-primary'),
  position(
    'session_date >= (p_observed_at at time zone ''Asia/Seoul'')::date'
    in pg_get_functiondef(
      'private.record_execution_observation_impl(uuid,integer,text,text,text,text,timestamptz,bigint,bigint,bigint,bigint,bigint,bigint,date,jsonb,text,text,bigint)'::regprocedure
    )
  ) > 0
);
""").stdout.strip().splitlines()[-1]
    boundary_parts = boundary_state.split("|")
    if (
        boundary_parts[:2] != ["t", "t"]
        or due_count != "1"
        or boundary_parts[2] != boundary_parts[3]
        or boundary_parts[4] != "t"
    ):
        raise VerificationError(
            f"cash settlement due/future/KST/snapshot mismatch: {boundary_state}"
        )

    expect_failure(
        container,
        f"""
begin;
alter table private.cash_settlement_obligations
  disable trigger guard_cash_settlement_obligation_scope_v1;
insert into private.cash_settlement_obligations (
  id,fill_id,trade_accounting_transaction_id,
  settlement_reclassification_transaction_id,intent_id,account_id,
  environment,obligation_type,amount_krw,trade_at,settlement_date,
  obligation_sha256,source_release_sha
)
select gen_random_uuid(),fill_id,trade_accounting_transaction_id,
  settlement_reclassification_transaction_id,intent_id,account_id,
  environment,obligation_type,amount_krw,
  '2026-01-01T15:30:00Z','2026-01-01','{'4' * 64}',source_release_sha
from private.cash_settlement_obligations where id='{due_obligation}';
""",
        "cash_settlement_obligation_date_check",
    )
    expect_failure(
        container,
        f"""
insert into private.cash_settlement_obligations (
  id,fill_id,trade_accounting_transaction_id,
  settlement_reclassification_transaction_id,intent_id,account_id,
  environment,obligation_type,amount_krw,trade_at,settlement_date,
  obligation_sha256,source_release_sha
)
select gen_random_uuid(),fill_id,trade_accounting_transaction_id,
  settlement_reclassification_transaction_id,intent_id,account_id,
  environment,obligation_type,amount_krw+1,trade_at,settlement_date,
  '{'5' * 64}',source_release_sha
from private.cash_settlement_obligations where id='{due_obligation}';
""",
        "cash_settlement_obligation_scope_mismatch",
    )

    claim_row = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select concat_ws('|',obligation_id,revision,claim_token)
from worker_api.claim_cash_settlement_batch(
  'paper-primary','{worker}','{RELEASE_SHA}',{fencing_token},
  clock_timestamp(),1
);
""").stdout.strip().splitlines()[-1].split("|")
    if claim_row[0] != due_obligation:
        raise VerificationError(f"cash settlement due claim mismatch: {claim_row}")
    claim_revision = int(claim_row[1])
    claim_token = claim_row[2]
    expect_failure(
        container,
        jwt_claim_sql(worker, role="service_role") + f"""
select worker_api.complete_cash_settlement(
  '{due_obligation}',{claim_revision},'{claim_token}','{worker}',
  '{RELEASE_SHA}',{fencing_token + 1},clock_timestamp()
);
""",
        "cash_settlement_claim_not_owned_current_or_expired",
    )
    receipts = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select concat_ws('|',value->>'replayed',value->>'claim_revision',
  value->>'settled_revision')
from (select worker_api.complete_cash_settlement(
  '{due_obligation}',{claim_revision},'{claim_token}','{worker}',
  '{RELEASE_SHA}',{fencing_token},clock_timestamp()) as value) as result;
select concat_ws('|',value->>'replayed',value->>'claim_revision',
  value->>'settled_revision')
from (select worker_api.complete_cash_settlement(
  '{due_obligation}',{claim_revision},'{claim_token}','{worker}',
  '{RELEASE_SHA}',{fencing_token},clock_timestamp()) as value) as result;
""").stdout.strip().splitlines()[-2:]
    expected_settled_revision = str(claim_revision + 1)
    if receipts != [
        f"false|{claim_revision}|{expected_settled_revision}",
        f"true|{claim_revision}|{expected_settled_revision}",
    ]:
        raise VerificationError(f"cash settlement exact replay mismatch: {receipts}")
    settled_state = psql(container, f"""
select concat_ws('|',
  (select state from private.cash_settlement_state
   where obligation_id='{due_obligation}'),
  (select count(*) from private.accounting_transactions
   where source_type='cash_settlement'
     and correlation_id='{due_intent}'),
  (select count(*) from private.cash_settlement_events
   where obligation_id='{due_obligation}' and event_type='settled'),
  (select pending_debit_cash_krw from private.account_snapshots
   where account_id='paper-primary' order by sequence desc limit 1),
  (select pending_debit_cash_krw from private.cash_balance_projection
   where account_id='paper-primary')
);
""").stdout.strip()
    settled_parts = settled_state.split("|")
    if (
        settled_parts[:3] != ["settled", "1", "1"]
        or settled_parts[3] != settled_parts[4]
    ):
        raise VerificationError(
            f"cash settlement completion projection mismatch: {settled_state}"
        )

    dead_obligation = create_buy_fill(
        dead_intent,
        dead_decision,
        dead_risk,
        "096770",
        "dedupe-cost",
        0,
    )
    psql(container, f"""
alter table private.cash_settlement_state
  disable trigger guard_cash_settlement_state_transition_v1;
update private.cash_settlement_state
set attempt_count=7,available_at=clock_timestamp()-interval '1 second',
    updated_at=greatest(clock_timestamp(),updated_at+interval '1 microsecond')
where obligation_id='{dead_obligation}' and state='pending';
alter table private.cash_settlement_state
  enable trigger guard_cash_settlement_state_transition_v1;
""")
    dead_claim = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select concat_ws('|',obligation_id,revision,claim_token)
from worker_api.claim_cash_settlement_batch(
  'paper-primary','{worker}','{RELEASE_SHA}',{fencing_token},
  clock_timestamp(),1
);
""").stdout.strip().splitlines()[-1].split("|")
    if dead_claim[0] != dead_obligation:
        raise VerificationError(f"cash settlement dead-letter claim mismatch: {dead_claim}")
    dead_revision = int(dead_claim[1])
    dead_token = dead_claim[2]
    failures = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select concat_ws('|',value->>'state',value->>'attempt_count',value->>'replayed')
from (select worker_api.fail_cash_settlement_attempt(
  '{dead_obligation}',{dead_revision},'{dead_token}','{worker}',
  '{RELEASE_SHA}',{fencing_token},clock_timestamp(),
  'settlement_worker_error') as value) as result;
select concat_ws('|',value->>'state',value->>'attempt_count',value->>'replayed')
from (select worker_api.fail_cash_settlement_attempt(
  '{dead_obligation}',{dead_revision},'{dead_token}','{worker}',
  '{RELEASE_SHA}',{fencing_token},clock_timestamp(),
  'settlement_worker_error') as value) as result;
""").stdout.strip().splitlines()[-2:]
    if failures != ["dead_letter|8|false", "dead_letter|8|true"]:
        raise VerificationError(
            f"cash settlement attempt-8 dead-letter mismatch: {failures}"
        )
    dead_state = psql(container, f"""
select concat_ws('|',state,attempt_count,
  (select count(*) from private.cash_settlement_events as event
   where event.obligation_id=state_row.obligation_id
     and event.event_type='dead_letter'),
  (select count(*) from private.incidents
   where incident_type='cash_settlement_dead_letter'
     and correlation_id='{dead_intent}'),
  (select count(*) from private.delivery_outbox
   where dedupe_key='cash-settlement-dead-letter:'||state_row.obligation_id::text)
)
from private.cash_settlement_state as state_row
where obligation_id='{dead_obligation}';
""").stdout.strip()
    if dead_state != "dead_letter|8|1|1|1":
        raise VerificationError(f"cash settlement dead-letter evidence mismatch: {dead_state}")

    psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select idempotent from worker_api.release_worker_lease(
  'paper-primary','{worker}',{fencing_token},clock_timestamp(),'{RELEASE_SHA}'
);
""")
    # Isolate the following legacy workflow fixture after proving the
    # dead-letter stop. The disposable setup resolves only its synthetic cash
    # break with already-reviewed fixture evidence, then re-opens execution
    # while the production qualification trigger is temporarily disabled.
    psql(container, f"""
update private.reconciliation_breaks
set state='resolved',evidence_id='{EVIDENCE}',
    resolution_command_id='{ACCOUNT_COMMAND}',resolved_at=clock_timestamp(),
    revision=revision+1
where account_id='paper-primary' and break_type='cash'
  and summary_code='settlement_worker_error' and state='open';
alter table private.execution_controls
  disable trigger guard_execution_control_qualification_freshness_v1;
update private.execution_controls
set execution_enabled=true,
    control_epoch=control_epoch+1,
    effective_at=greatest(clock_timestamp(),updated_at+interval '1 microsecond'),
    expires_at=clock_timestamp()+interval '1 day',
    updated_reason_code='verifier_next_isolated_case',
    updated_at=greatest(clock_timestamp(),updated_at+interval '1 microsecond')
where account_id='paper-primary';
alter table private.execution_controls
  enable trigger guard_execution_control_qualification_freshness_v1;
""")
    print(
        "PASS KST settlement boundary, due/future claims, exact replay, stale "
        "fence, snapshot pending cash, scope guard and attempt-8 dead-letter"
    )


def verify_unknown_resolution_v2(container: str) -> None:
    worker = "a1a1a1a1-a1a1-41a1-81a1-a1a1a1a1a1a1"
    cases = [
        {
            "name": "buy",
            "intent": "a2a2a2a2-a2a2-42a2-82a2-a2a2a2a2a2a2",
            "decision": "a3a3a3a3-a3a3-43a3-83a3-a3a3a3a3a3a3",
            "risk": "a4a4a4a4-a4a4-44a4-84a4-a4a4a4a4a4a4",
            "request": "a5a5a5a5-a5a5-45a5-85a5-a5a5a5a5a5a5",
            "review": "a6a6a6a6-a6a6-46a6-86a6-a6a6a6a6a6a6",
            "idempotency": "a7a7a7a7-a7a7-47a7-87a7-a7a7a7a7a7a7",
            "symbol": "035720",
            "side": "buy",
            "quantity": 2,
            "limit": 10000,
            "reserved_cash": 20020,
            "terminal": "filled",
            "fill_quantity": 2,
            "commission": 18,
            "tax": 0,
        },
        {
            "name": "sell",
            "intent": "b2a2a2a2-a2a2-42a2-82a2-a2a2a2a2a2a2",
            "decision": "b3a3a3a3-a3a3-43a3-83a3-a3a3a3a3a3a3",
            "risk": "b4a4a4a4-a4a4-44a4-84a4-a4a4a4a4a4a4",
            "request": "b5a5a5a5-a5a5-45a5-85a5-a5a5a5a5a5a5",
            "review": "b6a6a6a6-a6a6-46a6-86a6-a6a6a6a6a6a6",
            "idempotency": "b7a7a7a7-a7a7-47a7-87a7-a7a7a7a7a7a7",
            "symbol": "000660",
            "side": "sell",
            "quantity": 1,
            "limit": 8000,
            "reserved_cash": 0,
            "terminal": "filled",
            "fill_quantity": 1,
            "commission": 9,
            "tax": 18,
        },
        {
            "name": "no_fill",
            "intent": "c2a2a2a2-a2a2-42a2-82a2-a2a2a2a2a2a2",
            "decision": "c3a3a3a3-a3a3-43a3-83a3-a3a3a3a3a3a3",
            "risk": "c4a4a4a4-a4a4-44a4-84a4-a4a4a4a4a4a4",
            "request": "c5a5a5a5-a5a5-45a5-85a5-a5a5a5a5a5a5",
            "review": "c6a6a6a6-a6a6-46a6-86a6-a6a6a6a6a6a6",
            "idempotency": "c7a7a7a7-a7a7-47a7-87a7-a7a7a7a7a7a7",
            "symbol": "051910",
            "side": "buy",
            "quantity": 1,
            "limit": 10000,
            "reserved_cash": 10010,
            "terminal": "rejected",
            "fill_quantity": 0,
            "commission": 0,
            "tax": 0,
        },
    ]
    lease = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select fencing_token from worker_api.acquire_worker_lease(
  'paper-primary','{worker}',clock_timestamp(),300,'{RELEASE_SHA}'
);
""").stdout.strip().splitlines()[-1]
    fencing_token = int(lease)
    control = psql(container, """
select concat_ws('|',execution_enabled,control_epoch)
from private.execution_controls where account_id='paper-primary';
""").stdout.strip().split("|")
    if control[0] != "t":
        raise VerificationError(f"unknown V2 fixture control is not enabled: {control}")
    dispatch_epoch = int(control[1])
    kst_contracts = psql(container, """
select concat_ws('|',
  position(
    'session.session_date = (p_eligible_at at time zone ''Asia/Seoul'')::date'
    in pg_get_functiondef((select min(proc.oid)
      from pg_proc as proc join pg_namespace as namespace
        on namespace.oid=proc.pronamespace
      where namespace.nspname='private'
        and proc.proname='reserve_order_intent_impl'))
  ) > 0,
  position(
    'session.session_date >= (filled_time at time zone ''Asia/Seoul'')::date'
    in pg_get_functiondef(
      'private.assert_unknown_resolution_fill_manifest_v2(uuid,uuid,text,text,jsonb,timestamptz)'::regprocedure
    )
  ) > 0
);
""").stdout.strip()
    if kst_contracts != "t|t":
        raise VerificationError(f"KST reserve/unknown contract patch missing: {kst_contracts}")

    for case in cases:
        values = json.loads(psql(container, f"""
with boundary as (
  select
    case
      when clock_timestamp() - (
        date_trunc('day',clock_timestamp() at time zone 'Asia/Seoul')
          at time zone 'Asia/Seoul'
      ) >= interval '3 minutes'
      then date_trunc('day',clock_timestamp() at time zone 'Asia/Seoul')
        at time zone 'Asia/Seoul'
      else (
        date_trunc('day',clock_timestamp() at time zone 'Asia/Seoul')
          at time zone 'Asia/Seoul'
      ) - interval '1 day'
    end as boundary_midnight,
    clock_timestamp() as observed_now
), times as (
  select
    boundary_midnight as decision_at,
    boundary_midnight as signal_from,
    observed_now+interval '10 minutes' as signal_until,
    boundary_midnight+interval '1 minute' as eligible_at,
    boundary_midnight+interval '2 minutes' as boundary_filled_at,
    observed_now+interval '5 minutes' as expires_at,
    observed_now-interval '30 seconds' as risk_at,
    observed_now+interval '5 minutes' as risk_expires
  from boundary
)
select jsonb_build_object(
  'decision_at',decision_at,'signal_from',signal_from,
  'signal_until',signal_until,'eligible_at',eligible_at,
  'boundary_filled_at',boundary_filled_at,
  'expires_at',expires_at,'risk_at',risk_at,'risk_expires',risk_expires,
  'semantic_key',private.compute_order_semantic_key(
    'paper-primary','paper','dedupe-strategy','{case['symbol']}',
    '{case['side']}',signal_from,signal_until,'dedupe-policy'
  )
) from times;
""").stdout.strip())
        case["boundary_filled_at"] = values["boundary_filled_at"]
        prepared = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select concat_ws('|',reserved,intent_id,reason_code)
from worker_api.reserve_order_intent(
  '{case['intent']}','{values['semantic_key']}','paper-primary','paper',
  'dedupe-strategy','{case['decision']}','{'7' * 64}','{case['risk']}',true,
  array[]::text[],'{values['risk_at']}','{values['risk_expires']}',
  '{case['symbol']}','{case['side']}',{case['quantity']},{case['limit']},
  '{values['decision_at']}','{values['signal_from']}','{values['signal_until']}',
  'dedupe-policy','dedupe-cost','{'1' * 64}',{case['reserved_cash']},
  '{values['eligible_at']}','{values['expires_at']}',{dispatch_epoch},
  '{worker}',{fencing_token},'{RELEASE_SHA}'
);
select concat_ws('|',attempt_id,reason_code)
from worker_api.mark_dispatch_started(
  '{case['intent']}','paper-primary','paper','{worker}',{fencing_token},
  {dispatch_epoch},clock_timestamp(),'{'2' * 64}','paper:{case['intent']}'
);
""").stdout.strip().splitlines()[-2:]
        if not prepared[0].startswith(f"t|{case['intent']}|") \
                or not prepared[1].endswith("|prepared"):
            raise VerificationError(
                f"unknown V2 {case['name']} reserve/dispatch mismatch: {prepared}"
            )

    for case in cases:
        observed_at = psql(container, f"""
select '{case['boundary_filled_at']}'::timestamptz-interval '30 seconds';
""").stdout.strip()
        reason_code = "provider_state_ambiguous"
        observation_hash = psql(container, f"""
select encode(digest(convert_to(concat_ws('|',
  '{case['intent']}','1','unknown_requires_manual_check',
  'paper:{case['intent']}','',private.utc_iso8601('{observed_at}'::timestamptz),
  '0','0','0','0','','','', '{reason_code}'
),'UTF8'),'sha256'),'hex');
""").stdout.strip()
        unknown = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select concat_ws('|',observation_id,inserted,quarantined,reason_code)
from worker_api.record_execution_observation(
  '{case['intent']}',1,'unknown_requires_manual_check',
  'paper:{case['intent']}',null,'{observation_hash}','{observed_at}',
  0,0,0,0,null,null,null,'[]'::jsonb,'{reason_code}',
  '{worker}',{fencing_token}
);
reset role;
select event.event_summary->>'reconciliation_break_id'
from private.order_events as event
where event.intent_id='{case['intent']}'
  and event.event_type='manual_check_quarantined'
order by event.occurred_at desc limit 1;
""").stdout.strip().splitlines()[-2:]
        unknown_parts = unknown[0].split("|")
        if unknown_parts[1:] != ["t", "f", "recorded"] or not unknown[1]:
            raise VerificationError(
                f"unknown V2 {case['name']} quarantine mismatch: {unknown}"
            )
        case["unknown_observation"] = unknown_parts[0]
        case["break"] = unknown[1]

    resolution_epoch = int(psql(container, """
select control_epoch from private.execution_controls
where account_id='paper-primary';
""").stdout.strip())
    if resolution_epoch <= dispatch_epoch:
        raise VerificationError("unknown V2 quarantine did not advance control epoch")

    for index, case in enumerate(cases):
        evidence_hash = hashlib.sha256(
            f"unknown-v2-{case['name']}-evidence".encode()
        ).hexdigest()
        snapshot = json.loads(psql(container, f"""
select jsonb_build_object(
  'break_revision',(select revision from private.reconciliation_breaks
    where id='{case['break']}'),
  'cash_version',(select projection_version from private.cash_balance_projection
    where account_id='paper-primary'),
  'position_version',(select projection_version from private.position_projection
    where account_id='paper-primary' and symbol='{case['symbol']}'),
  'reservation_sequence',(select max(event_sequence)
    from private.reservation_events where intent_id='{case['intent']}'),
  'requested_at',clock_timestamp(),
  'evidence_captured_at',clock_timestamp()-interval '1 second',
  'filled_at','{case['boundary_filled_at']}'::timestamptz,
  'settlement_date',(select session_date
    from private.market_calendar_sessions
    where calendar_id='60606060-6060-4060-8060-606060606060'
      and session_date >= (
        '{case['boundary_filled_at']}'::timestamptz at time zone 'Asia/Seoul'
      )::date
      and is_open order by session_date limit 1)
);
""").stdout.strip())
        position_version = (
            "null"
            if snapshot["position_version"] is None
            else str(snapshot["position_version"])
        )
        if case["fill_quantity"]:
            missing_fills = (
                "jsonb_build_array(jsonb_build_object("
                "'fill_sequence',1,"
                f"'provider_order_id','paper:{case['intent']}',"
                f"'provider_execution_id','paper:{case['intent']}:resolved:1',"
                f"'quantity',{case['fill_quantity']},'price_krw',9000,"
                f"'commission_krw',{case['commission']},'tax_krw',{case['tax']},"
                f"'filled_at','{snapshot['filled_at']}'::timestamptz,"
                f"'settlement_date','{snapshot['settlement_date']}'::date,"
                f"'evidence_sha256','{evidence_hash}'"
                "))"
            )
        else:
            missing_fills = "'[]'::jsonb"
        request_draft = f"jsonb_build_object(" \
            "'schema_version',2," \
            f"'request_id','{case['request']}','environment','paper'," \
            f"'idempotency_key','{case['idempotency']}'," \
            "'command_type','close_unknown_execution'," \
            f"'break_id','{case['break']}','intent_id','{case['intent']}'," \
            f"'unknown_observation_id','{case['unknown_observation']}'," \
            f"'provider_order_id','paper:{case['intent']}'," \
            f"'evidence_artifact_uri','urn:sha256:{evidence_hash}'," \
            f"'evidence_sha256','{evidence_hash}'," \
            f"'evidence_captured_at','{snapshot['evidence_captured_at']}'::timestamptz," \
            "'reason_code','accounting_closure_requested'," \
            "'expected_break_state','open'," \
            f"'expected_break_revision',{snapshot['break_revision']}," \
            "'expected_reconciliation_state','manual'," \
            f"'expected_cash_projection_version',{snapshot['cash_version']}," \
            f"'expected_position_projection_version',{position_version}," \
            f"'expected_reservation_event_sequence',{snapshot['reservation_sequence']}," \
            f"'expected_control_epoch',{resolution_epoch}," \
            f"'terminal_status','{case['terminal']}'," \
            f"'missing_fills',{missing_fills}," \
            f"'requested_at','{snapshot['requested_at']}'::timestamptz," \
            f"'expires_at','{snapshot['requested_at']}'::timestamptz+interval '1 hour')"
        requested_raw = psql(container, jwt_claim_sql(OPERATOR) + f"""
with draft(value) as (select {request_draft}), grant_value(value) as (
  select api.issue_unknown_resolution_step_up_v2(jsonb_build_object(
    'schema_version',2,'bound_action','request',
    'bound_command_type','close_unknown_execution','command_payload',draft.value
  )) from draft
)
select api.request_unknown_resolution_v2(draft.value || grant_value.value)
from draft,grant_value;
""").stdout.strip().splitlines()[-1]
        requested = json.loads(requested_raw)
        if (
            requested["state"] != "requested"
            or requested["receipt_revision"] != 0
            or requested["accounting_mutation_allowed"] is not False
            or requested["resolution_complete"] is not False
        ):
            raise VerificationError(
                f"unknown V2 {case['name']} request mismatch: {requested}"
            )

        def review_draft(review_id: str) -> str:
            return (
                "jsonb_build_object('schema_version',2,"
                f"'review_id','{review_id}','command_id','{case['request']}',"
                "'command_type','close_unknown_execution',"
                "'reviewer_role','risk_approver','decision','approve',"
                "'reason_code','evidence_sufficient',"
                f"'expected_receipt_revision',{requested['receipt_revision']},"
                f"'expected_break_revision',{requested['break_revision']},"
                f"'request_digest_sha256','{requested['request_digest_sha256']}',"
                f"'evidence_sha256','{evidence_hash}',"
                "'reviewed_at',clock_timestamp())"
            )

        if index == 0:
            self_review_id = "a8a8a8a8-a8a8-48a8-88a8-a8a8a8a8a8a8"
            self_draft = review_draft(self_review_id)
            expect_failure(
                container,
                f"""
begin;
insert into private.role_assignments (user_id,role,reason)
values ('{OPERATOR}','risk_approver','unknown_v2_self_review_fixture');
{jwt_claim_sql(OPERATOR)}
with draft(value) as (select {self_draft}), grant_value(value) as (
  select api.issue_unknown_resolution_step_up_v2(jsonb_build_object(
    'schema_version',2,'bound_action','review',
    'bound_command_type','close_unknown_execution','command_payload',draft.value
  )) from draft
)
select api.review_unknown_resolution_v2(draft.value || grant_value.value)
from draft,grant_value;
""",
                "unknown_resolution_v2_self_review_forbidden",
            )

        approved_draft = review_draft(case["review"])
        approved_raw = psql(container, jwt_claim_sql(RISK) + f"""
with draft(value) as (select {approved_draft}), grant_value(value) as (
  select api.issue_unknown_resolution_step_up_v2(jsonb_build_object(
    'schema_version',2,'bound_action','review',
    'bound_command_type','close_unknown_execution','command_payload',draft.value
  )) from draft
)
select api.review_unknown_resolution_v2(draft.value || grant_value.value)
from draft,grant_value;
""").stdout.strip().splitlines()[-1]
        approved = json.loads(approved_raw)
        if (
            approved["state"] != "approved"
            or approved["receipt_revision"] != 1
            or approved["work_revision"] != 0
            or approved["accounting_mutation_allowed"] is not True
            or approved["resolution_complete"] is not False
        ):
            raise VerificationError(
                f"unknown V2 {case['name']} review mismatch: {approved}"
            )

        generic_count = psql(
            container,
            jwt_claim_sql(worker, role="service_role") + f"""
select count(*) from worker_api.claim_operation_command_batch(
  '{worker}','{RELEASE_SHA}',clock_timestamp(),25
) where command_id='{case['request']}';
""",
        ).stdout.strip().splitlines()[-1]
        if generic_count != "0":
            raise VerificationError(
                f"generic command claim accepted unknown V2: {generic_count}"
            )
        if index == 0:
            expect_failure(
                container,
                jwt_claim_sql(worker, role="service_role") + f"""
select * from worker_api.claim_unknown_resolution_v2(
  '{case['request']}','{worker}','{RELEASE_SHA}',{fencing_token},
  {approved['receipt_revision']},{approved['work_revision'] + 1},
  clock_timestamp()
);
""",
                "unknown_resolution_v2_claim_stale_or_not_claimable",
            )
        claim = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select concat_ws('|',command_id,claim_token,command_revision,work_revision)
from worker_api.claim_unknown_resolution_v2(
  '{case['request']}','{worker}','{RELEASE_SHA}',{fencing_token},
  {approved['receipt_revision']},{approved['work_revision']},clock_timestamp()
);
""").stdout.strip().splitlines()[-1].split("|")
        command_revision = int(claim[2])
        work_revision = int(claim[3])
        claim_token = claim[1]
        if claim[0] != case["request"]:
            raise VerificationError(
                f"unknown V2 {case['name']} dedicated claim mismatch: {claim}"
            )

        if index == 0:
            expect_failure(
                container,
                jwt_claim_sql(worker, role="service_role") + f"""
select * from worker_api.acknowledge_operation_command(
  '{case['request']}','applied','{worker}','{RELEASE_SHA}',clock_timestamp(),
  '{{}}'::jsonb,null
);
""",
                "operation_command_account_id_required",
            )
            expect_failure(
                container,
                jwt_claim_sql(worker, role="service_role") + f"""
select * from worker_api.acknowledge_operation_command(
  '{case['request']}','failed','{worker}','{RELEASE_SHA}',clock_timestamp(),
  '{{}}'::jsonb,'generic_ack_bypass_attempt'
);
""",
                "unknown_resolution_v2_command_transition_invalid",
            )
            for token_value, fence_value, epoch_value, expected_fragment in (
                (
                    "00000000-0000-4000-8000-000000000001",
                    fencing_token,
                    resolution_epoch,
                    "unknown_resolution_v2_apply_state_stale",
                ),
                (
                    claim_token,
                    fencing_token + 1,
                    resolution_epoch,
                    "unknown_resolution_v2_apply_gate_stale",
                ),
                (
                    claim_token,
                    fencing_token,
                    resolution_epoch + 1,
                    "unknown_resolution_v2_apply_gate_stale",
                ),
            ):
                expect_failure(
                    container,
                    jwt_claim_sql(worker, role="service_role") + f"""
select worker_api.apply_unknown_resolution_v2(
  '{case['request']}','{token_value}','{worker}','{RELEASE_SHA}',
  {fence_value},{command_revision},{work_revision},{epoch_value},
  clock_timestamp()
);
""",
                    expected_fragment,
                )

        before_counts = psql(container, f"""
select concat_ws('|',
  (select count(*) from private.fills where intent_id='{case['intent']}'),
  (select count(*) from private.accounting_transactions
   where correlation_id='{case['intent']}'),
  (select count(*) from private.cash_settlement_obligations
   where intent_id='{case['intent']}'),
  coalesce((select bool_and(
      obligation.settlement_date =
        (obligation.trade_at at time zone 'Asia/Seoul')::date
      and obligation.trade_at::date <> obligation.settlement_date
    ) from private.cash_settlement_obligations as obligation
    where obligation.intent_id='{case['intent']}'),true)
);
""").stdout.strip()
        applied_raw = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select worker_api.apply_unknown_resolution_v2(
  '{case['request']}','{claim_token}','{worker}','{RELEASE_SHA}',
  {fencing_token},{command_revision},{work_revision},{resolution_epoch},
  clock_timestamp()
);
""").stdout.strip().splitlines()[-1]
        applied = json.loads(applied_raw)
        if (
            applied["state"] != "applied"
            or applied["receipt_revision"] != command_revision + 1
            or applied["work_revision"] != work_revision + 1
            or applied["resolution_complete"] is not True
            or applied["inserted"] is not True
        ):
            raise VerificationError(
                f"unknown V2 {case['name']} application mismatch: {applied}"
            )
        replay_raw = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select worker_api.apply_unknown_resolution_v2(
  '{case['request']}','{claim_token}','{worker}','{RELEASE_SHA}',
  {fencing_token},{applied['receipt_revision']},{applied['work_revision']},
  {resolution_epoch},clock_timestamp()
);
""").stdout.strip().splitlines()[-1]
        replay = json.loads(replay_raw)
        if (
            replay["inserted"] is not False
            or replay["application_id"] != applied["application_id"]
            or replay["application_sha256"] != applied["application_sha256"]
        ):
            raise VerificationError(
                f"unknown V2 {case['name']} exact replay mismatch: {replay}"
            )
        after_counts = psql(container, f"""
select concat_ws('|',
  (select count(*) from private.fills where intent_id='{case['intent']}'),
  (select count(*) from private.accounting_transactions
   where correlation_id='{case['intent']}'),
  (select count(*) from private.cash_settlement_obligations
   where intent_id='{case['intent']}'),
  coalesce((select bool_and(
      obligation.settlement_date =
        (obligation.trade_at at time zone 'Asia/Seoul')::date
      and obligation.trade_at::date <> obligation.settlement_date
    ) from private.cash_settlement_obligations as obligation
    where obligation.intent_id='{case['intent']}'),true)
);
""").stdout.strip()
        expected_fill_count = 1 if case["fill_quantity"] else 0
        if case["fill_quantity"]:
            # fill + settlement reclassification are both immutable journals.
            expected_transaction_count = 2
            expected_obligation_count = 1
        else:
            expected_transaction_count = 0
            expected_obligation_count = 0
        if before_counts != "0|0|0|t" or after_counts != (
            f"{expected_fill_count}|{expected_transaction_count}|"
            f"{expected_obligation_count}|t"
        ):
            raise VerificationError(
                f"unknown V2 {case['name']} replay cardinality mismatch: "
                f"{before_counts} -> {after_counts}"
            )
        final_state = psql(container, f"""
select concat_ws('|',
  (select state from private.operation_commands where id='{case['request']}'),
  (select state from private.reconciliation_breaks where id='{case['break']}'),
  (select state from private.execution_reconciliation_state
   where intent_id='{case['intent']}'),
  (select status from private.incidents
   where incident_type='execution_unknown'
     and correlation_id='{case['intent']}' order by opened_at desc limit 1),
  (select count(*) from private.unknown_execution_resolution_applications_v2
   where command_id='{case['request']}'),
  (select coalesce(sum(case when posting.side='debit' then posting.amount_krw
      else -posting.amount_krw end),0)
   from private.accounting_postings as posting
   join private.accounting_transactions as transaction
     on transaction.id=posting.journal_entry_id
   where transaction.correlation_id='{case['intent']}')
);
""").stdout.strip()
        expected_balance = "0.0000" if case["fill_quantity"] else "0"
        if final_state != (
            f"applied|resolved|complete|resolved|1|{expected_balance}"
        ):
            raise VerificationError(
                f"unknown V2 {case['name']} final invariant mismatch: {final_state}"
            )

    outcome_state = psql(container, f"""
select concat_ws('|',
  (select quantity from private.position_projection
   where account_id='paper-primary' and symbol='035720'),
  (select quantity from private.position_projection
   where account_id='paper-primary' and symbol='000660'),
  (select count(*) from private.fills
   where intent_id='{cases[2]['intent']}'),
  (select remaining_cash_krw from private.reservation_events
   where intent_id='{cases[2]['intent']}' order by event_sequence desc limit 1)
);
""").stdout.strip()
    if outcome_state != "2|0|0|0":
        raise VerificationError(
            f"unknown V2 buy/sell/no-fill outcomes mismatch: {outcome_state}"
        )

    expected_cases = {case["intent"]: case for case in cases}
    for role_name, actor in (
        ("operator", OPERATOR),
        ("risk_approver", RISK),
        ("auditor", AUDITOR),
    ):
        projection_raw = psql(
            container,
            jwt_claim_sql(actor) + "select api.get_unknown_resolution_cases_v2();",
        ).stdout.strip().splitlines()[-1]
        projection = json.loads(projection_raw)
        if projection.get("schema_version") != 2 \
                or not isinstance(projection.get("cases"), list):
            raise VerificationError(
                f"unknown V2 {role_name} projection envelope mismatch: {projection}"
            )
        projected_by_intent = {
            item.get("intent_id"): item for item in projection["cases"]
            if item.get("intent_id") in expected_cases
        }
        if set(projected_by_intent) != set(expected_cases):
            raise VerificationError(
                f"unknown V2 {role_name} projection case set mismatch: "
                f"{sorted(projected_by_intent)}"
            )
        for intent_id, projected in projected_by_intent.items():
            expected = expected_cases[intent_id]
            request = projected.get("request") or {}
            review = projected.get("review") or {}
            work_receipt = projected.get("work_receipt") or {}
            application = projected.get("application_receipt") or {}
            postcondition = projected.get("postcondition") or {}
            if (
                projected.get("schema_version") != 2
                or projected.get("break_state") != "resolved"
                or projected.get("reconciliation_state") != "complete"
                or request.get("state") != "applied"
                or review.get("decision") != "approved"
                or work_receipt.get("state") != "applied"
                or application.get("terminal_status") != expected["terminal"]
                or postcondition.get("resolution_complete") is not True
                or postcondition.get("accounting_application_recorded") is not True
            ):
                raise VerificationError(
                    f"unknown V2 {role_name}/{expected['name']} projection state "
                    f"mismatch: {projected}"
                )

            pending = [projected]
            exposed_keys: set[str] = set()
            while pending:
                value = pending.pop()
                if isinstance(value, dict):
                    exposed_keys.update(value)
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)
            if "account_id" in exposed_keys or any(
                "raw" in key.lower() and "payload" in key.lower()
                for key in exposed_keys
            ):
                raise VerificationError(
                    f"unknown V2 {role_name} projection exposed a forbidden field"
                )
    psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select idempotent from worker_api.release_worker_lease(
  'paper-primary','{worker}',{fencing_token},clock_timestamp(),'{RELEASE_SHA}'
);
""")
    # The next test preserves the legacy evidence-only workflow. Re-open the
    # disposable fixture with the qualification trigger disabled only for this
    # superuser-owned setup statement; production paths remain fail closed.
    psql(container, """
alter table private.execution_controls
  disable trigger guard_execution_control_qualification_freshness_v1;
update private.execution_controls
set execution_enabled=true,
    control_epoch=control_epoch+1,
    effective_at=greatest(clock_timestamp(),updated_at+interval '1 microsecond'),
    expires_at=clock_timestamp()+interval '1 day',
    updated_reason_code='verifier_next_isolated_case',
    updated_at=greatest(clock_timestamp(),updated_at+interval '1 microsecond')
where account_id='paper-primary';
alter table private.execution_controls
  enable trigger guard_execution_control_qualification_freshness_v1;
""")
    print(
        "PASS unknown V2 buy/sell/no-fill accounting closure, exact replay, "
        "stale token/revision/fence/epoch, self-review, generic ACK denial and "
        "role-scoped Desktop projection"
    )


def verify_unknown_resolution_evidence_only(container: str) -> None:
    intent_id = "87878787-8787-4787-8787-878787878787"
    decision_id = "88888887-8888-4887-8888-888888888887"
    risk_id = "89898987-8989-4987-8989-898989898987"
    worker = "8a8a8a8a-8a8a-4a8a-8a8a-8a8a8a8a8a8a"
    command_id = "8b8b8b8b-8b8b-4b8b-8b8b-8b8b8b8b8b8b"
    idempotency_key = "8c8c8c8c-8c8c-4c8c-8c8c-8c8c8c8c8c8c"
    self_review_id = "8d8d8d8d-8d8d-4d8d-8d8d-8d8d8d8d8d8d"
    stale_review_id = "8e8e8e8e-8e8e-4e8e-8e8e-8e8e8e8e8e8e"
    review_id = "8f8f8f8f-8f8f-4f8f-8f8f-8f8f8f8f8f8f"
    request_evidence = "e" * 64
    review_evidence = "f" * 64
    setup = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select fencing_token from worker_api.acquire_worker_lease(
  'paper-primary','{worker}',clock_timestamp(),120,'{RELEASE_SHA}'
);
reset role;
with times as (
  select
    date_trunc('minute',clock_timestamp())-interval '1 minute' as decision_at,
    date_trunc('minute',clock_timestamp())-interval '2 minutes' as signal_from,
    date_trunc('minute',clock_timestamp())+interval '10 minutes' as signal_until,
    date_trunc('minute',clock_timestamp()) as eligible_at,
    clock_timestamp()+interval '5 minutes' as expires_at,
    clock_timestamp()-interval '30 seconds' as risk_at,
    clock_timestamp()+interval '5 minutes' as risk_expires
)
select jsonb_build_object(
  'decision_at',decision_at,'signal_from',signal_from,
  'signal_until',signal_until,'eligible_at',eligible_at,
  'expires_at',expires_at,'risk_at',risk_at,'risk_expires',risk_expires,
  'semantic_key',private.compute_order_semantic_key(
    'paper-primary','paper','dedupe-strategy','035420','buy',
    signal_from,signal_until,'dedupe-policy'
  ),
  'fencing_token',(select fencing_token from private.worker_leases
    where account_id='paper-primary'),
  'control_epoch',(select control_epoch from private.execution_controls
    where account_id='paper-primary')
)
from times;
""").stdout.strip().splitlines()[-1]
    values = json.loads(setup)
    token = int(values["fencing_token"])
    prepared = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select concat_ws('|',reserved,intent_id,reason_code)
from worker_api.reserve_order_intent(
  '{intent_id}','{values['semantic_key']}','paper-primary','paper',
  'dedupe-strategy','{decision_id}','{'7' * 64}','{risk_id}',true,
  array[]::text[],'{values['risk_at']}','{values['risk_expires']}',
  '035420','buy',1,10000,'{values['decision_at']}','{values['signal_from']}',
  '{values['signal_until']}','dedupe-policy','dedupe-cost','{'1' * 64}',
  10010,'{values['eligible_at']}','{values['expires_at']}',
  {values['control_epoch']},
  '{worker}',{token},'{RELEASE_SHA}'
);
select concat_ws('|',attempt_id,reason_code)
from worker_api.mark_dispatch_started(
  '{intent_id}','paper-primary','paper','{worker}',{token},
  {values['control_epoch']},
  clock_timestamp(),'{'2' * 64}','paper:{intent_id}'
);
""").stdout.strip().splitlines()[-2:]
    if not prepared[0].startswith(f"t|{intent_id}|") \
            or not prepared[1].endswith("|prepared"):
        raise VerificationError(f"unknown fixture reserve/dispatch mismatch: {prepared}")

    observed_at = psql(container, "select clock_timestamp();").stdout.strip()
    reason_code = "provider_state_ambiguous"
    observation_hash = psql(container, f"""
select encode(digest(convert_to(concat_ws('|',
  '{intent_id}','1','unknown_requires_manual_check','paper:{intent_id}',
  '',private.utc_iso8601('{observed_at}'::timestamptz),
  '0','0','0','0','','','',
  '{reason_code}'
),'UTF8'),'sha256'),'hex');
""").stdout.strip()
    unknown = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select concat_ws('|',inserted,quarantined,reason_code)
from worker_api.record_execution_observation(
  '{intent_id}',1,'unknown_requires_manual_check','paper:{intent_id}',
  null,'{observation_hash}','{observed_at}',0,0,0,0,
  null,null,null,'[]'::jsonb,'{reason_code}','{worker}',{token}
);
reset role;
select event.event_summary->>'reconciliation_break_id'
from private.order_events as event
where event.intent_id='{intent_id}' and event.observation_id is not null
order by event.occurred_at desc limit 1;
""").stdout.strip().splitlines()[-2:]
    if unknown[0] != "t|f|recorded" or not unknown[1]:
        raise VerificationError(f"accepted unknown did not create linked break: {unknown}")
    break_id = unknown[1]

    request_draft = f"jsonb_build_object('schema_version',1," \
        f"'request_id','{command_id}','environment','paper'," \
        f"'idempotency_key','{idempotency_key}'," \
        "'command_type','resolve_unknown_execution'," \
        f"'break_id','{break_id}','evidence_sha256','{request_evidence}'," \
        "'reason_code','evidence_review_requested','expected_break_state','open'," \
        "'expected_break_revision',0,'requested_at',clock_timestamp()," \
        "'expires_at',clock_timestamp()+interval '1 hour')"
    requested = psql(container, jwt_claim_sql(OPERATOR) + f"""
with draft(value) as (select {request_draft}), grant_value(value) as (
  select api.issue_step_up_grant_v1(jsonb_build_object(
    'schema_version',1,'bound_action','request',
    'bound_command_type','resolve_unknown_execution','command_payload',draft.value
  )) from draft
), response(value) as (
  select api.request_unknown_resolution_v1(draft.value || grant_value.value)
  from draft,grant_value
)
select concat_ws('|',value->>'state',value->>'break_revision',
  value->>'accounting_mutation_allowed',value->>'resolution_complete')
from response;
""").stdout.strip().splitlines()[-1]
    if requested != "requested|1|false|false":
        raise VerificationError(f"unknown resolution request mismatch: {requested}")

    def review_draft(review_value: str, break_revision: int) -> str:
        return f"jsonb_build_object('schema_version',1,'review_id','{review_value}'," \
            f"'command_id','{command_id}'," \
            "'command_type','resolve_unknown_execution'," \
            "'reviewer_role','risk_approver','decision','approve'," \
            "'reason_code','evidence_sufficient','expected_receipt_revision',0," \
            f"'expected_break_revision',{break_revision}," \
            f"'evidence_sha256','{review_evidence}'," \
            "'reviewed_at',clock_timestamp())"

    self_draft = review_draft(self_review_id, 1)
    expect_failure(
        container,
        f"""
begin;
insert into private.role_assignments (user_id,role,reason)
values ('{OPERATOR}','risk_approver','self_review_negative_fixture');
""" + jwt_claim_sql(OPERATOR) + f"""
with draft(value) as (select {self_draft}), grant_value(value) as (
  select api.issue_step_up_grant_v1(jsonb_build_object(
    'schema_version',1,'bound_action','review',
    'bound_command_type','resolve_unknown_execution','command_payload',draft.value
  )) from draft
)
select api.review_unknown_resolution_v1(draft.value || grant_value.value)
from draft,grant_value;
""",
        "unknown_resolution_self_review_forbidden",
    )
    stale_draft = review_draft(stale_review_id, 0)
    expect_failure(
        container,
        jwt_claim_sql(RISK) + f"""
with draft(value) as (select {stale_draft}), grant_value(value) as (
  select api.issue_step_up_grant_v1(jsonb_build_object(
    'schema_version',1,'bound_action','review',
    'bound_command_type','resolve_unknown_execution','command_payload',draft.value
  )) from draft
)
select api.review_unknown_resolution_v1(draft.value || grant_value.value)
from draft,grant_value;
""",
        "unknown_resolution_break_not_reviewable_or_stale",
    )
    approved_draft = review_draft(review_id, 1)
    approved = psql(container, jwt_claim_sql(RISK) + f"""
with draft(value) as (select {approved_draft}), grant_value(value) as (
  select api.issue_step_up_grant_v1(jsonb_build_object(
    'schema_version',1,'bound_action','review',
    'bound_command_type','resolve_unknown_execution','command_payload',draft.value
  )) from draft
), response(value) as (
  select api.review_unknown_resolution_v1(draft.value || grant_value.value)
  from draft,grant_value
)
select concat_ws('|',value->>'state',value->>'break_state',
  value->>'accounting_mutation_allowed',value->>'resolution_complete',
  value->>'requires_balanced_accounting_adjustment')
from response;
""").stdout.strip().splitlines()[-1]
    if approved != "approved|resolution_requested|false|false|true":
        raise VerificationError(f"unknown evidence review mismatch: {approved}")

    expect_failure(
        container,
        f"""
insert into private.order_intents
select (jsonb_populate_record(
  null::private.order_intents,
  to_jsonb(existing_intent) || jsonb_build_object(
    'id','90909090-9090-4090-8090-909090909090',
    'semantic_key_sha256','{'0' * 64}',
    'correlation_id','90909090-9090-4090-8090-909090909090'
  )
)).*
from private.order_intents as existing_intent
where existing_intent.id='{intent_id}';
""",
        "unresolved_reconciliation_break_blocks_order_intent",
    )

    final_state = psql(container, jwt_claim_sql(worker, role="service_role") + f"""
create temp table unknown_claim as
select * from worker_api.claim_operation_command_batch(
  '{worker}','{RELEASE_SHA}',clock_timestamp(),25
);
reset role;
select concat_ws('|',
  (select state from private.operation_commands where id='{command_id}'),
  (select revision from private.operation_commands where id='{command_id}'),
  (select state from private.reconciliation_breaks where id='{break_id}'),
  (select revision from private.reconciliation_breaks where id='{break_id}'),
  (select resolution_command_id='{command_id}'::uuid
    from private.reconciliation_breaks where id='{break_id}'),
  (select count(*) from private.operation_command_reviews
    where command_id='{command_id}' and evidence_sha256='{review_evidence}'
      and request_digest_sha256=(select command_sha256
        from private.operation_commands where id='{command_id}')),
  (select requested_change->>'accounting_mutation_allowed'
    from private.operation_commands where id='{command_id}'),
  (select requested_change->>'resolution_complete'
    from private.operation_commands where id='{command_id}'),
  (select count(*) from unknown_claim where command_id='{command_id}'),
  (select count(*) from private.fills where intent_id='{intent_id}'),
  (select count(*) from private.accounting_transactions
    where correlation_id='{intent_id}'),
  (select count(*) from private.accounting_postings as posting
    join private.accounting_transactions as transaction
      on transaction.id=posting.journal_entry_id
    where transaction.correlation_id='{intent_id}'),
  (select state from private.execution_reconciliation_state
    where intent_id='{intent_id}'),
  (select remaining_cash_krw from private.reservation_events
    where intent_id='{intent_id}' order by event_sequence desc limit 1),
  (select execution_enabled from private.execution_controls
    where account_id='paper-primary'),
  (select control_epoch from private.execution_controls
    where account_id='paper-primary'),
  (select updated_reason_code from private.execution_controls
    where account_id='paper-primary')
);
""").stdout.strip().splitlines()[-1]
    expected = (
        "approved|1|resolution_requested|2|t|1|false|false|0|0|0|0|"
        f"manual|10010|f|{values['control_epoch'] + 1}|"
        "unresolved_reconciliation_break"
    )
    if final_state != expected:
        raise VerificationError(f"unknown evidence-only invariants mismatch: {final_state}")
    psql(container, jwt_claim_sql(worker, role="service_role") + f"""
select idempotent from worker_api.release_worker_lease(
  'paper-primary','{worker}',{token},clock_timestamp(),'{RELEASE_SHA}'
);
""")
    print("PASS accepted unknown is two-person evidence-only and cannot mutate ledger")


def verify_snapshot(container: str) -> None:
    psql(container, """
insert into private.position_projection (
  account_id,symbol,quantity,average_cost_krw,projection_version
) values ('paper-primary','005930',10,8000,1);
""")
    raw = psql(
        container,
        jwt_claim_sql(VIEWER) + "select api.get_desktop_operations_snapshot_v1();",
    ).stdout.strip().splitlines()[-1]
    snapshot = json.loads(raw)
    position = snapshot["positions"][0]
    if any(position[key] is not None for key in (
        "market_price_krw", "market_value_krw", "unrealized_pnl_krw",
        "market_data_source", "market_data_as_of",
    )) or position["market_data_status"] != "unavailable":
        raise VerificationError(f"snapshot fabricated valuation: {position}")
    runtime = snapshot["runtime_health"]
    if runtime["realtime_connected"] is not False or runtime["realtime_last_seen_at"] is not None:
        raise VerificationError("snapshot fabricated client Realtime connectivity")
    if "access_changes" not in snapshot:
        raise VerificationError("snapshot omitted access changes")
    print("PASS snapshot null valuation, causal safety and fail-closed Realtime")


def verify_arithmetic() -> None:
    # Fees and taxes post to their own ledgers and do not alter position cost.
    buy_total = 80_000 + (2 * 9_009)
    quantity = 12
    relief = (buy_total * 4) // quantity
    realized = (4 * 9_990) - relief
    residual = buy_total - relief
    if (buy_total, relief, realized, residual) != (98_018, 32_672, 7_288, 65_346):
        raise VerificationError("accounting golden-vector arithmetic mismatch")
    print("PASS integer moving-average golden vector")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def jwt_token(role: str, sub: str) -> str:
    now = int(time.time())
    header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = b64url(json.dumps({
        "role": role, "sub": sub, "aal": "aal2", "iat": now, "exp": now + 600,
        "session_id": f"postgrest-{sub}",
        "amr": [{"method": "totp", "timestamp": now}],
    }, separators=(",", ":")).encode())
    signature = b64url(hmac.new(JWT_SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def http_post(url: str, token: str | None, *, profile: str = "api", body: dict | None = None) -> tuple[int, str]:
    headers = {"Content-Type": "application/json", "Content-Profile": profile, "Accept-Profile": profile}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=json.dumps(body or {}).encode(), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, response.read().decode()
    except HTTPError as error:
        return error.code, error.read().decode()


def verify_postgrest(pg: str, network: str, postgrest: str) -> None:
    run([
        "docker", "run", "-d", "--name", postgrest, "--network", network,
        "-p", "127.0.0.1::3000",
        "-e", f"PGRST_DB_URI=postgres://authenticator:{DB_PASSWORD}@{pg}:5432/postgres",
        "-e", "PGRST_DB_SCHEMAS=api,worker_api",
        "-e", "PGRST_DB_ANON_ROLE=anon",
        "-e", f"PGRST_JWT_SECRET={JWT_SECRET}",
        POSTGREST_IMAGE,
    ])
    port_text = run(["docker", "port", postgrest, "3000/tcp"]).stdout.strip()
    port = port_text.rsplit(":", 1)[-1]
    probe_host = os.environ.get("G1_G2_POSTGREST_HOST", "127.0.0.1")
    root = f"http://{probe_host}:{port}"
    for _ in range(60):
        try:
            with urlopen(root, timeout=1):
                break
        except HTTPError as error:
            if error.code < 500:
                break
            time.sleep(0.5)
        except Exception:
            time.sleep(0.5)
    else:
        logs = run(["docker", "logs", postgrest], check=False)
        raise VerificationError(
            "PostgREST did not become ready:\n" + logs.stdout + logs.stderr
        )
    auth = jwt_token("authenticated", VIEWER)
    operator = jwt_token("authenticated", OPERATOR)
    service = jwt_token("service_role", "00000000-0000-4000-8000-000000000099")
    status, _ = http_post(f"{root}/rpc/get_desktop_operations_snapshot_v1", auth)
    if status != 200:
        raise VerificationError(f"authenticated snapshot failed through PostgREST: {status}")
    status, body = http_post(
        f"{root}/rpc/get_unknown_resolution_cases_v2", operator
    )
    if status != 200:
        raise VerificationError(
            f"operator unknown V2 projection failed through PostgREST: {status}"
        )
    projection = json.loads(body)
    projected_intents = {
        item.get("intent_id") for item in projection.get("cases", [])
    }
    expected_intents = {
        "a2a2a2a2-a2a2-42a2-82a2-a2a2a2a2a2a2",
        "b2a2a2a2-a2a2-42a2-82a2-a2a2a2a2a2a2",
        "c2a2a2a2-a2a2-42a2-82a2-a2a2a2a2a2a2",
    }
    if projection.get("schema_version") != 2 \
            or not expected_intents.issubset(projected_intents):
        raise VerificationError(
            "operator unknown V2 PostgREST projection contract mismatch"
        )
    status, body = http_post(f"{root}/rpc/get_unknown_resolution_cases_v2", auth)
    viewer_projection = json.loads(body) if status == 200 else None
    if status != 200 or viewer_projection.get("cases") != []:
        raise VerificationError(
            f"viewer unknown V2 projection was not empty through PostgREST: {status}"
        )
    status, _ = http_post(
        f"{root}/rpc/claim_operation_command_batch", auth, profile="worker_api",
        body={"p_holder_id": str(uuid4()), "p_release_sha": RELEASE_SHA,
              "p_now": "2026-07-14T12:00:00Z", "p_limit": 1},
    )
    if status not in (401, 403, 404):
        raise VerificationError(f"authenticated worker RPC unexpectedly allowed: {status}")
    status, _ = http_post(
        f"{root}/rpc/claim_operation_command_batch", service, profile="worker_api",
        body={"p_holder_id": str(uuid4()), "p_release_sha": RELEASE_SHA,
              "p_now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "p_limit": 1},
    )
    if status != 200:
        raise VerificationError(f"service worker RPC failed through PostgREST: {status}")
    status, _ = http_post(f"{root}/rpc/get_desktop_operations_snapshot_v1", service)
    if status not in (401, 403, 404):
        raise VerificationError(f"service desktop snapshot unexpectedly allowed: {status}")
    status, _ = http_post(f"{root}/rpc/get_unknown_resolution_cases_v2", service)
    if status not in (401, 403, 404):
        raise VerificationError(
            f"service unknown V2 projection unexpectedly allowed: {status}"
        )
    print(
        "PASS actual PostgREST anon/authenticated/service boundary and unknown "
        "V2 role projection"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-postgrest", action="store_true", help="local parser debugging only")
    args = parser.parse_args()
    suffix = uuid4().hex[:10]
    pg = f"msp-g1g2-pg-{suffix}"
    upgrade_pg = f"msp-g1g2-upgrade-{suffix}"
    operational_upgrade_pg = f"msp-g1g2-operational-upgrade-{suffix}"
    postgrest = f"msp-g1g2-rest-{suffix}"
    network = f"msp-g1g2-net-{suffix}"
    try:
        run(["docker", "info"])
        run(["docker", "network", "create", network])
        run([
            "docker", "run", "-d", "--name", pg, "--network", network,
            "-e", f"POSTGRES_PASSWORD={DB_PASSWORD}", POSTGRES_IMAGE,
        ])
        wait_for_postgres(pg)
        apply_repository(pg)
        psql(pg, fixture_sql())
        verify_catalog(pg)
        verify_strict_auth(pg)
        verify_access_maker_checker(pg)
        verify_account_opening(pg)
        verify_command_claim_allowlist(pg)
        verify_command_ack_expiry(pg)
        verify_qualification_expiry_at_application(pg)
        verify_reconciliation_keyset(pg)
        canonical = verify_semantic_dedupe_concurrency(pg)
        verify_execution_transition_guards(pg)
        verify_pre_dispatch_recovery(pg, canonical)
        verify_partial_resume_accounting_and_expiry(pg)
        verify_cash_settlement_maturity(pg)
        verify_unknown_resolution_v2(pg)
        verify_unknown_resolution_evidence_only(pg)
        verify_lease_and_outbox(pg)
        verify_snapshot(pg)
        verify_manual_reconciliation_atomicity(pg)
        verify_arithmetic()
        run([
            "docker", "run", "-d", "--name", upgrade_pg, "--network", network,
            "-e", f"POSTGRES_PASSWORD={DB_PASSWORD}", POSTGRES_IMAGE,
        ])
        wait_for_postgres(upgrade_pg)
        verify_populated_0015_upgrade(upgrade_pg)
        run([
            "docker", "run", "-d", "--name", operational_upgrade_pg,
            "--network", network,
            "-e", f"POSTGRES_PASSWORD={DB_PASSWORD}", POSTGRES_IMAGE,
        ])
        wait_for_postgres(operational_upgrade_pg)
        verify_populated_0023_operational_upgrade(operational_upgrade_pg)
        if not args.skip_postgrest:
            verify_postgrest(pg, network, postgrest)
        else:
            print("WARN PostgREST integration skipped by explicit flag")
        print("FINAL=PASS g1_g2_migration_verifier")
        return 0
    except (VerificationError, OSError, json.JSONDecodeError) as error:
        print(f"FINAL=FAIL {error}", file=sys.stderr)
        return 1
    finally:
        run(["docker", "rm", "-f", postgrest], check=False)
        run(["docker", "rm", "-f", operational_upgrade_pg], check=False)
        run(["docker", "rm", "-f", upgrade_pg], check=False)
        run(["docker", "rm", "-f", pg], check=False)
        run(["docker", "network", "rm", network], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
