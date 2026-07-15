import assert from "node:assert/strict";

import { makeOperationsSnapshot } from "./operationsFixture";
import {
  makeRequestedUnknownResolutionSnapshot,
  makeUnknownResolutionSnapshot
} from "./unknownResolutionFixture";
import {
  attachUnknownResolutionRequestGrant,
  attachUnknownResolutionReviewGrant,
  buildUnknownResolutionRequestDraft,
  buildUnknownResolutionReviewDraft,
  buildUnknownResolutionStepUpRequest,
  canRequestUnknownResolution,
  canReviewUnknownResolution
} from "../src/lib/unknownResolutionRequests";

const access = makeOperationsSnapshot().access;
const context = makeUnknownResolutionSnapshot().cases[0];
const now = new Date("2099-07-14T00:00:00.000Z");
const ids = [
  "50505050-5050-4050-8050-505050505050",
  "51515151-5151-4151-8151-515151515151",
  "52525252-5252-4252-8252-525252525252"
];
let index = 0;
const idFactory = () => ids[index++] ?? "53535353-5353-4353-8353-535353535353";
const evidence = {
  evidenceArtifactUri: `urn:sha256:${"d".repeat(64)}`,
  evidenceSha256: "d".repeat(64),
  evidenceCapturedAt: "2099-07-13T23:59:00.000Z",
  terminalStatus: "canceled" as const,
  missingFills: []
};

assert.equal(canRequestUnknownResolution(access, context), true);
const requestDraft = buildUnknownResolutionRequestDraft({
  context,
  access,
  evidence,
  now,
  idFactory
});
assert.ok(requestDraft);
assert.equal(requestDraft.schema_version, 2);
assert.deepEqual(requestDraft.missing_fills, [], "no-fill closure must preserve the explicit empty manifest");
assert.equal(requestDraft.expected_break_revision, context.break_revision);
assert.equal(requestDraft.expected_cash_projection_version, context.cash_projection_version);
assert.equal(requestDraft.expected_reservation_event_sequence, context.reservation_event_sequence);
assert.equal(requestDraft.expected_control_epoch, context.control_epoch);

const stepUpRequest = buildUnknownResolutionStepUpRequest("request", requestDraft);
assert.equal(stepUpRequest.bound_action, "request");
assert.equal("command_hash" in stepUpRequest.command_payload, false);
const requestGrant = {
  schema_version: 2 as const,
  step_up_grant_id: "54545454-5454-4454-8454-545454545454",
  command_hash: "e".repeat(64),
  step_up_grant_issued_at: "2099-07-14T00:00:00.000Z",
  step_up_grant_expires_at: "2099-07-14T00:05:00.000Z",
  step_up_grant_one_time: true as const,
  step_up_grant_consumed_at: null,
  bound_action: "request" as const,
  bound_command_type: "close_unknown_execution" as const
};
const request = attachUnknownResolutionRequestGrant(requestDraft, requestGrant);
assert.equal(request.command_hash, requestGrant.command_hash);
assert.equal(request.bound_action, "request");

const aal1 = structuredClone(access);
aal1.assurance_level = "aal1";
aal1.active_step_up_grants = [];
assert.equal(canRequestUnknownResolution(aal1, context), false);
assert.equal(buildUnknownResolutionRequestDraft({ context, access: aal1, evidence, now, idFactory }), null);

const platformAdmin = structuredClone(access);
platformAdmin.actor?.roles.push("platform_admin");
assert.equal(canRequestUnknownResolution(platformAdmin, context), false);

const staleBreak = structuredClone(context);
staleBreak.break_state = "resolution_requested";
assert.equal(canRequestUnknownResolution(access, staleBreak), false);

const sellWithoutProjection = structuredClone(context);
sellWithoutProjection.side = "sell";
sellWithoutProjection.position_projection_version = null;
assert.equal(canRequestUnknownResolution(access, sellWithoutProjection), false);

const requested = makeRequestedUnknownResolutionSnapshot().cases[0];
assert.equal(canReviewUnknownResolution(access, requested, now), true);
const reviewDraft = buildUnknownResolutionReviewDraft({
  context: requested,
  access,
  decision: "approve",
  now,
  idFactory
});
assert.ok(reviewDraft);
assert.equal(reviewDraft.expected_receipt_revision, requested.request?.receipt_revision);
assert.equal(reviewDraft.expected_break_revision, requested.break_revision);
assert.equal(reviewDraft.request_digest_sha256, requested.request?.request_digest_sha256);
const reviewStepUp = buildUnknownResolutionStepUpRequest("review", reviewDraft);
assert.equal(reviewStepUp.bound_action, "review");
const reviewGrant = {
  ...requestGrant,
  step_up_grant_id: "55555555-5555-4555-8555-555555555555",
  command_hash: "f".repeat(64),
  bound_action: "review" as const
};
const review = attachUnknownResolutionReviewGrant(reviewDraft, reviewGrant);
assert.equal(review.command_hash, reviewGrant.command_hash);
assert.equal(review.bound_action, "review");

const selfReviewAccess = structuredClone(access);
assert.ok(selfReviewAccess.actor && requested.request);
selfReviewAccess.actor.actor_id = requested.request.requested_by.actor_id;
assert.equal(canReviewUnknownResolution(selfReviewAccess, requested, now), false);
assert.equal(
  buildUnknownResolutionReviewDraft({
    context: requested,
    access: selfReviewAccess,
    decision: "approve",
    now,
    idFactory
  }),
  null
);

const expiredSession = structuredClone(access);
expiredSession.session_state = "expired";
assert.equal(canReviewUnknownResolution(expiredSession, requested, now), false);
assert.equal(
  canReviewUnknownResolution(access, requested, new Date("2099-07-14T00:29:00.000Z")),
  false,
  "expired request must reject stale review CAS"
);

assert.throws(
  () => attachUnknownResolutionReviewGrant(reviewDraft, requestGrant),
  /unknown_resolution_review_step_up_action_mismatch/
);

console.log("unknown-resolution V2 request/review builders passed");
