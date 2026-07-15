import type { OperationsSnapshot } from "../src/lib/operationsContracts";

const actorTemplate = {
  actor_id: "11111111-1111-4111-8111-111111111111",
  display_name: "위험 승인자",
  roles: ["risk_approver" as const, "operator" as const]
};

const requesterTemplate = {
  actor_id: "22222222-2222-4222-8222-222222222222",
  display_name: "운영 요청자",
  roles: ["operator" as const]
};

export function makeOperationsSnapshot(): OperationsSnapshot {
  const actor = structuredClone(actorTemplate);
  const requester = structuredClone(requesterTemplate);
  return {
    schema_version: 1,
    generated_at: "2099-07-14T00:00:00.000Z",
    runtime_health: {
      schema_version: 1,
      environment: "paper",
      live_permitted: false,
      execution_enabled: false,
      overall_state: "fresh",
      as_of: "2099-07-14T00:00:00.000Z",
      state_version: 17,
      realtime_connected: true,
      realtime_last_seen_at: "2099-07-14T00:00:00.000Z",
      active_strategy_version_id: null,
      active_risk_policy_version_id: null,
      execution_policy_version: null,
      execution_policy_sha256: null,
      risk_policy_sha256: null,
      provider_contract_version: null,
      provider_openapi_sha256: null,
      worker_release_sha: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      worker_heartbeat_at: "2099-07-13T23:59:55.000Z",
      components: [
        {
          schema_version: 1,
          component: "control_plane",
          state: "fresh",
          observed_at: "2099-07-14T00:00:00.000Z",
          detail_code: "rpc_available"
        },
        {
          schema_version: 1,
          component: "realtime",
          state: "fresh",
          observed_at: "2099-07-14T00:00:00.000Z",
          detail_code: "channel_connected"
        },
        {
          schema_version: 1,
          component: "worker",
          state: "fresh",
          observed_at: "2099-07-13T23:59:55.000Z",
          detail_code: "heartbeat_fresh"
        }
      ],
      freshness_policy: {
        snapshot_max_age_seconds: 30,
        worker_heartbeat_max_age_seconds: 120,
        realtime_max_age_seconds: 45
      }
    },
    access: {
      schema_version: 1,
      signed_in: true,
      actor,
      session_state: "active",
      assurance_level: "aal2",
      active_step_up_grants: [
        {
          step_up_grant_id: "25252525-2525-4525-8525-252525252525",
          command_hash: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
          step_up_grant_issued_at: "2099-07-13T23:59:00.000Z",
          step_up_grant_expires_at: "2099-07-14T00:04:00.000Z",
          step_up_grant_one_time: true,
          step_up_grant_consumed_at: null,
          bound_action: "request",
          bound_command_type: "pause_paper"
        },
        {
          step_up_grant_id: "26262626-2626-4626-8626-262626262626",
          command_hash: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
          step_up_grant_issued_at: "2099-07-13T23:59:00.000Z",
          step_up_grant_expires_at: "2099-07-14T00:04:00.000Z",
          step_up_grant_one_time: true,
          step_up_grant_consumed_at: null,
          bound_action: "review",
          bound_command_type: "resume_paper"
        },
        {
          step_up_grant_id: "27272727-2727-4727-8727-272727272727",
          command_hash: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
          step_up_grant_issued_at: "2099-07-13T23:59:00.000Z",
          step_up_grant_expires_at: "2099-07-14T00:04:00.000Z",
          step_up_grant_one_time: true,
          step_up_grant_consumed_at: null,
          bound_action: "request",
          bound_command_type: "resume_paper"
        },
        {
          step_up_grant_id: "28282828-2828-4828-8828-282828282828",
          command_hash: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
          step_up_grant_issued_at: "2099-07-13T23:59:00.000Z",
          step_up_grant_expires_at: "2099-07-14T00:04:00.000Z",
          step_up_grant_one_time: true,
          step_up_grant_consumed_at: null,
          bound_action: "request",
          bound_command_type: "start_contract_test"
        }
      ],
      permissions: [
        "request_command",
        "review_command",
        "acknowledge_incident",
        "resolve_incident",
        "view_audit",
        "view_reconciliation"
      ]
    },
    access_changes: [],
    qualification: {
      schema_version: 1,
      qualification_id: "33333333-3333-4333-8333-333333333333",
      environment: "paper",
      status: "qualified",
      release_sha: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      ledger_checkpoint: "ledger-checkpoint-2026-07-14",
      dataset_version: "dataset-2026-07-14",
      strategy_version_id: "44444444-4444-4444-8444-444444444444",
      risk_policy_version_id: "55555555-5555-4555-8555-555555555555",
      valid_from: "2099-07-13T23:30:00.000Z",
      valid_until: "2099-07-14T01:00:00.000Z",
      g1: { status: "pass", checked_at: "2099-07-13T23:30:00.000Z", evidence_ref: "g1-report-17" },
      g2: { status: "pass", checked_at: "2099-07-13T23:35:00.000Z", evidence_ref: "g2-report-9" }
    },
    commands: [
      {
        schema_version: 1,
        command_id: "66666666-6666-4666-8666-666666666666",
        command_type: "pause_paper",
        environment: "paper",
        state: "approved",
        requested_by: requester,
        requested_at: "2099-07-13T23:50:00.000Z",
        expires_at: "2099-07-14T00:10:00.000Z",
        qualification_id: null,
        strategy_version_id: null,
        risk_policy_version_id: null,
        release_sha: null,
        ledger_checkpoint: null,
        command_hash: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        control_plane_receipt: {
          schema_version: 1,
          receipt_id: "77777777-7777-4777-8777-777777777777",
          command_id: "66666666-6666-4666-8666-666666666666",
          state: "approved",
          revision: 2,
          persisted_at: "2099-07-13T23:51:00.000Z",
          approved_at: "2099-07-13T23:51:00.000Z",
          approved_by: actor
        },
        worker_ack: null
      }
    ],
    pending_reviews: [
      {
        schema_version: 1,
        command_id: "88888888-8888-4888-8888-888888888888",
        command_type: "resume_paper",
        environment: "paper",
        state: "requested",
        requested_by: requester,
        requested_at: "2099-07-13T23:58:00.000Z",
        expires_at: "2099-07-14T00:20:00.000Z",
        qualification_id: "33333333-3333-4333-8333-333333333333",
        strategy_version_id: "44444444-4444-4444-8444-444444444444",
        risk_policy_version_id: "55555555-5555-4555-8555-555555555555",
        release_sha: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ledger_checkpoint: "ledger-checkpoint-2026-07-14",
        command_hash: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        control_plane_receipt: {
          schema_version: 1,
          receipt_id: "99999999-9999-4999-8999-999999999999",
          command_id: "88888888-8888-4888-8888-888888888888",
          state: "requested",
          revision: 1,
          persisted_at: "2099-07-13T23:58:00.000Z",
          approved_at: null,
          approved_by: null
        },
        worker_ack: null
      }
    ],
    reviews: [],
    incidents: [
      {
        schema_version: 1,
        incident_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        severity: "sev2",
        status: "open",
        kind: "command_timeout",
        title: "Worker ACK 지연",
        summary: "승인된 명령의 claim ACK가 정책 시간 안에 관찰되지 않았습니다.",
        detected_at: "2099-07-13T23:59:00.000Z",
        ack_due_at: null,
        escalation_status: "not_required",
        acknowledged_at: null,
        resolved_at: null,
        owner: null,
        evidence_refs: ["command:66666666"]
      }
    ],
    orders: [
      {
        schema_version: 1,
        order_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        command_id: null,
        environment: "paper",
        symbol: "005930",
        side: "buy",
        order_type: "limit",
        requested_quantity: 2,
        filled_quantity: 0,
        requested_price_krw: 70000,
        average_fill_price_krw: null,
        status: "reconciliation_required",
        strategy_version_id: "44444444-4444-4444-8444-444444444444",
        risk_policy_version_id: "55555555-5555-4555-8555-555555555555",
        idempotency_key: "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
        created_at: "2099-07-13T23:40:00.000Z",
        updated_at: "2099-07-13T23:55:00.000Z"
      }
    ],
    positions: [
      {
        schema_version: 1,
        position_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        environment: "paper",
        symbol: "005930",
        quantity: 10,
        average_price_krw: 69000,
        market_price_krw: null,
        market_value_krw: null,
        unrealized_pnl_krw: null,
        market_data_status: "unavailable",
        market_data_source: null,
        market_data_as_of: null,
        as_of: "2099-07-14T00:00:00.000Z"
      }
    ],
    audit_events: [
      {
        schema_version: 1,
        audit_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
        occurred_at: "2099-07-13T23:51:00.000Z",
        actor,
        action: "command_reviewed",
        resource_type: "command",
        resource_id: "66666666-6666-4666-8666-666666666666",
        outcome: "success",
        reason_code: "policy_satisfied",
        correlation_id: "ffffffff-ffff-4fff-8fff-ffffffffffff"
      }
    ],
    reconciliation_cases: [
      {
        schema_version: 1,
        case_id: "12121212-1212-4212-8212-121212121212",
        order_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        environment: "paper",
        status: "open",
        reason_code: "ack_timeout",
        opened_at: "2099-07-13T23:56:00.000Z",
        updated_at: "2099-07-13T23:59:00.000Z",
        owner: null,
        evidence_refs: ["worker-heartbeat:latest", "order:bbbbbbbb"],
        resolution_code: null
      }
    ]
  };
}
