import type {
  Incident,
  OperationCommandReceipt,
  OperationsSnapshot,
  UnknownResolutionContextV2
} from "./operationsContracts";
import type { OperationCommandType } from "./operationRequests";

export function operationsStateGuard(snapshot: OperationsSnapshot): string {
  const actor = snapshot.access.actor;
  const qualification = snapshot.qualification;
  return JSON.stringify({
    stateVersion: snapshot.runtime_health.state_version,
    environment: snapshot.runtime_health.environment,
    executionEnabled: snapshot.runtime_health.execution_enabled,
    activeStrategyVersionId: snapshot.runtime_health.active_strategy_version_id,
    activeRiskPolicyVersionId: snapshot.runtime_health.active_risk_policy_version_id,
    sessionState: snapshot.access.session_state,
    assuranceLevel: snapshot.access.assurance_level,
    actorId: actor?.actor_id ?? null,
    roles: [...(actor?.roles ?? [])].sort(),
    permissions: [...snapshot.access.permissions].sort(),
    qualification: qualification === null
      ? null
      : {
          status: qualification.status,
          environment: qualification.environment,
          releaseSha: qualification.release_sha,
          strategyVersionId: qualification.strategy_version_id,
          riskPolicyVersionId: qualification.risk_policy_version_id,
          ledgerCheckpoint: qualification.ledger_checkpoint,
          validUntil: qualification.valid_until
        }
  });
}

export function commandConfirmationGuard(
  snapshot: OperationsSnapshot,
  commandType: OperationCommandType
): string {
  return `${operationsStateGuard(snapshot)}:${commandType}`;
}

export function reviewConfirmationGuard(
  snapshot: OperationsSnapshot,
  command: OperationCommandReceipt
): string {
  return `${operationsStateGuard(snapshot)}:${command.command_id}:${command.state}:${command.control_plane_receipt.revision}`;
}

export function incidentConfirmationGuard(
  snapshot: OperationsSnapshot,
  incident: Incident,
  action: "acknowledge" | "resolve"
): string {
  return `${operationsStateGuard(snapshot)}:${incident.incident_id}:${incident.status}:${incident.owner?.actor_id ?? "none"}:${action}`;
}

export function unknownConfirmationGuard(
  snapshot: OperationsSnapshot,
  context: UnknownResolutionContextV2
): string {
  return JSON.stringify({
    operations: operationsStateGuard(snapshot),
    breakId: context.break_id,
    breakState: context.break_state,
    breakRevision: context.break_revision,
    reconciliationState: context.reconciliation_state,
    cashProjectionVersion: context.cash_projection_version,
    positionProjectionVersion: context.position_projection_version,
    reservationEventSequence: context.reservation_event_sequence,
    controlEpoch: context.control_epoch,
    requestId: context.request?.request_id ?? null,
    requestState: context.request?.state ?? null,
    requestRevision: context.request?.receipt_revision ?? null,
    requestExpiresAt: context.request?.expires_at ?? null,
    hasReview: context.review !== null,
    workReceiptState: context.work_receipt?.state ?? null,
    accountingApplicationRecorded: context.postcondition.accounting_application_recorded,
    resolutionComplete: context.postcondition.resolution_complete
  });
}
