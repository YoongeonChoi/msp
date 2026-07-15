import type {
  UnknownResolutionContextV2,
  UnknownResolutionSnapshotV2
} from "../src/lib/operationsContracts";

export function makeUnknownResolutionSnapshot(): UnknownResolutionSnapshotV2 {
  return {
    schema_version: 2,
    generated_at: "2099-07-14T00:00:00.000Z",
    cases: [makeUnknownResolutionContext()]
  };
}

export function makeUnknownResolutionContext(): UnknownResolutionContextV2 {
  return {
    schema_version: 2,
    break_id: "41414141-4141-4141-8141-414141414141",
    intent_id: "42424242-4242-4242-8242-424242424242",
    environment: "paper",
    symbol: "005930",
    side: "buy",
    requested_quantity: 2,
    limit_price_krw: 70000,
    break_state: "open",
    break_revision: 3,
    break_reason_code: "unknown_requires_manual_check",
    detected_at: "2099-07-13T23:55:00.000Z",
    resolved_at: null,
    reconciliation_state: "manual",
    reconciliation_updated_at: "2099-07-13T23:55:00.000Z",
    cash_projection_version: 11,
    position_projection_version: null,
    reservation_event_sequence: 2,
    control_epoch: 17,
    provider_identity: {
      schema_version: 2,
      broker: "internal_paper",
      provider_order_id: "paper-order-42424242",
      provider_execution_id: null,
      provider_binding_sha256: "a".repeat(64),
      provider_contract_version: null,
      provider_openapi_sha256: null
    },
    unknown_observation: {
      schema_version: 2,
      observation_id: "43434343-4343-4343-8343-434343434343",
      sequence: 2,
      event_type: "unknown_requires_manual_check",
      observed_at: "2099-07-13T23:55:00.000Z",
      cumulative_quantity: 0,
      cumulative_gross_krw: 0,
      cumulative_commission_krw: 0,
      cumulative_tax_krw: 0,
      reason_code: "provider_response_ambiguous",
      observation_sha256: "b".repeat(64),
      provider_observation_sha256: "c".repeat(64)
    },
    request: null,
    review: null,
    work_receipt: null,
    application_receipt: null,
    postcondition: {
      schema_version: 2,
      resolution_complete: false,
      accounting_application_recorded: false
    }
  };
}

export function makeRequestedUnknownResolutionSnapshot(): UnknownResolutionSnapshotV2 {
  const snapshot = makeUnknownResolutionSnapshot();
  const context = snapshot.cases[0];
  context.break_state = "resolution_requested";
  context.break_revision = 4;
  context.request = {
    schema_version: 2,
    request_id: "44444444-4444-4444-8444-444444444444",
    state: "requested",
    receipt_revision: 0,
    requested_by: {
      actor_id: "22222222-2222-4222-8222-222222222222",
      display_name: "운영 요청자",
      roles: ["operator"]
    },
    requested_at: "2099-07-13T23:58:00.000Z",
    expires_at: "2099-07-14T00:28:00.000Z",
    terminal_status: "canceled",
    evidence_artifact_uri: `urn:sha256:${"d".repeat(64)}`,
    evidence_sha256: "d".repeat(64),
    evidence_captured_at: "2099-07-13T23:57:00.000Z",
    request_digest_sha256: "e".repeat(64),
    expected_break_revision: 3,
    expected_cash_projection_version: 11,
    expected_position_projection_version: null,
    expected_reservation_event_sequence: 2,
    expected_control_epoch: 17,
    missing_fills: []
  };
  return snapshot;
}
