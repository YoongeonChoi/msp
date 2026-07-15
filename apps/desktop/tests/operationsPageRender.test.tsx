import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { AppLayout } from "../src/components/Layout";
import { isCommandPostconditionVerified } from "../src/components/operations/SafetyCommandCenter";
import { operationsSnapshotQueryKey } from "../src/lib/operationsData";
import type { OperationsDataApi } from "../src/lib/operationsData";
import {
  canMutateIncidentOperations,
  canMutateOperations,
  isOperationsSnapshotFresh,
  OperationsPage
} from "../src/pages/OperationsPage";
import { makeOperationsSnapshot } from "./operationsFixture";

function renderOperations({ offline = false, stale = false, selfReview = false, aal1 = false } = {}) {
  const snapshot = makeOperationsSnapshot();
  if (stale) {
    snapshot.runtime_health.overall_state = "stale";
  }
  if (selfReview && snapshot.access.actor) {
    snapshot.access.actor.actor_id = snapshot.pending_reviews[0].requested_by.actor_id;
  }
  if (aal1) {
    snapshot.access.assurance_level = "aal1";
    snapshot.access.active_step_up_grants = [];
  }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(operationsSnapshotQueryKey, snapshot);
  const dataApi: OperationsDataApi = {
    fetchSnapshot: async () => snapshot,
    issueStepUpGrant: async (input) => {
      const grant = snapshot.access.active_step_up_grants.find(
        (candidate) => candidate.bound_action === input.bound_action && candidate.bound_command_type === input.bound_command_type
      );
      assert.ok(grant);
      return { schema_version: 1, ...grant };
    },
    requestCommand: async () => snapshot.commands[0],
    reviewCommand: async () => snapshot.pending_reviews[0],
    actOnIncident: async (input) => ({
      schema_version: 1,
      incident_id: input.incident_id,
      status: input.action === "acknowledge" ? "acknowledged" : "resolved",
      action_id: input.action_id,
      acted_at: input.acted_at
    })
  };
  const markup = renderToStaticMarkup(
    <QueryClientProvider client={client}>
      <AppLayout page="control" setPage={() => undefined}>
        <OperationsPage
          dataApi={dataApi}
          onlineOverride={!offline}
          nowFactory={() => new Date("2099-07-14T00:00:00.000Z")}
        />
      </AppLayout>
    </QueryClientProvider>
  );
  return new JSDOM(markup).window.document;
}

const freshDocument = renderOperations();
const freshText = freshDocument.body.textContent ?? "";
assert.ok(freshDocument.querySelector('[aria-label="운영 상태 레일"]'));
assert.match(freshText, /LIVE 금지/);
assert.match(freshText, /CONTRACT TEST \/ 계약 테스트/);
assert.match(freshText, /안전 명령 센터/);
assert.match(freshText, /승인은 제어면 영수증이며 Worker 적용 완료를 의미하지 않습니다/);
assert.match(freshText, /승인됨 · 적용 전/);
assert.match(freshText, /Worker ACK 없음/);
assert.match(freshText, /수동 대사 케이스/);
assert.match(freshText, /브로커 API를 직접 호출하거나 주문 상태를 임의 보정하지 않습니다/);
assert.doesNotMatch(freshText, /실주문 허용 활성화/);
assert.doesNotMatch(freshText, /샌드박스|broker_sandbox/);

const pauseButton = buttonByText(freshDocument, "PAPER 일시정지");
assert.equal(pauseButton.disabled, false, "fresh online operator may request a paper pause");
const contractTestButton = buttonByText(freshDocument, "CONTRACT TEST 시작");
assert.equal(contractTestButton.disabled, true, "contract test action is disabled outside contract_test");
const approveButton = buttonByText(freshDocument, "승인");
assert.equal(approveButton.disabled, false, "independent approver may review a request");

const staleDocument = renderOperations({ stale: true });
assert.ok(staleDocument.querySelector('[role="alert"]'));
assert.match(staleDocument.body.textContent ?? "", /데이터 지연 — 거래 제어 변경 차단/);
assert.equal(buttonByText(staleDocument, "비상 정지 요청").disabled, true);
assert.equal(buttonByText(staleDocument, "승인").disabled, true);
assert.equal(
  buttonByText(staleDocument, "확인 접수").disabled,
  false,
  "incident acknowledgement remains available through a current healthy control plane"
);

const offlineDocument = renderOperations({ offline: true });
assert.match(offlineDocument.body.textContent ?? "", /네트워크 오프라인 — 거래 제어 변경 차단/);
assert.equal(buttonByText(offlineDocument, "PAPER 일시정지").disabled, true);

const selfReviewDocument = renderOperations({ selfReview: true });
assert.match(selfReviewDocument.body.textContent ?? "", /본인 요청은 승인하거나 거절할 수 없습니다/);
assert.equal(buttonByText(selfReviewDocument, "승인").disabled, true);

const aal1Document = renderOperations({ aal1: true });
assert.equal(buttonByText(aal1Document, "비상 정지 요청").disabled, true);
assert.equal(buttonByText(aal1Document, "확인 접수").disabled, true, "AAL1 must block every operation mutation");

const postconditionSnapshot = makeOperationsSnapshot();
postconditionSnapshot.commands[0].state = "applied";
postconditionSnapshot.commands[0].worker_ack = {
  schema_version: 1,
  ack_id: "34343434-3434-4434-8434-343434343434",
  command_id: postconditionSnapshot.commands[0].command_id,
  worker_instance_id: "35353535-3535-4535-8535-353535353535",
  worker_release_sha: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  claimed_at: "2099-07-13T23:52:00.000Z",
  state: "applied",
  applied_at: "2099-07-13T23:53:00.000Z",
  post_state_version: 18,
  failure_code: null
};
assert.equal(isCommandPostconditionVerified(postconditionSnapshot.commands[0], postconditionSnapshot), false);
postconditionSnapshot.runtime_health.state_version = 18;
assert.equal(isCommandPostconditionVerified(postconditionSnapshot.commands[0], postconditionSnapshot), true);
postconditionSnapshot.runtime_health.execution_enabled = true;
assert.equal(
  isCommandPostconditionVerified(postconditionSnapshot.commands[0], postconditionSnapshot),
  false,
  "pause must observe execution disabled, not merely a newer state version"
);

const identitySnapshot = makeOperationsSnapshot();
const identityCommand = structuredClone(identitySnapshot.pending_reviews[0]);
identityCommand.state = "applied";
identityCommand.control_plane_receipt.state = "approved";
identityCommand.control_plane_receipt.approved_at = "2099-07-13T23:59:00.000Z";
identityCommand.control_plane_receipt.approved_by = identitySnapshot.access.actor;
identityCommand.worker_ack = {
  schema_version: 1,
  ack_id: "36363636-3636-4636-8636-363636363636",
  command_id: identityCommand.command_id,
  worker_instance_id: "37373737-3737-4737-8737-373737373737",
  worker_release_sha: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  claimed_at: "2099-07-13T23:59:01.000Z",
  state: "applied",
  applied_at: "2099-07-13T23:59:02.000Z",
  post_state_version: 18,
  failure_code: null
};
identitySnapshot.runtime_health.state_version = 18;
identitySnapshot.runtime_health.execution_enabled = true;
assert.equal(isCommandPostconditionVerified(identityCommand, identitySnapshot), true);

identityCommand.command_type = "activate_paper_strategy";
identitySnapshot.runtime_health.active_strategy_version_id = null;
assert.equal(isCommandPostconditionVerified(identityCommand, identitySnapshot), false);
identitySnapshot.runtime_health.active_strategy_version_id = identityCommand.strategy_version_id;
assert.equal(isCommandPostconditionVerified(identityCommand, identitySnapshot), true);

identityCommand.command_type = "apply_risk_policy_version";
identitySnapshot.runtime_health.active_risk_policy_version_id = null;
assert.equal(isCommandPostconditionVerified(identityCommand, identitySnapshot), false);
identitySnapshot.runtime_health.active_risk_policy_version_id = identityCommand.risk_policy_version_id;
assert.equal(isCommandPostconditionVerified(identityCommand, identitySnapshot), true);

identityCommand.command_type = "start_contract_test";
identityCommand.environment = "contract_test";
assert.equal(isCommandPostconditionVerified(identityCommand, identitySnapshot), false);
identitySnapshot.runtime_health.environment = "contract_test";
assert.equal(isCommandPostconditionVerified(identityCommand, identitySnapshot), true);

const oldSnapshot = makeOperationsSnapshot();
oldSnapshot.generated_at = "2099-07-13T23:58:00.000Z";
assert.equal(
  isOperationsSnapshotFresh(oldSnapshot, new Date("2099-07-14T00:00:00.000Z")),
  false,
  "client-side snapshot age must fail closed even when the server labels it fresh"
);

const realtimeDisconnectedSnapshot = makeOperationsSnapshot();
realtimeDisconnectedSnapshot.runtime_health.realtime_connected = false;
realtimeDisconnectedSnapshot.runtime_health.realtime_last_seen_at = null;
assert.equal(
  isOperationsSnapshotFresh(realtimeDisconnectedSnapshot, new Date("2099-07-14T00:00:00.000Z")),
  false,
  "missing canonical Realtime publication must block mutations while polling remains read-only"
);
assert.equal(
  isOperationsSnapshotFresh(
    realtimeDisconnectedSnapshot,
    new Date("2099-07-14T00:00:00.000Z"),
    {
      connected: true,
      connectedAt: "2099-07-14T00:00:00.000Z",
      lastSignalAt: null
    }
  ),
  false,
  "a SUBSCRIBED channel without a canonical server signal must remain read-only"
);

const incidentDegradedSnapshot = makeOperationsSnapshot();
incidentDegradedSnapshot.runtime_health.overall_state = "offline";
incidentDegradedSnapshot.runtime_health.worker_heartbeat_at = null;
const workerComponent = incidentDegradedSnapshot.runtime_health.components.find(
  (component) => component.component === "worker"
);
assert.ok(workerComponent);
workerComponent.state = "offline";
assert.equal(
  canMutateOperations(incidentDegradedSnapshot, true, new Date("2099-07-14T00:00:00.000Z")),
  false,
  "dangerous commands must remain blocked while worker state is not fresh"
);
assert.equal(
  canMutateIncidentOperations(incidentDegradedSnapshot, true, new Date("2099-07-14T00:00:00.000Z")),
  true,
  "a current healthy control plane must still permit incident acknowledgement when the worker is offline"
);

console.log("operations page render fixtures passed");

function buttonByText(document: Document, label: string): HTMLButtonElement {
  const buttons = Array.from(document.querySelectorAll("button"));
  const button =
    buttons.find((candidate) => candidate.textContent?.trim() === label) ??
    buttons.find((candidate) => candidate.textContent?.includes(label));
  assert.ok(button, `Button not found: ${label}`);
  return button as HTMLButtonElement;
}
