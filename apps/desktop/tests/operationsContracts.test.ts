import assert from "node:assert/strict";

import {
  DataContractError,
  AccessProfileV1Schema,
  AuditSummaryV1Schema,
  CommandReceiptV1Schema,
  CommandRequestV1Schema,
  CommandReviewV1Schema,
  ConnectionHealthV1Schema,
  IncidentSummaryV1Schema,
  OrderSummaryV1Schema,
  PositionSummaryV1Schema,
  QualificationSnapshotV1Schema,
  RuntimeStatusV1Schema,
  commandDraftForHash,
  operationCommandRequestSchema,
  operationsRoleSchema,
  operationsSnapshotSchema,
  parseDataContract,
  reviewDraftForHash,
  stepUpGrantDraftRequestSchema,
  stepUpGrantIssueResponseSchema
} from "../src/lib/operationsContracts";
import { makeOperationsSnapshot } from "./operationsFixture";

const valid = makeOperationsSnapshot();
assert.equal(operationsSnapshotSchema.parse(valid).schema_version, 1);
for (const role of [
  "platform_admin",
  "operator",
  "risk_approver",
  "strategy_reviewer",
  "auditor",
  "release_manager",
  "viewer"
]) {
  assert.equal(operationsRoleSchema.safeParse(role).success, true);
}
assert.equal(operationsRoleSchema.safeParse("ops_viewer").success, false);
assert.equal(operationsRoleSchema.safeParse("security_admin").success, false);
for (const schema of [
  CommandRequestV1Schema,
  CommandReviewV1Schema,
  CommandReceiptV1Schema,
  RuntimeStatusV1Schema,
  ConnectionHealthV1Schema,
  OrderSummaryV1Schema,
  PositionSummaryV1Schema,
  IncidentSummaryV1Schema,
  AuditSummaryV1Schema,
  AccessProfileV1Schema,
  QualificationSnapshotV1Schema
]) {
  assert.ok(schema, "required public V1 schema export must exist");
}

const requestGrant = valid.access.active_step_up_grants.find(
  (grant) => grant.bound_action === "request" && grant.bound_command_type === "pause_paper"
);
const reviewGrant = valid.access.active_step_up_grants.find(
  (grant) => grant.bound_action === "review" && grant.bound_command_type === "resume_paper"
);
assert.ok(requestGrant && reviewGrant);
const commandRequestSample = {
  schema_version: 1,
  request_id: "29292929-2929-4929-8929-292929292929",
  environment: "paper",
  idempotency_key: "30303030-3030-4030-8030-303030303030",
  expected_state_version: 17,
  requested_at: "2099-07-14T00:00:00.000Z",
  expires_at: "2099-07-14T00:05:00.000Z",
  ...requestGrant,
  command_type: "pause_paper",
  reason_code: "operator_pause"
};
const commandReviewSample = {
  schema_version: 1,
  review_id: "31313131-3131-4131-8131-313131313131",
  command_id: valid.pending_reviews[0].command_id,
  command_type: "resume_paper",
  ...reviewGrant,
  reviewer_role: "risk_approver",
  decision: "approve",
  reason_code: "policy_satisfied",
  expected_receipt_revision: 1,
  reviewed_at: "2099-07-14T00:00:00.000Z"
};

const parsedCommandRequest = CommandRequestV1Schema.parse(commandRequestSample);
const commandHashDraft = commandDraftForHash(parsedCommandRequest);
assert.equal("command_hash" in commandHashDraft, false);
assert.equal("step_up_grant_id" in commandHashDraft, false);
assert.equal("step_up_grant_issued_at" in commandHashDraft, false);
const parsedCommandReview = CommandReviewV1Schema.parse(commandReviewSample);
const reviewHashDraft = reviewDraftForHash(parsedCommandReview);
assert.equal("command_hash" in reviewHashDraft, false);
assert.equal("step_up_grant_id" in reviewHashDraft, false);
assert.equal(
  stepUpGrantDraftRequestSchema.safeParse({
    schema_version: 1,
    bound_action: "request",
    bound_command_type: "pause_paper",
    command_payload: commandHashDraft
  }).success,
  true
);
assert.equal(
  stepUpGrantIssueResponseSchema.safeParse({ schema_version: 1, ...requestGrant }).success,
  true
);

for (const [label, schema, sample] of [
  ["CommandRequestV1", CommandRequestV1Schema, commandRequestSample],
  ["CommandReviewV1", CommandReviewV1Schema, commandReviewSample],
  ["CommandReceiptV1", CommandReceiptV1Schema, valid.commands[0]],
  ["RuntimeStatusV1", RuntimeStatusV1Schema, valid.runtime_health],
  ["ConnectionHealthV1", ConnectionHealthV1Schema, valid.runtime_health.components[0]],
  ["OrderSummaryV1", OrderSummaryV1Schema, valid.orders[0]],
  ["PositionSummaryV1", PositionSummaryV1Schema, valid.positions[0]],
  ["IncidentSummaryV1", IncidentSummaryV1Schema, valid.incidents[0]],
  ["AuditSummaryV1", AuditSummaryV1Schema, valid.audit_events[0]],
  ["AccessProfileV1", AccessProfileV1Schema, valid.access],
  ["QualificationSnapshotV1", QualificationSnapshotV1Schema, valid.qualification]
] as const) {
  assertVersionAndStrictness(label, schema, sample);
}

assert.equal(CommandRequestV1Schema.safeParse({ ...commandRequestSample, request_id: "not-a-uuid" }).success, false);
assert.equal(CommandRequestV1Schema.safeParse({ ...commandRequestSample, requested_at: "not-a-time" }).success, false);
assert.equal(CommandRequestV1Schema.safeParse({ ...commandRequestSample, command_hash: "bad-sha" }).success, false);
assert.equal(RuntimeStatusV1Schema.safeParse({ ...valid.runtime_health, worker_release_sha: "bad-sha" }).success, false);
assert.equal(OrderSummaryV1Schema.safeParse({ ...valid.orders[0], order_id: "not-a-uuid" }).success, false);
assert.equal(
  OrderSummaryV1Schema.safeParse({
    ...valid.orders[0],
    idempotency_key: "30303030-3030-4030-8030-303030303030"
  }).success,
  false,
  "order semantic idempotency keys must be SHA-256, not intent UUIDs"
);
assert.equal(QualificationSnapshotV1Schema.safeParse({ ...valid.qualification, release_sha: "bad-sha" }).success, false);

assert.equal(
  PositionSummaryV1Schema.safeParse({ ...valid.positions[0], market_price_krw: 0 }).success,
  false,
  "unavailable market data must not be fabricated from average cost"
);

const missingQualifiedPin = structuredClone(valid.pending_reviews[0]);
missingQualifiedPin.strategy_version_id = null;
assert.equal(
  CommandReceiptV1Schema.safeParse(missingQualifiedPin).success,
  false,
  "qualified command receipts must retain every requested target pin"
);

const missingVersion = structuredClone(valid) as Record<string, unknown>;
delete missingVersion.schema_version;
assert.equal(operationsSnapshotSchema.safeParse(missingVersion).success, false, "schema_version must never default");

const unknownTopLevel = { ...valid, unexpected_control: true };
assert.equal(operationsSnapshotSchema.safeParse(unknownTopLevel).success, false, "unknown keys must fail closed");

const liveFlag = structuredClone(valid) as typeof valid & { runtime_health: { live_permitted: boolean } };
liveFlag.runtime_health.live_permitted = true;
assert.equal(operationsSnapshotSchema.safeParse(liveFlag).success, false, "LIVE must be contractually impossible");

const unofficialSandbox = structuredClone(valid) as unknown as { runtime_health: { environment: string } };
unofficialSandbox.runtime_health.environment = "broker_sandbox";
assert.equal(
  operationsSnapshotSchema.safeParse(unofficialSandbox).success,
  false,
  "an unverified broker sandbox label must not enter the strict model"
);

const marketOrder = structuredClone(valid) as unknown as { orders: Array<{ order_type: string }> };
marketOrder.orders[0].order_type = "market";
assert.equal(operationsSnapshotSchema.safeParse(marketOrder).success, false, "strict operation orders are limit-only");

const limitOrderWithoutPrice = structuredClone(valid) as unknown as {
  orders: Array<{ requested_price_krw: number | null }>;
};
limitOrderWithoutPrice.orders[0].requested_price_krw = null;
assert.equal(
  operationsSnapshotSchema.safeParse(limitOrderWithoutPrice).success,
  false,
  "strict limit orders require a positive requested price"
);

const mismatchedAck = structuredClone(valid);
mismatchedAck.commands[0].control_plane_receipt.command_id = "13131313-1313-4313-8313-131313131313";
assert.equal(operationsSnapshotSchema.safeParse(mismatchedAck).success, false, "receipt identity must match command identity");

const mismatchedLifecycle = structuredClone(valid);
mismatchedLifecycle.commands[0].control_plane_receipt.state = "requested";
assert.equal(
  operationsSnapshotSchema.safeParse(mismatchedLifecycle).success,
  false,
  "control-plane and worker lifecycle states must remain distinct and consistent"
);

const selfApproved = structuredClone(valid);
selfApproved.commands[0].control_plane_receipt.approved_by = selfApproved.commands[0].requested_by;
assert.equal(
  operationsSnapshotSchema.safeParse(selfApproved).success,
  false,
  "non-emergency commands require maker-checker separation"
);

const longStepUp = structuredClone(valid);
longStepUp.access.active_step_up_grants[0].step_up_grant_expires_at = "2099-07-14T00:10:00.000Z";
assert.equal(operationsSnapshotSchema.safeParse(longStepUp).success, false, "step-up grants must expire within five minutes");

const platformAdminApproval = structuredClone(valid);
platformAdminApproval.commands[0].control_plane_receipt.approved_by?.roles.push("platform_admin");
assert.equal(
  operationsSnapshotSchema.safeParse(platformAdminApproval).success,
  false,
  "platform_admin must not appear as a trading approver"
);

assert.equal(
  operationCommandRequestSchema.safeParse({
    schema_version: 1,
    request_id: "14141414-1414-4414-8414-141414141414",
    command_type: "enable_live",
    environment: "live",
    idempotency_key: "15151515-1515-4515-8515-151515151515",
    expected_state_version: 1,
    requested_at: "2099-07-14T00:00:00.000Z",
    expires_at: "2099-07-14T00:05:00.000Z"
  }).success,
  false,
  "LIVE command shapes must be rejected"
);

const secretPayload = { ...valid, schema_version: "secret-value-that-must-not-leak" };
assert.throws(
  () => parseDataContract(operationsSnapshotSchema, secretPayload, "operations_snapshot_v1"),
  (error: unknown) => {
    assert.ok(error instanceof DataContractError);
    assert.match(error.message, /operations_snapshot_v1/);
    assert.doesNotMatch(error.message, /secret-value-that-must-not-leak/);
    assert.ok(error.issues.every((issue) => !issue.path.includes("secret-value")));
    return true;
  }
);

console.log("operations strict contract fixtures passed");

function assertVersionAndStrictness(
  label: string,
  schema: { readonly safeParse: (value: unknown) => { readonly success: boolean } },
  sample: unknown
): void {
  assert.equal(schema.safeParse(sample).success, true, `${label} valid sample must pass`);
  const missingVersion = structuredClone(sample) as Record<string, unknown>;
  delete missingVersion.schema_version;
  assert.equal(schema.safeParse(missingVersion).success, false, `${label} must require schema_version`);
  const unknownKey = { ...(structuredClone(sample) as Record<string, unknown>), unexpected_key: true };
  assert.equal(schema.safeParse(unknownKey).success, false, `${label} must reject unknown keys`);
}
