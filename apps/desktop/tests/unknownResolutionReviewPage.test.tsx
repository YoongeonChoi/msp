import assert from "node:assert/strict";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type {
  UnknownResolutionReviewV2,
  UnknownResolutionStepUpRequestV2
} from "../src/lib/operationsContracts";
import type {
  OperationsDataApi,
  UnknownResolutionDataApi
} from "../src/lib/operationsData";
import { OperationsPage } from "../src/pages/OperationsPage";
import { makeOperationsSnapshot } from "./operationsFixture";
import { makeRequestedUnknownResolutionSnapshot } from "./unknownResolutionFixture";

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  pretendToBeVisual: true,
  url: "http://localhost:1420/?page=control"
});
installDom(dom);
installDialogShim(dom);

const operationsSnapshot = makeOperationsSnapshot();
const unknownSnapshot = makeRequestedUnknownResolutionSnapshot();
const genericCalls: string[] = [];
const unknownGrants: UnknownResolutionStepUpRequestV2[] = [];
const reviews: UnknownResolutionReviewV2[] = [];
const operationsApi: OperationsDataApi = {
  fetchSnapshot: async () => operationsSnapshot,
  issueStepUpGrant: async () => {
    genericCalls.push("grant");
    throw new Error("generic grant forbidden");
  },
  requestCommand: async () => {
    genericCalls.push("request");
    throw new Error("generic request forbidden");
  },
  reviewCommand: async () => {
    genericCalls.push("review");
    throw new Error("generic review forbidden");
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
      step_up_grant_id: "60606060-6060-4060-8060-606060606060",
      command_hash: "2".repeat(64),
      step_up_grant_issued_at: "2099-07-14T00:00:00.000Z",
      step_up_grant_expires_at: "2099-07-14T00:05:00.000Z",
      step_up_grant_one_time: true,
      step_up_grant_consumed_at: null,
      bound_action: input.bound_action,
      bound_command_type: "close_unknown_execution"
    };
  },
  requestResolution: async () => {
    throw new Error("request path not used");
  },
  reviewResolution: async (input) => {
    reviews.push(input);
    const request = unknownSnapshot.cases[0].request;
    assert.ok(request);
    return {
      schema_version: 2,
      command_id: input.command_id,
      break_id: unknownSnapshot.cases[0].break_id,
      intent_id: unknownSnapshot.cases[0].intent_id,
      state: "rejected",
      receipt_revision: input.expected_receipt_revision + 1,
      break_revision: input.expected_break_revision + 1,
      request_digest_sha256: request.request_digest_sha256,
      review_digest_sha256: input.command_hash,
      terminal_status: request.terminal_status,
      claim_token: null,
      work_revision: null,
      application_id: null,
      application_sha256: null,
      accounting_mutation_allowed: false,
      resolution_complete: false,
      inserted: true
    };
  }
};
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
const container = dom.window.document.getElementById("root");
assert.ok(container);
const { render } = await import("@testing-library/react");
const rendered = render(
  <QueryClientProvider client={queryClient}>
    <OperationsPage
      dataApi={operationsApi}
      unknownDataApi={unknownApi}
      onlineOverride
      idFactory={() => "61616161-6161-4161-8161-616161616161"}
      nowFactory={() => new Date("2099-07-14T00:00:00.000Z")}
    />
  </QueryClientProvider>,
  { container }
);

await waitFor(() => container.textContent?.includes("지금 확인할 항목") === true);
await act(async () => buttonByText(dom, container, "독립 검토").click());
await waitFor(() => container.textContent?.includes("위험 승인자 독립 검토") === true);
assert.match(container.textContent ?? "", /요청 요약 해시, 증거 SHA, 최종 주문 상태와 모든 누락 체결을 검토/);
await act(async () => buttonByText(dom, container, "증거 거절").click());
await waitFor(() => container.querySelector("dialog[open]") !== null);
await act(async () => buttonByText(dom, container, "거절 전송").click());
await waitFor(() => reviews.length === 1);
assert.equal(unknownGrants.length, 1);
assert.equal(unknownGrants[0].bound_action, "review");
assert.equal(reviews[0].decision, "reject");
assert.equal(reviews[0].reason_code, "evidence_incomplete");
assert.equal(reviews[0].expected_receipt_revision, unknownSnapshot.cases[0].request?.receipt_revision);
assert.equal(reviews[0].expected_break_revision, unknownSnapshot.cases[0].break_revision);
assert.deepEqual(genericCalls, [], "review must not cross the generic command API");
await waitFor(() => container.textContent?.includes("Worker 적용과 최신 회계 반영을 계속 확인하세요") === true);

await act(async () => rendered.unmount());
queryClient.clear();
dom.window.close();

console.log("unknown-resolution V2 risk-approver reject flow passed");

function installDom(value: JSDOM): void {
  Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", { value: true, configurable: true, writable: true });
  Object.defineProperty(globalThis, "window", { value: value.window, configurable: true });
  Object.defineProperty(globalThis, "document", { value: value.window.document, configurable: true });
  Object.defineProperty(globalThis, "navigator", { value: value.window.navigator, configurable: true });
  Object.defineProperty(globalThis, "HTMLElement", { value: value.window.HTMLElement, configurable: true });
  Object.defineProperty(globalThis, "HTMLButtonElement", { value: value.window.HTMLButtonElement, configurable: true });
  Object.defineProperty(globalThis, "HTMLDialogElement", { value: value.window.HTMLDialogElement, configurable: true });
  Object.defineProperty(globalThis, "Event", { value: value.window.Event, configurable: true });
}

function installDialogShim(value: JSDOM): void {
  Object.defineProperty(value.window.HTMLDialogElement.prototype, "showModal", {
    configurable: true,
    value(this: HTMLDialogElement) { this.setAttribute("open", ""); }
  });
  Object.defineProperty(value.window.HTMLDialogElement.prototype, "close", {
    configurable: true,
    value(this: HTMLDialogElement) { this.removeAttribute("open"); }
  });
}

function buttonByText(value: JSDOM, root: HTMLElement, label: string): HTMLButtonElement {
  const button = Array.from(root.querySelectorAll("button")).find((candidate) => candidate.textContent?.includes(label));
  assert.ok(button instanceof value.window.HTMLButtonElement, `Button not found: ${label}`);
  return button;
}

async function waitFor(predicate: () => boolean): Promise<void> {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    if (predicate()) {
      return;
    }
    await act(async () => new Promise((resolve) => setTimeout(resolve, 0)));
  }
  assert.ok(predicate(), "Timed out waiting for unknown-resolution review state");
}
