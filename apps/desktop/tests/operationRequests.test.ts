import assert from "node:assert/strict";

import {
  attachStepUpGrantToCommandDraft,
  attachStepUpGrantToReviewDraft,
  buildCommandReviewDraft,
  buildEmergencyStopCommandRequest,
  buildIncidentActionRequest,
  buildOperationCommandDraft,
  buildStepUpGrantDraftRequest
} from "../src/lib/operationRequests";
import { operationCommandRequestSchema } from "../src/lib/operationsContracts";
import { makeOperationsSnapshot } from "./operationsFixture";

const ids = [
  "16161616-1616-4616-8616-161616161616",
  "17171717-1717-4717-8717-171717171717",
  "18181818-1818-4818-8818-181818181818",
  "19191919-1919-4919-8919-191919191919"
];
let idIndex = 0;
const idFactory = () => ids[idIndex++] ?? "20202020-2020-4020-8020-202020202020";
const now = new Date("2099-07-14T00:00:00.000Z");
const snapshot = makeOperationsSnapshot();

const stop = buildEmergencyStopCommandRequest({ snapshot, now, idFactory });
assert.ok(stop);
assert.equal(stop.command_type, "emergency_stop");
assert.equal(stop.environment, "paper");
assert.equal(stop.expected_state_version, 17);
assert.equal(stop.assurance_level, "aal2");
assert.equal(stop.actor_role, "operator");
assert.equal(operationCommandRequestSchema.safeParse(stop).success, true);

const resumeDraft = buildOperationCommandDraft({ commandType: "resume_paper", snapshot, now, idFactory });
assert.ok(resumeDraft);
assert.equal(resumeDraft.command_type, "resume_paper");
assert.equal(resumeDraft.qualification_id, snapshot.qualification?.qualification_id);
assert.equal(resumeDraft.reason_code, "qualified_resume");

const pauseDraft = buildOperationCommandDraft({ commandType: "pause_paper", snapshot, now, idFactory });
assert.ok(pauseDraft);
const pauseGrantRequest = buildStepUpGrantDraftRequest("request", pauseDraft);
assert.deepEqual(pauseGrantRequest.command_payload, pauseDraft);
assert.equal("command_hash" in pauseGrantRequest.command_payload, false);
assert.equal("step_up_grant_id" in pauseGrantRequest.command_payload, false);
const pauseGrant = snapshot.access.active_step_up_grants.find(
  (grant) => grant.bound_action === "request" && grant.bound_command_type === "pause_paper"
);
assert.ok(pauseGrant);
const pauseRequest = attachStepUpGrantToCommandDraft(pauseDraft, { schema_version: 1, ...pauseGrant });
assert.equal(pauseRequest.request_id, pauseDraft.request_id, "the same in-memory draft must be submitted after grant issuance");
assert.equal(pauseRequest.command_hash, pauseGrant.command_hash);

const expiredSnapshot = makeOperationsSnapshot();
if (expiredSnapshot.qualification) {
  expiredSnapshot.qualification.valid_until = "2099-07-13T23:59:59.000Z";
}
assert.equal(
  buildOperationCommandDraft({ commandType: "resume_paper", snapshot: expiredSnapshot, now, idFactory }),
  null,
  "expired qualification must not produce a request"
);

const contractTestSnapshot = makeOperationsSnapshot();
contractTestSnapshot.runtime_health.environment = "contract_test";
if (contractTestSnapshot.qualification) {
  contractTestSnapshot.qualification.environment = "contract_test";
}
const contractTest = buildOperationCommandDraft({
  commandType: "start_contract_test",
  snapshot: contractTestSnapshot,
  now,
  idFactory
});
assert.ok(contractTest);
assert.equal(contractTest.environment, "contract_test");
assert.equal(contractTest.reason_code, "boundary_verification");

const reviewDraft = buildCommandReviewDraft({
  command: snapshot.pending_reviews[0],
  access: snapshot.access,
  decision: "approve",
  now,
  idFactory
});
assert.ok(reviewDraft);
const reviewGrantRequest = buildStepUpGrantDraftRequest("review", reviewDraft);
assert.equal("command_hash" in reviewGrantRequest.command_payload, false);
const reviewGrant = snapshot.access.active_step_up_grants.find(
  (grant) => grant.bound_action === "review" && grant.bound_command_type === "resume_paper"
);
assert.ok(reviewGrant);
const reviewDraftGrant = {
  schema_version: 1 as const,
  ...reviewGrant,
  command_hash: "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
};
assert.notEqual(
  reviewDraftGrant.command_hash,
  snapshot.pending_reviews[0].command_hash,
  "a review grant binds the review draft hash, not the original command request hash"
);
const attachedReview = attachStepUpGrantToReviewDraft(reviewDraft, reviewDraftGrant);
assert.equal(attachedReview.review_id, reviewDraft.review_id);
assert.equal(attachedReview.command_hash, reviewDraftGrant.command_hash);
assert.equal(attachedReview.expected_receipt_revision, 1);
assert.equal(attachedReview.reason_code, "policy_satisfied");

const incidentAction = buildIncidentActionRequest({
  incident: snapshot.incidents[0],
  action: "acknowledge",
  now,
  idFactory
});
assert.equal(incidentAction.expected_status, "open");
assert.equal(incidentAction.reason_code, "operator_acknowledged");

console.log("operation request builders passed");
