import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { AppLayout } from "../src/components/Layout";
import { isCommandPostconditionVerified } from "../src/components/operations/SafetyCommandCenter";
import { OperationsStatusRailView } from "../src/components/operations/OperationsStatusRail";
import { operationsSnapshotQueryKey } from "../src/lib/operationsData";
import type { OperationsDataApi } from "../src/lib/operationsData";
import type { OperationCommandReceipt, OperationsSnapshot } from "../src/lib/operationsContracts";
import { buildSafetyRailModel } from "../src/lib/operationsStatusModel";
import {
  canMutateIncidentOperations,
  canMutateOperations,
  isOperationsSnapshotFresh,
  OperationsPage
} from "../src/pages/OperationsPage";
import { makeOperationsSnapshot } from "./operationsFixture";

interface RenderOptions {
  readonly offline?: boolean;
  readonly stale?: boolean;
  readonly aal1?: boolean;
  readonly running?: boolean;
  readonly workerOffline?: boolean;
  readonly commandState?: OperationCommandReceipt["state"];
  readonly appliedWithoutPostcondition?: boolean;
}

function renderOperations({
  offline = false,
  stale = false,
  aal1 = false,
  running = false,
  workerOffline = false,
  commandState,
  appliedWithoutPostcondition = false
}: RenderOptions = {}) {
  const snapshot = makeOperationsSnapshot();
  snapshot.runtime_health.execution_enabled = running;
  if (stale) {
    snapshot.runtime_health.overall_state = "stale";
  }
  if (workerOffline) {
    snapshot.runtime_health.overall_state = "offline";
    snapshot.runtime_health.worker_heartbeat_at = null;
    const worker = snapshot.runtime_health.components.find((component) => component.component === "worker");
    assert.ok(worker);
    worker.state = "offline";
  }
  if (aal1) {
    snapshot.access.assurance_level = "aal1";
    snapshot.access.active_step_up_grants = [];
  }
  if (commandState !== undefined) {
    snapshot.commands[0].state = commandState;
  }
  if (appliedWithoutPostcondition) {
    makeCommandAppliedWithoutPostcondition(snapshot);
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
      <AppLayout page="control" setPage={() => undefined} connectionState="connected">
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
const appHeader = freshDocument.querySelector('header[aria-label="앱 헤더"]');
assert.ok(appHeader, "the unified AppHeader is rendered");
const primaryNavigation = appHeader.querySelector('nav[aria-label="주 탐색"]');
assert.ok(primaryNavigation);
assert.deepEqual(
  Array.from(primaryNavigation.querySelectorAll("button"), (button) => button.textContent?.trim()),
  ["운영 제어", "계정·보안"],
  "the AppHeader exposes only the two approved top-level destinations"
);
assert.ok(freshDocument.querySelector('[aria-label="운영 상태 레일"]'));
assert.equal((freshText.match(/LIVE 잠금/g) ?? []).length, 1, "LIVE lock is shown once in the shared header rail");
assert.doesNotMatch(freshText, /LIVE 금지/, "legacy repeated LIVE warnings are removed from page content");
const unavailableLiveState = buildSafetyRailModel(null, true).items.find((item) => item.key === "live");
assert.ok(unavailableLiveState);
assert.equal(unavailableLiveState.value, "확인 불가", "missing snapshot must not imply that the LIVE lock is verified");
assert.equal(unavailableLiveState.tone, "warning");
assert.notEqual(unavailableLiveState.value, "잠금 유지");
assert.doesNotMatch(freshText, /로그인/, "the shared header uses device-session language instead of a persistent login state");
assert.match(freshText, /현재 실행 상태/);
assert.match(freshText, /지금 확인할 항목/);
assert.match(freshText, /검토 승인 · Worker 대기/);
assert.match(freshText, /Worker 적용 확인 없음/);
assert.match(freshText, /최근 기록/);
assert.doesNotMatch(freshText, /실주문 허용 활성화/);
assert.doesNotMatch(freshText, /샌드박스|broker_sandbox/);

const stoppedPrimaryButtons = commandButtonsByPriority(freshDocument, "primary");
assert.equal(stoppedPrimaryButtons.length, 1, "stopped runtime exposes exactly one primary CTA");
assert.equal(stoppedPrimaryButtons[0].textContent?.trim(), "모의거래 재개 요청");
assert.equal(stoppedPrimaryButtons[0].disabled, false, "a qualified stopped runtime may request a paper resume");

const runningDocument = renderOperations({ running: true });
const runningPrimaryButtons = commandButtonsByPriority(runningDocument, "primary");
assert.equal(runningPrimaryButtons.length, 1, "running runtime exposes exactly one primary CTA");
assert.equal(runningPrimaryButtons[0].textContent?.trim(), "모의거래 일시정지");
assert.equal(runningPrimaryButtons[0].disabled, false, "fresh online operator may request a paper pause");

const otherOperations = detailsBySummary(freshDocument, "기타 운영 작업");
const secondaryCommandButtons = Array.from(
  otherOperations.querySelectorAll<HTMLButtonElement>('button[data-command-priority="secondary"]')
);
assert.equal(secondaryCommandButtons.length, 3, "other operations contains the three non-primary command paths");
assert.deepEqual(
  secondaryCommandButtons.map((button) => commandButtonLabel(button)),
  ["모의거래 전략 적용", "위험 정책 적용", "계약 테스트 시작"]
);

const allCommandLabels = new Set([
  ...commandButtonLabels(freshDocument),
  ...commandButtonLabels(runningDocument)
]);
assert.deepEqual(
  [...allCommandLabels].sort(),
  [
    "계약 테스트 시작",
    "모의거래 일시정지",
    "모의거래 재개 요청",
    "모의거래 전략 적용",
    "비상 정지 요청",
    "위험 정책 적용"
  ].sort(),
  "the stopped and running states keep all six operation commands reachable through primary, danger, or other paths"
);

const contractTestButton = buttonByText(freshDocument, "계약 테스트 시작");
assert.equal(contractTestButton.disabled, true, "contract test action is disabled outside contract_test");

const commandTimeline = freshDocument.querySelector('ol[aria-label="명령 진행 4단계"]');
assert.ok(commandTimeline, "the most urgent command has an explicit four-step progress view");
assert.equal(commandTimeline.children.length, 4);
assert.deepEqual(
  Array.from(commandTimeline.children, (step) => step.querySelector("span")?.textContent?.trim()),
  [
    "1. 요청 접수",
    "2. 독립 검토자 승인",
    "3. Worker 작업 인수 / 적용",
    "4. 최신 실행 상태 확인"
  ]
);

const appliedDocument = renderOperations({ appliedWithoutPostcondition: true });
const appliedText = appliedDocument.body.textContent ?? "";
assert.match(appliedText, /Worker 적용 보고 · 확인 중/);
assert.match(appliedText, /완료로 표시하지 않습니다/);
assert.doesNotMatch(appliedText, /최신 실행 상태 확인 완료/);

const terminalLabels = new Map<OperationCommandReceipt["state"], string>([
  ["rejected", "검토 거절"],
  ["failed", "적용 실패"],
  ["expired", "요청 만료"],
  ["canceled", "요청 취소"]
]);
for (const [state, label] of terminalLabels) {
  const terminalDocument = renderOperations({ commandState: state });
  assert.match(
    terminalDocument.body.textContent ?? "",
    new RegExp(label),
    `${state} is rendered with its own terminal label`
  );
}
assert.equal(new Set(terminalLabels.values()).size, terminalLabels.size, "terminal command labels are not collapsed");

const staleDocument = renderOperations({ stale: true });
assert.ok(staleDocument.querySelector('[role="alert"]'));
assert.match(staleDocument.body.textContent ?? "", /데이터 지연 — 거래 제어 변경 차단/);
assert.equal(buttonByText(staleDocument, "비상 정지 요청").disabled, true);

const offlineDocument = renderOperations({ offline: true });
assert.match(offlineDocument.body.textContent ?? "", /네트워크 오프라인 — 거래 제어 변경 차단/);
assert.equal(commandButtonsByPriority(offlineDocument, "primary")[0].disabled, true);

const workerOfflineDocument = renderOperations({ workerOffline: true });
const workerOfflineRailDocument = renderStatusRail(workerOfflineSnapshot());
const tradingCommandStatus = definitionItemByTerm(workerOfflineRailDocument, "거래 명령");
assert.match(tradingCommandStatus.textContent ?? "", /차단/);
assert.match(tradingCommandStatus.textContent ?? "", /서비스 오프라인|Worker 상태 확인 필요|Worker 상태 신호 확인 필요/);
assert.equal(commandButtonsByPriority(workerOfflineDocument, "primary")[0].disabled, true);
assert.match(
  workerOfflineDocument.body.textContent ?? "",
  /거래 명령은 차단되지만 조건을 충족한 사고 확인 작업은 계속할 수 있습니다/,
  "the visible attention queue explains the narrower Worker-offline command block"
);

const aal1Document = renderOperations({ aal1: true });
assert.equal(buttonByText(aal1Document, "비상 정지 요청").disabled, true);

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

function commandButtonsByPriority(
  document: Document,
  priority: "primary" | "danger" | "secondary"
): HTMLButtonElement[] {
  return Array.from(
    document.querySelectorAll<HTMLButtonElement>(`button[data-command-priority="${priority}"]`)
  );
}

function commandButtonLabels(document: Document): string[] {
  return Array.from(
    document.querySelectorAll<HTMLButtonElement>("button[data-command-priority]")
  ).map((button) => commandButtonLabel(button));
}

function commandButtonLabel(button: HTMLButtonElement): string {
  return button.firstElementChild?.textContent?.trim() ?? button.textContent?.trim() ?? "";
}

function detailsBySummary(document: Document, label: string): HTMLDetailsElement {
  const details = Array.from(document.querySelectorAll("details")).find(
    (candidate) => candidate.querySelector("summary")?.textContent?.includes(label)
  );
  assert.ok(details, `Details not found: ${label}`);
  return details;
}

function definitionItemByTerm(document: Document, label: string): HTMLElement {
  const term = Array.from(document.querySelectorAll("dt")).find(
    (candidate) => candidate.textContent?.trim() === label
  );
  assert.ok(term, `Definition term not found: ${label}`);
  assert.ok(term.parentElement);
  return term.parentElement;
}

function makeCommandAppliedWithoutPostcondition(snapshot: OperationsSnapshot): void {
  const command = snapshot.commands[0];
  command.state = "applied";
  command.worker_ack = {
    schema_version: 1,
    ack_id: "34343434-3434-4434-8434-343434343434",
    command_id: command.command_id,
    worker_instance_id: "35353535-3535-4535-8535-353535353535",
    worker_release_sha: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    claimed_at: "2099-07-13T23:52:00.000Z",
    state: "applied",
    applied_at: "2099-07-13T23:53:00.000Z",
    post_state_version: snapshot.runtime_health.state_version + 1,
    failure_code: null
  };
}

function workerOfflineSnapshot(): OperationsSnapshot {
  const snapshot = makeOperationsSnapshot();
  snapshot.runtime_health.overall_state = "offline";
  snapshot.runtime_health.worker_heartbeat_at = null;
  const worker = snapshot.runtime_health.components.find((component) => component.component === "worker");
  assert.ok(worker);
  worker.state = "offline";
  return snapshot;
}

function renderStatusRail(snapshot: OperationsSnapshot): Document {
  const markup = renderToStaticMarkup(
    <OperationsStatusRailView
      snapshot={snapshot}
      isOnline={true}
      errorMessage={null}
      clientRealtime={null}
    />
  );
  return new JSDOM(markup).window.document;
}
