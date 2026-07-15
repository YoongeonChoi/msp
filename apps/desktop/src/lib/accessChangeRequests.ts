import {
  accessChangeRequestDraftSchema,
  accessChangeRequestSchema,
  accessChangeReviewDraftSchema,
  accessChangeReviewSchema,
  accessStepUpGrantDraftRequestSchema,
  accessStepUpGrantIssueResponseSchema,
  parseDataContract
} from "./operationsContracts";
import type {
  AccessChangeReceipt,
  AccessChangeRequest,
  AccessChangeRequestDraft,
  AccessChangeReviewDraft,
  AccessChangeReviewRequest,
  AccessStepUpGrantDraftRequest,
  AccessStepUpGrantIssueResponse,
  OperationsRole,
  OperationsSnapshot
} from "./operationsContracts";
import { secureOperationId } from "./operationRequests";
import type { OperationIdFactory } from "./operationRequests";

export function buildAccessChangeRequestDraft({
  snapshot,
  subjectUserId,
  requestedRole,
  changeType,
  evidenceId,
  now,
  idFactory = secureOperationId
}: {
  readonly snapshot: OperationsSnapshot;
  readonly subjectUserId: string;
  readonly requestedRole: OperationsRole;
  readonly changeType: "grant" | "revoke";
  readonly evidenceId: string;
  readonly now: Date;
  readonly idFactory?: OperationIdFactory;
}): AccessChangeRequestDraft | null {
  const actor = snapshot.access.actor;
  if (
    snapshot.access.assurance_level !== "aal2" ||
    actor === null ||
    !actor.roles.includes("platform_admin") ||
    actor.actor_id === subjectUserId
  ) {
    return null;
  }
  return parseDataContract(
    accessChangeRequestDraftSchema,
    {
      schema_version: 1,
      request_id: idFactory(),
      subject_user_id: subjectUserId,
      requested_role: requestedRole,
      change_type: changeType,
      evidence_id: evidenceId,
      reason_code: changeType === "grant" ? "role_required" : "access_removal",
      requested_at: now.toISOString(),
      expires_at: new Date(now.getTime() + 24 * 60 * 60_000).toISOString()
    },
    "desktop:access_change_request_draft"
  );
}

export function buildAccessChangeReviewDraft({
  snapshot,
  receipt,
  decision,
  now,
  idFactory = secureOperationId
}: {
  readonly snapshot: OperationsSnapshot;
  readonly receipt: AccessChangeReceipt;
  readonly decision: "approve" | "reject";
  readonly now: Date;
  readonly idFactory?: OperationIdFactory;
}): AccessChangeReviewDraft | null {
  const actor = snapshot.access.actor;
  if (
    snapshot.access.assurance_level !== "aal2" ||
    actor === null ||
    !actor.roles.includes("platform_admin") ||
    receipt.state !== "requested" ||
    receipt.requested_by.actor_id === actor.actor_id ||
    receipt.subject_user_id === actor.actor_id ||
    Date.parse(receipt.expires_at) <= now.getTime()
  ) {
    return null;
  }
  return parseDataContract(
    accessChangeReviewDraftSchema,
    {
      schema_version: 1,
      review_id: idFactory(),
      request_id: receipt.request_id,
      decision,
      reason_code: decision === "approve" ? "policy_satisfied" : "evidence_incomplete",
      expected_state: "requested",
      reviewed_at: now.toISOString()
    },
    "desktop:access_change_review_draft"
  );
}

export function buildAccessStepUpGrantDraftRequest(
  action: "request",
  draft: AccessChangeRequestDraft
): AccessStepUpGrantDraftRequest;
export function buildAccessStepUpGrantDraftRequest(
  action: "review",
  draft: AccessChangeReviewDraft
): AccessStepUpGrantDraftRequest;
export function buildAccessStepUpGrantDraftRequest(
  action: "request" | "review",
  draft: AccessChangeRequestDraft | AccessChangeReviewDraft
): AccessStepUpGrantDraftRequest {
  return parseDataContract(
    accessStepUpGrantDraftRequestSchema,
    { schema_version: 1, bound_action: action, access_change_payload: draft },
    "desktop:access_step_up_grant_draft"
  );
}

export function attachAccessStepUpGrantToRequestDraft(
  draft: AccessChangeRequestDraft,
  grant: AccessStepUpGrantIssueResponse
): AccessChangeRequest {
  const binding = parseDataContract(
    accessStepUpGrantIssueResponseSchema,
    grant,
    "api.issue_access_step_up_grant_v1:response"
  );
  return parseDataContract(
    accessChangeRequestSchema,
    { ...draft, ...withoutAccessGrantSchemaVersion(binding) },
    "desktop:access_change_request"
  );
}

export function attachAccessStepUpGrantToReviewDraft(
  draft: AccessChangeReviewDraft,
  grant: AccessStepUpGrantIssueResponse
): AccessChangeReviewRequest {
  const binding = parseDataContract(
    accessStepUpGrantIssueResponseSchema,
    grant,
    "api.issue_access_step_up_grant_v1:response"
  );
  return parseDataContract(
    accessChangeReviewSchema,
    { ...draft, ...withoutAccessGrantSchemaVersion(binding) },
    "desktop:access_change_review"
  );
}

function withoutAccessGrantSchemaVersion(grant: AccessStepUpGrantIssueResponse) {
  return {
    step_up_grant_id: grant.step_up_grant_id,
    change_hash: grant.change_hash,
    step_up_grant_issued_at: grant.step_up_grant_issued_at,
    step_up_grant_expires_at: grant.step_up_grant_expires_at,
    step_up_grant_one_time: grant.step_up_grant_one_time,
    step_up_grant_consumed_at: grant.step_up_grant_consumed_at,
    bound_action: grant.bound_action
  };
}
