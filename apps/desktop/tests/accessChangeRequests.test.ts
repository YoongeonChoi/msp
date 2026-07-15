import assert from "node:assert/strict";

import {
  attachAccessStepUpGrantToRequestDraft,
  attachAccessStepUpGrantToReviewDraft,
  buildAccessChangeRequestDraft,
  buildAccessChangeReviewDraft,
  buildAccessStepUpGrantDraftRequest
} from "../src/lib/accessChangeRequests";
import type { AccessChangeReceipt } from "../src/lib/operationsContracts";
import { makeOperationsSnapshot } from "./operationsFixture";

const snapshot = makeOperationsSnapshot();
assert.ok(snapshot.access.actor);
snapshot.access.actor.roles = ["platform_admin"];
snapshot.access.permissions = [];
const ids = [
  "61616161-6161-4161-8161-616161616161",
  "62626262-6262-4262-8262-626262626262",
  "63636363-6363-4363-8363-636363636363"
];
let idIndex = 0;
const idFactory = () => ids[idIndex++] ?? "64646464-6464-4464-8464-646464646464";
const now = new Date("2099-07-14T00:00:00.000Z");

const requestDraft = buildAccessChangeRequestDraft({
  snapshot,
  subjectUserId: "71717171-7171-4171-8171-717171717171",
  requestedRole: "auditor",
  changeType: "grant",
  evidenceId: "72727272-7272-4272-8272-727272727272",
  now,
  idFactory
});
assert.ok(requestDraft);
const grantDraft = buildAccessStepUpGrantDraftRequest("request", requestDraft);
assert.deepEqual(grantDraft.access_change_payload, requestDraft);
const requestGrant = {
  schema_version: 1 as const,
  step_up_grant_id: "73737373-7373-4373-8373-737373737373",
  change_hash: "a".repeat(64),
  step_up_grant_issued_at: "2099-07-14T00:00:01.000Z",
  step_up_grant_expires_at: "2099-07-14T00:05:01.000Z",
  step_up_grant_one_time: true as const,
  step_up_grant_consumed_at: null,
  bound_action: "request" as const
};
const request = attachAccessStepUpGrantToRequestDraft(requestDraft, requestGrant);
assert.equal(request.request_id, requestDraft.request_id, "the exact request draft must survive grant issuance");
assert.equal(request.expires_at, requestDraft.expires_at, "24-hour request expiry must not collide with grant expiry");
assert.equal(request.step_up_grant_expires_at, requestGrant.step_up_grant_expires_at);

assert.equal(
  buildAccessChangeRequestDraft({
    snapshot,
    subjectUserId: snapshot.access.actor.actor_id,
    requestedRole: "viewer",
    changeType: "grant",
    evidenceId: "72727272-7272-4272-8272-727272727272",
    now,
    idFactory
  }),
  null,
  "platform admins cannot request their own role changes"
);

const receipt: AccessChangeReceipt = {
  schema_version: 1,
  request_id: request.request_id,
  subject_user_id: request.subject_user_id,
  requested_role: request.requested_role,
  change_type: request.change_type,
  state: "requested",
  requested_by: {
    actor_id: "74747474-7474-4474-8474-747474747474",
    display_name: "권한 요청자",
    roles: ["platform_admin"]
  },
  reviewed_by: null,
  requested_at: request.requested_at,
  reviewed_at: null,
  applied_at: null,
  expires_at: request.expires_at,
  reason_code: "role_required"
};
const reviewDraft = buildAccessChangeReviewDraft({ snapshot, receipt, decision: "approve", now, idFactory });
assert.ok(reviewDraft);
const reviewGrantDraft = buildAccessStepUpGrantDraftRequest("review", reviewDraft);
assert.deepEqual(reviewGrantDraft.access_change_payload, reviewDraft);
const reviewGrant = { ...requestGrant, step_up_grant_id: "75757575-7575-4575-8575-757575757575", change_hash: "b".repeat(64), bound_action: "review" as const };
const review = attachAccessStepUpGrantToReviewDraft(reviewDraft, reviewGrant);
assert.equal(review.review_id, reviewDraft.review_id);
assert.equal(review.change_hash, reviewGrant.change_hash);

receipt.requested_by.actor_id = snapshot.access.actor.actor_id;
assert.equal(
  buildAccessChangeReviewDraft({ snapshot, receipt, decision: "approve", now, idFactory }),
  null,
  "the access-change maker cannot act as checker"
);

console.log("access change immutable draft and maker-checker builders passed");
