import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  classifyOperationsRpcError,
  OperationsTransportError,
  operationsErrorDetail,
  operationsErrorMessage,
  operationsErrorTitle,
  toOperationsTransportError
} from "../src/lib/operationsData";

const testDir = dirname(fileURLToPath(import.meta.url));
const dataSource = readFileSync(resolve(testDir, "../src/lib/operationsData.ts"), "utf8");
const pageSource = readFileSync(resolve(testDir, "../src/pages/OperationsPage.tsx"), "utf8");

assert.doesNotMatch(dataSource, /\.from\(/, "operations path must not mutate tables directly");
for (const rpc of [
  "get_desktop_operations_snapshot_v1",
  "issue_step_up_grant_v1",
  "request_operation_command_v1",
  "review_operation_command_v1",
  "act_on_operation_incident_v1",
  "issue_access_step_up_grant_v1",
  "request_access_change_v1",
  "review_access_change_v1",
  "get_unknown_resolution_cases_v2",
  "issue_unknown_resolution_step_up_v2",
  "request_unknown_resolution_v2",
  "review_unknown_resolution_v2"
]) {
  assert.match(dataSource, new RegExp(rpc), `missing RPC boundary: ${rpc}`);
}
for (const legacyRpc of [
  "get_platform_status",
  "request_operation_command",
  "review_operation_command",
  "emergency_stop",
  "acknowledge_incident",
  "resolve_incident"
]) {
  assert.doesNotMatch(
    dataSource,
    new RegExp(`\\.rpc\\(\\s*["']${legacyRpc}["']`),
    `legacy RPC must not remain: ${legacyRpc}`
  );
}
assert.match(dataSource, /parseDataContract\(operationsSnapshotSchema/);
assert.doesNotMatch(dataSource, /operationsMutationContractReady|mutationContractReady/);
assert.match(dataSource, /request_payload: request/);
assert.match(dataSource, /review_payload: request/);
assert.match(dataSource, /action_payload: request/);
assert.match(dataSource, /stepUpGrantIssueResponseSchema/);
assert.match(dataSource, /operationCommandReceiptSchema/);
assert.match(dataSource, /unknownResolutionSnapshotV2Schema/);
assert.match(dataSource, /unknownResolutionRequestV2Schema/);
assert.match(dataSource, /unknownResolutionReviewV2Schema/);
assert.match(dataSource, /unknownResolutionReceiptV2Schema/);
assert.doesNotMatch(
  dataSource,
  /requestUnknownResolution[\s\S]{0,1200}requestOperationCommand/,
  "unknown resolution must use its dedicated V2 request RPC"
);
assert.doesNotMatch(dataSource, /request_live_enable|live_order_allowed|broker_sandbox/);
assert.match(pageSource, /networkMode: "always"/, "mutations must fail immediately instead of pausing offline");
assert.match(pageSource, /retry: false/, "mutations must not retry or replay automatically");
assert.match(
  pageSource,
  /data:\s*props\.snapshotSource\.snapshot/,
  "shared operations UI must consume the provider's fail-closed snapshot"
);
assert.equal(
  operationsErrorMessage(new OperationsTransportError("api.snapshot", "configuration")),
  "운영 연결 설정이 없어 모든 운영 변경을 차단했습니다."
);
assert.equal(
  operationsErrorMessage(new OperationsTransportError("api.snapshot", "request")),
  "운영 연결을 사용할 수 없어 모든 운영 변경을 차단했습니다."
);
const backendSetupError = new OperationsTransportError("api.snapshot", "backend-setup");
assert.equal(
  operationsErrorMessage(backendSetupError),
  "운영 API 준비 상태를 확인해야 해 모든 운영 변경을 차단했습니다."
);
assert.equal(operationsErrorTitle(backendSetupError), "운영 API 준비 상태 확인 필요");
assert.match(operationsErrorDetail(backendSetupError), /데스크톱 V2 운영 API/);
assert.equal(classifyOperationsRpcError({ code: "PGRST106" }), "backend-setup");
assert.equal(classifyOperationsRpcError({ code: "PGRST202" }), "request");
assert.equal(classifyOperationsRpcError(null), "request");
const originalRpcError = { code: "PGRST106", message: "Invalid schema" };
const classifiedRpcError = toOperationsTransportError("api.snapshot", originalRpcError);
assert.equal(classifiedRpcError.reason, "backend-setup");
assert.equal(classifiedRpcError.backendCode, "PGRST106");
assert.equal(classifiedRpcError.cause, originalRpcError);

console.log("operations RPC/read-model boundary passed");
