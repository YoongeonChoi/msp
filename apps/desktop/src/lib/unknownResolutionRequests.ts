import {
  parseDataContract,
  unknownResolutionRequestDraftV2Schema,
  unknownResolutionRequestV2Schema,
  unknownResolutionReviewDraftV2Schema,
  unknownResolutionReviewV2Schema,
  unknownResolutionStepUpGrantDraftRequestV2Schema,
  unknownResolutionStepUpGrantV2Schema
} from "./operationsContracts";
import type {
  AccessContext,
  UnknownResolutionContextV2,
  UnknownResolutionMissingFillV2,
  UnknownResolutionRequestDraftV2,
  UnknownResolutionRequestV2,
  UnknownResolutionReviewDraftV2,
  UnknownResolutionReviewV2,
  UnknownResolutionStepUpRequestV2,
  UnknownResolutionStepUpReceiptV2
} from "./operationsContracts";
import type { OperationIdFactory } from "./operationRequests";

export interface UnknownResolutionEvidenceInput {
  readonly evidenceArtifactUri: string;
  readonly evidenceSha256: string;
  readonly evidenceCapturedAt: string;
  readonly terminalStatus: "filled" | "canceled" | "expired" | "rejected";
  readonly missingFills: readonly UnknownResolutionMissingFillV2[];
}

export function canRequestUnknownResolution(
  access: AccessContext,
  context: UnknownResolutionContextV2
): boolean {
  const roles = new Set(access.actor?.roles ?? []);
  const previousRequestIsTerminal = context.request === null ||
    ["rejected", "failed", "expired", "canceled"].includes(context.request.state);
  return access.signed_in &&
    access.session_state === "active" &&
    access.assurance_level === "aal2" &&
    access.actor !== null &&
    roles.has("operator") &&
    !roles.has("platform_admin") &&
    context.break_state === "open" &&
    context.reconciliation_state === "manual" &&
    !context.postcondition.resolution_complete &&
    previousRequestIsTerminal &&
    (context.side === "buy" || context.position_projection_version !== null);
}

export function canReviewUnknownResolution(
  access: AccessContext,
  context: UnknownResolutionContextV2,
  now: Date
): boolean {
  const roles = new Set(access.actor?.roles ?? []);
  return access.signed_in &&
    access.session_state === "active" &&
    access.assurance_level === "aal2" &&
    access.actor !== null &&
    roles.has("risk_approver") &&
    !roles.has("platform_admin") &&
    context.request !== null &&
    context.request.state === "requested" &&
    context.review === null &&
    context.break_state === "resolution_requested" &&
    context.reconciliation_state === "manual" &&
    context.request.requested_by.actor_id !== access.actor.actor_id &&
    Date.parse(context.request.expires_at) > now.getTime();
}

export function buildUnknownResolutionRequestDraft({
  context,
  access,
  evidence,
  now,
  idFactory
}: {
  readonly context: UnknownResolutionContextV2;
  readonly access: AccessContext;
  readonly evidence: UnknownResolutionEvidenceInput;
  readonly now: Date;
  readonly idFactory: OperationIdFactory;
}): UnknownResolutionRequestDraftV2 | null {
  if (!canRequestUnknownResolution(access, context)) {
    return null;
  }
  const requestedAt = now.toISOString();
  const request = {
    schema_version: 2 as const,
    request_id: idFactory(),
    environment: context.environment,
    idempotency_key: idFactory(),
    command_type: "close_unknown_execution" as const,
    break_id: context.break_id,
    intent_id: context.intent_id,
    unknown_observation_id: context.unknown_observation.observation_id,
    provider_order_id: context.provider_identity.provider_order_id,
    evidence_artifact_uri: evidence.evidenceArtifactUri,
    evidence_sha256: evidence.evidenceSha256,
    evidence_captured_at: evidence.evidenceCapturedAt,
    reason_code: "accounting_closure_requested" as const,
    expected_break_state: "open" as const,
    expected_break_revision: context.break_revision,
    expected_reconciliation_state: "manual" as const,
    expected_cash_projection_version: context.cash_projection_version,
    expected_position_projection_version: context.position_projection_version,
    expected_reservation_event_sequence: context.reservation_event_sequence,
    expected_control_epoch: context.control_epoch,
    terminal_status: evidence.terminalStatus,
    missing_fills: [...evidence.missingFills],
    requested_at: requestedAt,
    expires_at: new Date(now.getTime() + 30 * 60_000).toISOString()
  };
  return parseDataContract(
    unknownResolutionRequestDraftV2Schema,
    request,
    "unknown_resolution_request_draft_v2"
  );
}

export function buildUnknownResolutionReviewDraft({
  context,
  access,
  decision,
  now,
  idFactory
}: {
  readonly context: UnknownResolutionContextV2;
  readonly access: AccessContext;
  readonly decision: "approve" | "reject";
  readonly now: Date;
  readonly idFactory: OperationIdFactory;
}): UnknownResolutionReviewDraftV2 | null {
  if (!canReviewUnknownResolution(access, context, now) || context.request === null) {
    return null;
  }
  return parseDataContract(
    unknownResolutionReviewDraftV2Schema,
    {
      schema_version: 2,
      review_id: idFactory(),
      command_id: context.request.request_id,
      command_type: "close_unknown_execution",
      reviewer_role: "risk_approver",
      decision,
      reason_code: decision === "approve" ? "evidence_sufficient" : "evidence_incomplete",
      expected_receipt_revision: context.request.receipt_revision,
      expected_break_revision: context.break_revision,
      request_digest_sha256: context.request.request_digest_sha256,
      evidence_sha256: context.request.evidence_sha256,
      reviewed_at: now.toISOString()
    },
    "unknown_resolution_review_draft_v2"
  );
}

export function buildUnknownResolutionStepUpRequest(
  action: "request",
  draft: UnknownResolutionRequestDraftV2
): UnknownResolutionStepUpRequestV2;
export function buildUnknownResolutionStepUpRequest(
  action: "review",
  draft: UnknownResolutionReviewDraftV2
): UnknownResolutionStepUpRequestV2;
export function buildUnknownResolutionStepUpRequest(
  action: "request" | "review",
  draft: UnknownResolutionRequestDraftV2 | UnknownResolutionReviewDraftV2
): UnknownResolutionStepUpRequestV2 {
  return parseDataContract(
    unknownResolutionStepUpGrantDraftRequestV2Schema,
    {
      schema_version: 2,
      bound_action: action,
      bound_command_type: "close_unknown_execution",
      command_payload: draft
    },
    `unknown_resolution_step_up_v2:${action}`
  );
}

export function attachUnknownResolutionRequestGrant(
  draft: UnknownResolutionRequestDraftV2,
  grant: UnknownResolutionStepUpReceiptV2
): UnknownResolutionRequestV2 {
  const parsedGrant = parseDataContract(
    unknownResolutionStepUpGrantV2Schema,
    grant,
    "unknown_resolution_request_step_up_receipt_v2"
  );
  if (parsedGrant.bound_action !== "request") {
    throw new Error("unknown_resolution_request_step_up_action_mismatch");
  }
  return parseDataContract(
    unknownResolutionRequestV2Schema,
    { ...draft, ...parsedGrant },
    "unknown_resolution_request_v2"
  );
}

export function attachUnknownResolutionReviewGrant(
  draft: UnknownResolutionReviewDraftV2,
  grant: UnknownResolutionStepUpReceiptV2
): UnknownResolutionReviewV2 {
  const parsedGrant = parseDataContract(
    unknownResolutionStepUpGrantV2Schema,
    grant,
    "unknown_resolution_review_step_up_receipt_v2"
  );
  if (parsedGrant.bound_action !== "review") {
    throw new Error("unknown_resolution_review_step_up_action_mismatch");
  }
  return parseDataContract(
    unknownResolutionReviewV2Schema,
    { ...draft, ...parsedGrant },
    "unknown_resolution_review_v2"
  );
}
