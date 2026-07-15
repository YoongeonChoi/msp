import assert from "node:assert/strict";

import {
  DataContractError,
  UnknownResolutionContextV2Schema,
  UnknownResolutionReceiptV2Schema,
  UnknownResolutionRequestDraftV2Schema,
  UnknownResolutionRequestV2Schema,
  UnknownResolutionReviewDraftV2Schema,
  UnknownResolutionReviewV2Schema,
  UnknownResolutionSnapshotV2Schema,
  UnknownResolutionStepUpReceiptV2Schema,
  UnknownResolutionStepUpRequestV2Schema,
  parseDataContract
} from "../src/lib/operationsContracts";
import {
  makeRequestedUnknownResolutionSnapshot,
  makeUnknownResolutionSnapshot
} from "./unknownResolutionFixture";

const snapshot = makeUnknownResolutionSnapshot();
const context = snapshot.cases[0];
const requestDraft = {
  schema_version: 2 as const,
  request_id: "45454545-4545-4545-8545-454545454545",
  environment: "paper" as const,
  idempotency_key: "46464646-4646-4646-8646-464646464646",
  command_type: "close_unknown_execution" as const,
  break_id: context.break_id,
  intent_id: context.intent_id,
  unknown_observation_id: context.unknown_observation.observation_id,
  provider_order_id: context.provider_identity.provider_order_id,
  evidence_artifact_uri: `urn:sha256:${"d".repeat(64)}`,
  evidence_sha256: "d".repeat(64),
  evidence_captured_at: "2099-07-13T23:59:00.000Z",
  reason_code: "accounting_closure_requested" as const,
  expected_break_state: "open" as const,
  expected_break_revision: context.break_revision,
  expected_reconciliation_state: "manual" as const,
  expected_cash_projection_version: context.cash_projection_version,
  expected_position_projection_version: context.position_projection_version,
  expected_reservation_event_sequence: context.reservation_event_sequence,
  expected_control_epoch: context.control_epoch,
  terminal_status: "canceled" as const,
  missing_fills: [],
  requested_at: "2099-07-14T00:00:00.000Z",
  expires_at: "2099-07-14T00:30:00.000Z"
};
const requestGrant = {
  schema_version: 2 as const,
  step_up_grant_id: "47474747-4747-4747-8747-474747474747",
  command_hash: "f".repeat(64),
  step_up_grant_issued_at: "2099-07-14T00:00:00.000Z",
  step_up_grant_expires_at: "2099-07-14T00:05:00.000Z",
  step_up_grant_one_time: true as const,
  step_up_grant_consumed_at: null,
  bound_action: "request" as const,
  bound_command_type: "close_unknown_execution" as const
};
const requested = makeRequestedUnknownResolutionSnapshot().cases[0];
assert.ok(requested.request);
const reviewDraft = {
  schema_version: 2 as const,
  review_id: "48484848-4848-4848-8848-484848484848",
  command_id: requested.request.request_id,
  command_type: "close_unknown_execution" as const,
  reviewer_role: "risk_approver" as const,
  decision: "approve" as const,
  reason_code: "evidence_sufficient" as const,
  expected_receipt_revision: requested.request.receipt_revision,
  expected_break_revision: requested.break_revision,
  request_digest_sha256: requested.request.request_digest_sha256,
  evidence_sha256: requested.request.evidence_sha256,
  reviewed_at: "2099-07-14T00:00:00.000Z"
};
const reviewGrant = {
  ...requestGrant,
  step_up_grant_id: "49494949-4949-4949-8949-494949494949",
  command_hash: "1".repeat(64),
  bound_action: "review" as const
};
const receipt = {
  schema_version: 2 as const,
  command_id: requestDraft.request_id,
  break_id: requestDraft.break_id,
  intent_id: requestDraft.intent_id,
  state: "requested" as const,
  receipt_revision: 0,
  break_revision: 4,
  request_digest_sha256: requestGrant.command_hash,
  review_digest_sha256: null,
  terminal_status: requestDraft.terminal_status,
  claim_token: null,
  work_revision: null,
  application_id: null,
  application_sha256: null,
  accounting_mutation_allowed: false,
  resolution_complete: false,
  inserted: true
};

for (const [label, schema, sample] of [
  ["UnknownResolutionContextV2", UnknownResolutionContextV2Schema, context],
  ["UnknownResolutionSnapshotV2", UnknownResolutionSnapshotV2Schema, snapshot],
  ["UnknownResolutionRequestDraftV2", UnknownResolutionRequestDraftV2Schema, requestDraft],
  ["UnknownResolutionRequestV2", UnknownResolutionRequestV2Schema, { ...requestDraft, ...requestGrant }],
  ["UnknownResolutionReviewDraftV2", UnknownResolutionReviewDraftV2Schema, reviewDraft],
  ["UnknownResolutionReviewV2", UnknownResolutionReviewV2Schema, { ...reviewDraft, ...reviewGrant }],
  ["UnknownResolutionStepUpReceiptV2", UnknownResolutionStepUpReceiptV2Schema, requestGrant],
  ["UnknownResolutionReceiptV2", UnknownResolutionReceiptV2Schema, receipt]
] as const) {
  assert.equal(schema.safeParse(sample).success, true, `${label} valid fixture must pass`);
  const missingVersion = structuredClone(sample) as Record<string, unknown>;
  delete missingVersion.schema_version;
  assert.equal(schema.safeParse(missingVersion).success, false, `${label} requires schema_version=2`);
  assert.equal(
    schema.safeParse({ ...(structuredClone(sample) as object), unexpected_key: true }).success,
    false,
    `${label} rejects unknown fields`
  );
}

assert.equal(
  UnknownResolutionStepUpRequestV2Schema.safeParse({
    schema_version: 2,
    bound_action: "request",
    bound_command_type: "close_unknown_execution",
    command_payload: requestDraft
  }).success,
  true
);
assert.equal(
  UnknownResolutionRequestDraftV2Schema.safeParse({ ...requestDraft, request_id: "not-a-uuid" }).success,
  false
);
assert.equal(
  UnknownResolutionRequestDraftV2Schema.safeParse({ ...requestDraft, evidence_sha256: "bad" }).success,
  false
);
assert.equal(
  UnknownResolutionRequestDraftV2Schema.safeParse({
    ...requestDraft,
    evidence_artifact_uri: `urn:sha256:${"3".repeat(64)}`
  }).success,
  false,
  "evidence URN must bind the evidence SHA"
);
assert.equal(
  UnknownResolutionRequestDraftV2Schema.safeParse({ ...requestDraft, requested_at: "not-a-time" }).success,
  false
);
assert.equal(
  UnknownResolutionRequestDraftV2Schema.safeParse({
    ...requestDraft,
    missing_fills: [{
      schema_version: 2,
      fill_sequence: 2,
      provider_order_id: requestDraft.provider_order_id,
      provider_execution_id: "paper-fill-1",
      quantity: 1,
      price_krw: 70000,
      commission_krw: 10,
      tax_krw: 0,
      filled_at: "2099-07-13T23:59:30.000Z",
      settlement_date: "2099-07-16",
      evidence_sha256: "2".repeat(64)
    }]
  }).success,
  false,
  "missing-fill sequence cannot silently skip sequence one"
);
assert.equal(
  UnknownResolutionRequestDraftV2Schema.safeParse({
    ...requestDraft,
    missing_fills: [{
      schema_version: 2,
      fill_sequence: 1,
      provider_order_id: requestDraft.provider_order_id,
      provider_execution_id: "paper-fill-1",
      quantity: 1,
      price_krw: 70000,
      commission_krw: 10,
      tax_krw: 0,
      filled_at: "2099-07-13T23:59:30.000Z",
      settlement_date: "2099-02-30",
      evidence_sha256: "2".repeat(64)
    }]
  }).success,
  false,
  "invalid calendar dates must fail before mutation"
);
assert.equal(
  UnknownResolutionReviewDraftV2Schema.safeParse({
    ...reviewDraft,
    decision: "reject",
    reason_code: "evidence_sufficient"
  }).success,
  false,
  "review decision and reason must agree"
);
assert.equal(
  UnknownResolutionReceiptV2Schema.safeParse({ ...receipt, resolution_complete: true }).success,
  false,
  "request receipt cannot be shown as complete before Worker application"
);
assert.equal(
  UnknownResolutionContextV2Schema.safeParse({
    ...context,
    provider_identity: { ...context.provider_identity, broker: "local_contract_simulator" }
  }).success,
  false,
  "paper context cannot masquerade as contract-test provider identity"
);

assert.throws(
  () => parseDataContract(
    UnknownResolutionSnapshotV2Schema,
    { ...snapshot, generated_at: "sensitive-invalid-value" },
    "api.get_unknown_resolution_cases_v2"
  ),
  (error: unknown) => {
    assert.ok(error instanceof DataContractError);
    assert.doesNotMatch(error.message, /sensitive-invalid-value/);
    return true;
  }
);

console.log("unknown-resolution V2 strict contracts passed");
