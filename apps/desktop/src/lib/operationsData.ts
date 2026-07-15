import {
  accessChangeReceiptSchema,
  accessChangeRequestSchema,
  accessChangeReviewSchema,
  accessStepUpGrantDraftRequestSchema,
  accessStepUpGrantIssueResponseSchema,
  commandReviewRequestSchema,
  DataContractError,
  incidentActionReceiptSchema,
  incidentActionRequestSchema,
  operationCommandRequestSchema,
  operationCommandReceiptSchema,
  operationsSnapshotSchema,
  parseDataContract,
  stepUpGrantDraftRequestSchema,
  stepUpGrantIssueResponseSchema,
  unknownResolutionReceiptV2Schema,
  unknownResolutionRequestV2Schema,
  unknownResolutionReviewV2Schema,
  unknownResolutionSnapshotV2Schema,
  unknownResolutionStepUpGrantDraftRequestV2Schema,
  unknownResolutionStepUpGrantV2Schema
} from "./operationsContracts";
import type {
  AccessChangeReceipt,
  AccessChangeRequest,
  AccessChangeReviewRequest,
  AccessStepUpGrantDraftRequest,
  AccessStepUpGrantIssueResponse,
  CommandReviewRequest,
  IncidentActionReceipt,
  IncidentActionRequest,
  OperationCommandReceipt,
  OperationCommandRequest,
  OperationsSnapshot,
  StepUpGrantDraftRequest,
  StepUpGrantIssueResponse,
  UnknownResolutionReceiptV2,
  UnknownResolutionRequestV2,
  UnknownResolutionReviewV2,
  UnknownResolutionSnapshotV2,
  UnknownResolutionStepUpRequestV2,
  UnknownResolutionStepUpReceiptV2
} from "./operationsContracts";
import { hasSupabaseConfig, supabase } from "./supabaseClient";

export const operationsRpcCatalog = {
  operationsSnapshot: "get_desktop_operations_snapshot_v1",
  issueStepUpGrant: "issue_step_up_grant_v1",
  requestCommand: "request_operation_command_v1",
  reviewCommand: "review_operation_command_v1",
  actOnIncident: "act_on_operation_incident_v1",
  issueAccessStepUpGrant: "issue_access_step_up_grant_v1",
  requestAccessChange: "request_access_change_v1",
  reviewAccessChange: "review_access_change_v1",
  unknownResolutionSnapshot: "get_unknown_resolution_cases_v2",
  issueUnknownResolutionStepUpGrant: "issue_unknown_resolution_step_up_v2",
  requestUnknownResolution: "request_unknown_resolution_v2",
  reviewUnknownResolution: "review_unknown_resolution_v2"
} as const;

export const operationsSnapshotQueryKey = ["operations", "snapshot", 1] as const;
export const unknownResolutionSnapshotQueryKey = ["operations", "unknown-resolution", 2] as const;

export class OperationsTransportError extends Error {
  readonly operation: string;

  constructor(operation: string) {
    super(`운영 제어 API 호출에 실패했습니다: ${operation}`);
    this.name = "OperationsTransportError";
    this.operation = operation;
  }
}

export class OperationsResponseMismatchError extends Error {
  readonly operation: string;

  constructor(operation: string) {
    super(`운영 제어 API 응답 식별자가 요청과 일치하지 않습니다: ${operation}`);
    this.name = "OperationsResponseMismatchError";
    this.operation = operation;
  }
}

export interface OperationsDataApi {
  readonly fetchSnapshot: () => Promise<OperationsSnapshot>;
  readonly issueStepUpGrant: (input: StepUpGrantDraftRequest) => Promise<StepUpGrantIssueResponse>;
  readonly requestCommand: (input: OperationCommandRequest) => Promise<OperationCommandReceipt>;
  readonly reviewCommand: (input: CommandReviewRequest) => Promise<OperationCommandReceipt>;
  readonly actOnIncident: (input: IncidentActionRequest) => Promise<IncidentActionReceipt>;
}

export interface AccessChangeDataApi {
  readonly issueStepUpGrant: (input: AccessStepUpGrantDraftRequest) => Promise<AccessStepUpGrantIssueResponse>;
  readonly requestChange: (input: AccessChangeRequest) => Promise<AccessChangeReceipt>;
  readonly reviewChange: (input: AccessChangeReviewRequest) => Promise<AccessChangeReceipt>;
}

export interface UnknownResolutionDataApi {
  readonly fetchSnapshot: () => Promise<UnknownResolutionSnapshotV2>;
  readonly issueStepUpGrant: (
    input: UnknownResolutionStepUpRequestV2
  ) => Promise<UnknownResolutionStepUpReceiptV2>;
  readonly requestResolution: (
    input: UnknownResolutionRequestV2
  ) => Promise<UnknownResolutionReceiptV2>;
  readonly reviewResolution: (
    input: UnknownResolutionReviewV2
  ) => Promise<UnknownResolutionReceiptV2>;
}

export async function fetchUnknownResolutionSnapshot(): Promise<UnknownResolutionSnapshotV2> {
  const operation = `api.${operationsRpcCatalog.unknownResolutionSnapshot}`;
  const client = requireOperationsClient(operation);
  const result = await client.schema("api").rpc(operationsRpcCatalog.unknownResolutionSnapshot);
  failOnRpcError(operation, result.error);
  return parseDataContract(unknownResolutionSnapshotV2Schema, result.data, operation);
}

export async function issueUnknownResolutionStepUpGrant(
  input: UnknownResolutionStepUpRequestV2
): Promise<UnknownResolutionStepUpReceiptV2> {
  const operation = `api.${operationsRpcCatalog.issueUnknownResolutionStepUpGrant}`;
  const request = parseDataContract(
    unknownResolutionStepUpGrantDraftRequestV2Schema,
    input,
    `${operation}:request`
  );
  const client = requireOperationsClient(operation);
  const result = await client.schema("api").rpc(
    operationsRpcCatalog.issueUnknownResolutionStepUpGrant,
    { request_payload: request }
  );
  failOnRpcError(operation, result.error);
  const grant = parseDataContract(
    unknownResolutionStepUpGrantV2Schema,
    result.data,
    `${operation}:response`
  );
  if (
    grant.bound_action !== request.bound_action ||
    grant.bound_command_type !== request.bound_command_type
  ) {
    throw new OperationsResponseMismatchError(operation);
  }
  return grant;
}

export async function requestUnknownResolution(
  input: UnknownResolutionRequestV2
): Promise<UnknownResolutionReceiptV2> {
  const operation = `api.${operationsRpcCatalog.requestUnknownResolution}`;
  const request = parseDataContract(
    unknownResolutionRequestV2Schema,
    input,
    `${operation}:request`
  );
  const client = requireOperationsClient(operation);
  const result = await client.schema("api").rpc(operationsRpcCatalog.requestUnknownResolution, {
    request_payload: request
  });
  failOnRpcError(operation, result.error);
  const receipt = parseDataContract(
    unknownResolutionReceiptV2Schema,
    result.data,
    `${operation}:response`
  );
  if (
    receipt.command_id !== request.request_id ||
    receipt.break_id !== request.break_id ||
    receipt.intent_id !== request.intent_id ||
    receipt.terminal_status !== request.terminal_status
  ) {
    throw new OperationsResponseMismatchError(operation);
  }
  return receipt;
}

export async function reviewUnknownResolution(
  input: UnknownResolutionReviewV2
): Promise<UnknownResolutionReceiptV2> {
  const operation = `api.${operationsRpcCatalog.reviewUnknownResolution}`;
  const review = parseDataContract(
    unknownResolutionReviewV2Schema,
    input,
    `${operation}:request`
  );
  const client = requireOperationsClient(operation);
  const result = await client.schema("api").rpc(operationsRpcCatalog.reviewUnknownResolution, {
    review_payload: review
  });
  failOnRpcError(operation, result.error);
  const receipt = parseDataContract(
    unknownResolutionReceiptV2Schema,
    result.data,
    `${operation}:response`
  );
  if (receipt.command_id !== review.command_id) {
    throw new OperationsResponseMismatchError(operation);
  }
  return receipt;
}

export async function issueAccessStepUpGrant(
  input: AccessStepUpGrantDraftRequest
): Promise<AccessStepUpGrantIssueResponse> {
  const operation = `api.${operationsRpcCatalog.issueAccessStepUpGrant}`;
  const request = parseDataContract(accessStepUpGrantDraftRequestSchema, input, `${operation}:request`);
  const client = requireOperationsClient(operation);
  const result = await client.schema("api").rpc(operationsRpcCatalog.issueAccessStepUpGrant, {
    request_payload: request
  });
  failOnRpcError(operation, result.error);
  const grant = parseDataContract(accessStepUpGrantIssueResponseSchema, result.data, `${operation}:response`);
  if (grant.bound_action !== request.bound_action) {
    throw new OperationsResponseMismatchError(operation);
  }
  return grant;
}

export async function requestAccessChange(input: AccessChangeRequest): Promise<AccessChangeReceipt> {
  const operation = `api.${operationsRpcCatalog.requestAccessChange}`;
  const request = parseDataContract(accessChangeRequestSchema, input, `${operation}:request`);
  const client = requireOperationsClient(operation);
  const result = await client.schema("api").rpc(operationsRpcCatalog.requestAccessChange, {
    request_payload: request
  });
  failOnRpcError(operation, result.error);
  const receipt = parseDataContract(accessChangeReceiptSchema, result.data, `${operation}:response`);
  if (
    receipt.request_id !== request.request_id ||
    receipt.subject_user_id !== request.subject_user_id ||
    receipt.requested_role !== request.requested_role ||
    receipt.change_type !== request.change_type
  ) {
    throw new OperationsResponseMismatchError(operation);
  }
  return receipt;
}

export async function reviewAccessChange(input: AccessChangeReviewRequest): Promise<AccessChangeReceipt> {
  const operation = `api.${operationsRpcCatalog.reviewAccessChange}`;
  const review = parseDataContract(accessChangeReviewSchema, input, `${operation}:request`);
  const client = requireOperationsClient(operation);
  const result = await client.schema("api").rpc(operationsRpcCatalog.reviewAccessChange, {
    review_payload: review
  });
  failOnRpcError(operation, result.error);
  const receipt = parseDataContract(accessChangeReceiptSchema, result.data, `${operation}:response`);
  if (receipt.request_id !== review.request_id) {
    throw new OperationsResponseMismatchError(operation);
  }
  return receipt;
}

export async function issueOperationStepUpGrant(
  input: StepUpGrantDraftRequest
): Promise<StepUpGrantIssueResponse> {
  const operation = `api.${operationsRpcCatalog.issueStepUpGrant}`;
  const request = parseDataContract(stepUpGrantDraftRequestSchema, input, `${operation}:request`);
  const client = requireOperationsClient(operation);
  const result = await client.schema("api").rpc(operationsRpcCatalog.issueStepUpGrant, {
    request_payload: request
  });
  failOnRpcError(operation, result.error);
  const grant = parseDataContract(stepUpGrantIssueResponseSchema, result.data, `${operation}:response`);
  if (grant.bound_action !== request.bound_action || grant.bound_command_type !== request.bound_command_type) {
    throw new OperationsResponseMismatchError(operation);
  }
  return grant;
}

export async function fetchOperationsSnapshot(): Promise<OperationsSnapshot> {
  const operation = `api.${operationsRpcCatalog.operationsSnapshot}`;
  const client = requireOperationsClient(operation);
  const result = await client.schema("api").rpc(operationsRpcCatalog.operationsSnapshot);
  failOnRpcError(operation, result.error);
  return parseDataContract(operationsSnapshotSchema, result.data, operation);
}

export async function requestOperationCommand(
  input: OperationCommandRequest
): Promise<OperationCommandReceipt> {
  const operation = `api.${operationsRpcCatalog.requestCommand}`;
  const request = parseDataContract(operationCommandRequestSchema, input, `${operation}:request`);
  const client = requireOperationsClient(operation);
  const result = await client.schema("api").rpc(operationsRpcCatalog.requestCommand, {
    request_payload: request
  });
  failOnRpcError(operation, result.error);
  const receipt = parseDataContract(operationCommandReceiptSchema, result.data, `${operation}:response`);
  if (
    receipt.command_id !== request.request_id ||
    receipt.command_type !== request.command_type ||
    receipt.environment !== request.environment ||
    (request.command_type !== "emergency_stop" && receipt.command_hash !== request.command_hash) ||
    (request.command_type !== "emergency_stop" && request.command_type !== "pause_paper" &&
      (receipt.qualification_id !== request.qualification_id ||
        receipt.strategy_version_id !== request.strategy_version_id ||
        receipt.risk_policy_version_id !== request.risk_policy_version_id ||
        receipt.release_sha !== request.release_sha ||
        receipt.ledger_checkpoint !== request.ledger_checkpoint))
  ) {
    throw new OperationsResponseMismatchError(operation);
  }
  return receipt;
}

export async function reviewOperationCommand(
  input: CommandReviewRequest
): Promise<OperationCommandReceipt> {
  const operation = `api.${operationsRpcCatalog.reviewCommand}`;
  const request = parseDataContract(commandReviewRequestSchema, input, `${operation}:request`);
  const client = requireOperationsClient(operation);
  const result = await client.schema("api").rpc(operationsRpcCatalog.reviewCommand, {
    review_payload: request
  });
  failOnRpcError(operation, result.error);
  const receipt = parseDataContract(operationCommandReceiptSchema, result.data, `${operation}:response`);
  if (receipt.command_id !== request.command_id || receipt.command_type !== request.command_type) {
    throw new OperationsResponseMismatchError(operation);
  }
  return receipt;
}

export async function actOnOperationIncident(input: IncidentActionRequest): Promise<IncidentActionReceipt> {
  const operation = `api.${operationsRpcCatalog.actOnIncident}`;
  const request = parseDataContract(
    incidentActionRequestSchema,
    input,
    `${operation}:request`
  );
  const client = requireOperationsClient(operation);
  const result = await client.schema("api").rpc(operationsRpcCatalog.actOnIncident, {
    action_payload: request
  });
  failOnRpcError(operation, result.error);
  const receipt = parseDataContract(incidentActionReceiptSchema, result.data, `${operation}:response`);
  if (receipt.action_id !== request.action_id || receipt.incident_id !== request.incident_id) {
    throw new OperationsResponseMismatchError(operation);
  }
  return receipt;
}

export const operationsDataApi: OperationsDataApi = {
  fetchSnapshot: fetchOperationsSnapshot,
  issueStepUpGrant: issueOperationStepUpGrant,
  requestCommand: requestOperationCommand,
  reviewCommand: reviewOperationCommand,
  actOnIncident: actOnOperationIncident
};

export const accessChangeDataApi: AccessChangeDataApi = {
  issueStepUpGrant: issueAccessStepUpGrant,
  requestChange: requestAccessChange,
  reviewChange: reviewAccessChange
};

export const unknownResolutionDataApi: UnknownResolutionDataApi = {
  fetchSnapshot: fetchUnknownResolutionSnapshot,
  issueStepUpGrant: issueUnknownResolutionStepUpGrant,
  requestResolution: requestUnknownResolution,
  reviewResolution: reviewUnknownResolution
};

export function operationsErrorMessage(error: unknown): string {
  if (error instanceof DataContractError) {
    if (error.source.includes("unknown_resolution")) {
      return "수동 대사 응답이 schema_version=2 계약과 일치하지 않아 모든 회계 조정을 차단했습니다.";
    }
    return "응답이 schema_version=1 계약과 일치하지 않아 모든 운영 변경을 차단했습니다.";
  }
  if (error instanceof OperationsTransportError) {
    return "운영 연결을 사용할 수 없어 모든 운영 변경을 차단했습니다.";
  }
  if (error instanceof OperationsResponseMismatchError) {
    return "운영 제어 응답이 요청 식별자와 일치하지 않아 결과를 반영하지 않았습니다.";
  }
  return "운영 상태를 확인할 수 없어 모든 운영 변경을 차단했습니다.";
}

function requireOperationsClient(operation: string) {
  if (!hasSupabaseConfig || supabase === null) {
    throw new OperationsTransportError(operation);
  }
  return supabase;
}

function failOnRpcError(operation: string, error: unknown): void {
  if (error) {
    throw new OperationsTransportError(operation);
  }
}
