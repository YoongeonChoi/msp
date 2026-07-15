import assert from "node:assert/strict";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type {
  OperationCommandRequest,
  StepUpGrantDraftRequest,
  UnknownResolutionRequestV2,
  UnknownResolutionReviewV2,
  UnknownResolutionStepUpRequestV2
} from "../src/lib/operationsContracts";
import type {
  OperationsDataApi,
  UnknownResolutionDataApi
} from "../src/lib/operationsData";
import { OperationsPage } from "../src/pages/OperationsPage";
import { makeOperationsSnapshot } from "./operationsFixture";
import { makeUnknownResolutionSnapshot } from "./unknownResolutionFixture";

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  pretendToBeVisual: true,
  url: "http://localhost:1420/?page=control"
});
installDom(dom);
dom.window.confirm = () => true;

const operationsSnapshot = makeOperationsSnapshot();
const unknownSnapshot = makeUnknownResolutionSnapshot();
const genericRequests: OperationCommandRequest[] = [];
const genericGrants: StepUpGrantDraftRequest[] = [];
const unknownGrants: UnknownResolutionStepUpRequestV2[] = [];
const unknownRequests: UnknownResolutionRequestV2[] = [];
const unknownReviews: UnknownResolutionReviewV2[] = [];
const operationsApi: OperationsDataApi = {
  fetchSnapshot: async () => operationsSnapshot,
  issueStepUpGrant: async (input) => {
    genericGrants.push(input);
    throw new Error("generic operation API must not handle unknown resolution");
  },
  requestCommand: async (input) => {
    genericRequests.push(input);
    throw new Error("generic operation API must not handle unknown resolution");
  },
  reviewCommand: async () => {
    throw new Error("generic operation API must not handle unknown resolution");
  },
  actOnIncident: async (input) => ({
    schema_version: 1,
    incident_id: input.incident_id,
    status: input.action === "acknowledge" ? "acknowledged" : "resolved",
    action_id: input.action_id,
    acted_at: input.acted_at
  })
};
const unknownApi: UnknownResolutionDataApi = {
  fetchSnapshot: async () => unknownSnapshot,
  issueStepUpGrant: async (input) => {
    unknownGrants.push(input);
    return {
      schema_version: 2,
      step_up_grant_id: "56565656-5656-4656-8656-565656565656",
      command_hash: "f".repeat(64),
      step_up_grant_issued_at: "2099-07-14T00:00:00.000Z",
      step_up_grant_expires_at: "2099-07-14T00:05:00.000Z",
      step_up_grant_one_time: true,
      step_up_grant_consumed_at: null,
      bound_action: input.bound_action,
      bound_command_type: "close_unknown_execution"
    };
  },
  requestResolution: async (input) => {
    unknownRequests.push(input);
    return {
      schema_version: 2,
      command_id: input.request_id,
      break_id: input.break_id,
      intent_id: input.intent_id,
      state: "requested",
      receipt_revision: 0,
      break_revision: input.expected_break_revision + 1,
      request_digest_sha256: input.command_hash,
      review_digest_sha256: null,
      terminal_status: input.terminal_status,
      claim_token: null,
      work_revision: null,
      application_id: null,
      application_sha256: null,
      accounting_mutation_allowed: false,
      resolution_complete: false,
      inserted: true
    };
  },
  reviewResolution: async (input) => {
    unknownReviews.push(input);
    throw new Error("review path is not exercised in this request fixture");
  }
};
const generatedIds = [
  "57575757-5757-4757-8757-575757575757",
  "58585858-5858-4858-8858-585858585858"
];
let generatedIndex = 0;
const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false }, mutations: { retry: false } }
});
const container = dom.window.document.getElementById("root");
assert.ok(container);
const { fireEvent, render } = await import("@testing-library/react");
const rendered = render(
  <QueryClientProvider client={queryClient}>
    <OperationsPage
      dataApi={operationsApi}
      unknownDataApi={unknownApi}
      onlineOverride
      idFactory={() => generatedIds[generatedIndex++] ?? "59595959-5959-4959-8959-595959595959"}
      nowFactory={() => new Date("2099-07-14T00:00:00.000Z")}
    />
  </QueryClientProvider>,
  { container }
);

await waitFor(() => container.textContent?.includes("paper-order-42424242") === true);
assert.ok(container.querySelector('ol[aria-label="조정 요청·승인·Worker ACK·postcondition 타임라인"]'));
assert.match(container.textContent ?? "", /Worker ACK와 회계 postcondition 전에는 완료로 표시하지 않습니다/);
assert.match(container.textContent ?? "", /누락 체결이 없더라도 \[\]를 입력해야 하며 자동 기본값은 없습니다/);

const requestButton = buttonByText(dom, container, "AAL2 step-up 후 조정 요청");
assert.equal(requestButton.disabled, true, "empty evidence and missing-fill fields must never silently default");
fireEvent.change(inputByLabel(dom, container, "Evidence artifact URI"), {
  target: { value: `urn:sha256:${"d".repeat(64)}` }
});
fireEvent.change(inputByLabel(dom, container, "Evidence SHA-256"), {
  target: { value: "d".repeat(64) }
});
fireEvent.change(inputByLabel(dom, container, "Evidence captured at"), {
  target: { value: "2099-07-13T23:59:00.000Z" }
});
fireEvent.change(inputByLabel(dom, container, "확정 terminal 상태"), {
  target: { value: "canceled" }
});
fireEvent.change(inputByLabel(dom, container, "누락 체결 manifest (strict JSON array)"), {
  target: { value: "[]" }
});
assert.equal(requestButton.disabled, false, "explicit [] plus complete evidence enables the operator request");

await act(async () => requestButton.click());
await waitFor(() => unknownRequests.length === 1);
assert.equal(unknownGrants.length, 1);
assert.equal(unknownGrants[0].bound_action, "request");
assert.equal(unknownGrants[0].command_payload.schema_version, 2);
assert.equal("command_hash" in unknownGrants[0].command_payload, false);
assert.deepEqual(unknownRequests[0].missing_fills, []);
assert.equal(unknownRequests[0].expected_break_revision, unknownSnapshot.cases[0].break_revision);
assert.equal(genericGrants.length, 0, "unknown workflow must not use the generic command grant RPC");
assert.equal(genericRequests.length, 0, "unknown workflow must not use the generic command request RPC");
assert.equal(unknownReviews.length, 0);
await waitFor(() => container.textContent?.includes("독립 승인과 Worker ACK 전에는 회계 조정 완료가 아닙니다") === true);

await act(async () => {
  rendered.rerender(
    <QueryClientProvider client={queryClient}>
      <OperationsPage
        dataApi={operationsApi}
        unknownDataApi={unknownApi}
        onlineOverride={false}
        nowFactory={() => new Date("2099-07-14T00:00:00.000Z")}
      />
    </QueryClientProvider>
  );
});
await waitFor(() => buttonByText(dom, container, "AAL2 step-up 후 조정 요청").disabled === true);
assert.equal(unknownRequests.length, 1, "offline rerender must not queue or replay an unknown-resolution request");

await act(async () => rendered.unmount());
queryClient.clear();
dom.window.close();

console.log("unknown-resolution V2 page interaction and accessible evidence form passed");

function installDom(value: JSDOM): void {
  Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", { value: true, configurable: true, writable: true });
  Object.defineProperty(globalThis, "window", { value: value.window, configurable: true });
  Object.defineProperty(globalThis, "document", { value: value.window.document, configurable: true });
  Object.defineProperty(globalThis, "navigator", { value: value.window.navigator, configurable: true });
  Object.defineProperty(globalThis, "HTMLElement", { value: value.window.HTMLElement, configurable: true });
  Object.defineProperty(globalThis, "HTMLButtonElement", { value: value.window.HTMLButtonElement, configurable: true });
  Object.defineProperty(globalThis, "Event", { value: value.window.Event, configurable: true });
}

function buttonByText(value: JSDOM, root: HTMLElement, label: string): HTMLButtonElement {
  const button = Array.from(root.querySelectorAll("button")).find((candidate) =>
    candidate.textContent?.includes(label)
  );
  assert.ok(button instanceof value.window.HTMLButtonElement, `Button not found: ${label}`);
  return button;
}

function inputByLabel(value: JSDOM, root: HTMLElement, label: string): HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement {
  const labelElement = Array.from(root.querySelectorAll("label")).find((candidate) =>
    candidate.textContent?.includes(label)
  );
  assert.ok(labelElement, `Label not found: ${label}`);
  const input = labelElement.querySelector("input, select, textarea");
  assert.ok(
    input instanceof value.window.HTMLInputElement ||
      input instanceof value.window.HTMLSelectElement ||
      input instanceof value.window.HTMLTextAreaElement,
    `Input not found: ${label}`
  );
  return input;
}

async function waitFor(predicate: () => boolean): Promise<void> {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    if (predicate()) {
      return;
    }
    await act(async () => new Promise((resolve) => setTimeout(resolve, 0)));
  }
  assert.ok(predicate(), "Timed out waiting for unknown-resolution page state");
}
