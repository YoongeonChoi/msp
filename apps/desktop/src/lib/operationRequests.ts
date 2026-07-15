import {
  commandReviewDraftSchema,
  commandReviewRequestSchema,
  incidentActionRequestSchema,
  operationCommandRequestSchema,
  parseDataContract,
  stepUpCommandDraftSchema,
  stepUpGrantDraftRequestSchema,
  stepUpGrantIssueResponseSchema
} from "./operationsContracts";
import type {
  AccessContext,
  CommandReviewDraft,
  CommandReviewRequest,
  Incident,
  IncidentActionRequest,
  OperationCommandReceipt,
  OperationCommandRequest,
  OperationsSnapshot,
  StepUpCommandDraft,
  StepUpGrantDraftRequest,
  StepUpGrantIssueResponse
} from "./operationsContracts";

export type OperationCommandType = OperationCommandRequest["command_type"];
export type StepUpOperationCommandType = Exclude<OperationCommandType, "emergency_stop">;
export type OperationIdFactory = () => string;

export function secureOperationId(): string {
  return crypto.randomUUID();
}

export function buildEmergencyStopCommandRequest({
  snapshot,
  now,
  idFactory = secureOperationId
}: {
  readonly snapshot: OperationsSnapshot;
  readonly now: Date;
  readonly idFactory?: OperationIdFactory;
}): OperationCommandRequest | null {
  const requestedAt = now.toISOString();
  const expiresAt = new Date(now.getTime() + 5 * 60_000).toISOString();
  if (
    snapshot.access.assurance_level !== "aal2" ||
    snapshot.access.actor === null ||
    !snapshot.access.actor.roles.includes("operator")
  ) {
    return null;
  }
  return parseDataContract(
    operationCommandRequestSchema,
    {
      schema_version: 1,
      request_id: idFactory(),
      environment: snapshot.runtime_health.environment,
      idempotency_key: idFactory(),
      expected_state_version: snapshot.runtime_health.state_version,
      requested_at: requestedAt,
      expires_at: expiresAt,
      command_type: "emergency_stop",
      reason_code: "operator_safety_stop",
      assurance_level: "aal2",
      actor_role: "operator",
      single_actor: true
    },
    "desktop:emergency_stop_request"
  );
}

export function buildOperationCommandDraft({
  commandType,
  snapshot,
  now,
  idFactory = secureOperationId
}: {
  readonly commandType: StepUpOperationCommandType;
  readonly snapshot: OperationsSnapshot;
  readonly now: Date;
  readonly idFactory?: OperationIdFactory;
}): StepUpCommandDraft | null {
  if (
    snapshot.access.assurance_level !== "aal2" ||
    snapshot.access.actor === null ||
    !snapshot.access.permissions.includes("request_command")
  ) {
    return null;
  }
  const requestedAt = now.toISOString();
  const base = {
    schema_version: 1 as const,
    request_id: idFactory(),
    environment: snapshot.runtime_health.environment,
    idempotency_key: idFactory(),
    expected_state_version: snapshot.runtime_health.state_version,
    requested_at: requestedAt,
    expires_at: new Date(now.getTime() + 5 * 60_000).toISOString()
  };
  if (commandType === "pause_paper") {
    return parseDataContract(
      stepUpCommandDraftSchema,
      { ...base, command_type: commandType, reason_code: "operator_pause" },
      "desktop:command_draft"
    );
  }

  const qualification = snapshot.qualification;
  if (
    qualification === null ||
    qualification.status !== "qualified" ||
    qualification.environment !== snapshot.runtime_health.environment ||
    Date.parse(qualification.valid_from) > now.getTime() ||
    Date.parse(qualification.valid_until) <= now.getTime()
  ) {
    return null;
  }

  const reasonCode =
    commandType === "resume_paper"
      ? "qualified_resume"
      : commandType === "start_contract_test"
        ? "boundary_verification"
        : "approved_change";
  return parseDataContract(
    stepUpCommandDraftSchema,
    {
      ...base,
      command_type: commandType,
      qualification_id: qualification.qualification_id,
      strategy_version_id: qualification.strategy_version_id,
      risk_policy_version_id: qualification.risk_policy_version_id,
      release_sha: qualification.release_sha,
      ledger_checkpoint: qualification.ledger_checkpoint,
      reason_code: reasonCode
    },
    "desktop:command_draft"
  );
}

export function attachStepUpGrantToCommandDraft(
  draft: StepUpCommandDraft,
  grant: StepUpGrantIssueResponse
): OperationCommandRequest {
  const parsedGrant = parseDataContract(stepUpGrantIssueResponseSchema, grant, "api.issue_step_up_grant_v1:response");
  return parseDataContract(
    operationCommandRequestSchema,
    {
      ...draft,
      step_up_grant_id: parsedGrant.step_up_grant_id,
      command_hash: parsedGrant.command_hash,
      step_up_grant_issued_at: parsedGrant.step_up_grant_issued_at,
      step_up_grant_expires_at: parsedGrant.step_up_grant_expires_at,
      step_up_grant_one_time: parsedGrant.step_up_grant_one_time,
      step_up_grant_consumed_at: parsedGrant.step_up_grant_consumed_at,
      bound_action: parsedGrant.bound_action,
      bound_command_type: parsedGrant.bound_command_type
    },
    "desktop:command_request"
  );
}

export function buildCommandReviewDraft({
  command,
  access,
  decision,
  now,
  idFactory = secureOperationId
}: {
  readonly command: OperationCommandReceipt;
  readonly access: AccessContext;
  readonly decision: "approve" | "reject";
  readonly now: Date;
  readonly idFactory?: OperationIdFactory;
}): CommandReviewDraft | null {
  if (command.command_type === "emergency_stop" || command.command_hash === null) {
    return null;
  }
  const reviewerRole = requiredReviewerRole(command.command_type);
  const actorRoles = access.actor?.roles ?? [];
  if (actorRoles.includes("platform_admin") || !actorRoles.includes(reviewerRole)) {
    return null;
  }
  return parseDataContract(
    commandReviewDraftSchema,
    {
      schema_version: 1,
      review_id: idFactory(),
      command_id: command.command_id,
      command_type: command.command_type,
      reviewer_role: reviewerRole,
      decision,
      reason_code: decision === "approve" ? "policy_satisfied" : "evidence_incomplete",
      expected_receipt_revision: command.control_plane_receipt.revision,
      reviewed_at: now.toISOString()
    },
    "desktop:command_review_draft"
  );
}

export function attachStepUpGrantToReviewDraft(
  draft: CommandReviewDraft,
  grant: StepUpGrantIssueResponse
): CommandReviewRequest {
  const parsedGrant = parseDataContract(stepUpGrantIssueResponseSchema, grant, "api.issue_step_up_grant_v1:response");
  return parseDataContract(
    commandReviewRequestSchema,
    {
      ...draft,
      step_up_grant_id: parsedGrant.step_up_grant_id,
      command_hash: parsedGrant.command_hash,
      step_up_grant_issued_at: parsedGrant.step_up_grant_issued_at,
      step_up_grant_expires_at: parsedGrant.step_up_grant_expires_at,
      step_up_grant_one_time: parsedGrant.step_up_grant_one_time,
      step_up_grant_consumed_at: parsedGrant.step_up_grant_consumed_at,
      bound_action: parsedGrant.bound_action,
      bound_command_type: parsedGrant.bound_command_type
    },
    "desktop:command_review"
  );
}

export function buildStepUpGrantDraftRequest(
  action: "request",
  draft: StepUpCommandDraft
): StepUpGrantDraftRequest;
export function buildStepUpGrantDraftRequest(
  action: "review",
  draft: CommandReviewDraft
): StepUpGrantDraftRequest;
export function buildStepUpGrantDraftRequest(
  action: "request" | "review",
  draft: StepUpCommandDraft | CommandReviewDraft
): StepUpGrantDraftRequest {
  return parseDataContract(
    stepUpGrantDraftRequestSchema,
    {
      schema_version: 1,
      bound_action: action,
      bound_command_type: draft.command_type,
      command_payload: draft
    },
    "desktop:step_up_grant_draft"
  );
}

export function requiredReviewerRole(
  commandType: Exclude<OperationCommandType, "emergency_stop">
): "risk_approver" | "strategy_reviewer" | "release_manager" {
  if (commandType === "activate_paper_strategy") {
    return "strategy_reviewer";
  }
  return "risk_approver";
}

export function buildIncidentActionRequest({
  incident,
  action,
  now,
  idFactory = secureOperationId
}: {
  readonly incident: Incident;
  readonly action: "acknowledge" | "resolve";
  readonly now: Date;
  readonly idFactory?: OperationIdFactory;
}): IncidentActionRequest {
  if (incident.status === "resolved") {
    throw new Error("resolved_incident_is_immutable");
  }
  return parseDataContract(
    incidentActionRequestSchema,
    {
      schema_version: 1,
      action_id: idFactory(),
      incident_id: incident.incident_id,
      action,
      reason_code: action === "acknowledge" ? "operator_acknowledged" : "mitigation_verified",
      expected_status: incident.status,
      acted_at: now.toISOString()
    },
    "desktop:incident_action"
  );
}
