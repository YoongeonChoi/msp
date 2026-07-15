import assert from "node:assert/strict";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { AccessChangePanel } from "../src/components/operations/AccessChangePanel";
import type { AccessChangeDataApi, OperationsDataApi } from "../src/lib/operationsData";
import type { AccessChangeReceipt } from "../src/lib/operationsContracts";
import { makeOperationsSnapshot } from "./operationsFixture";

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  pretendToBeVisual: true,
  url: "http://localhost:1420/?page=settings"
});
installDom(dom);
installDialogShim(dom);

const now = new Date("2099-07-14T00:00:00.000Z");
const actorId = "abcdefab-cdef-4abc-8def-abcdefabcdef";
const snapshot = makeOperationsSnapshot();
assert.ok(snapshot.access.actor);
snapshot.access.actor.actor_id = actorId;
snapshot.access.actor.roles = ["platform_admin"];
snapshot.access_changes = [
  makeReceipt({
    requestId: "81818181-8181-4181-8181-818181818181",
    subjectId: "82828282-8282-4282-8282-828282828282",
    requesterId: actorId.toUpperCase()
  }),
  makeReceipt({
    requestId: "83838383-8383-4383-8383-838383838383",
    subjectId: actorId.toUpperCase(),
    requesterId: "84848484-8484-4484-8484-848484848484"
  })
];

let grantRpcCalls = 0;
let requestRpcCalls = 0;
let reviewRpcCalls = 0;
const dataApi: AccessChangeDataApi = {
  issueStepUpGrant: async () => {
    grantRpcCalls += 1;
    throw new Error("step-up grant RPC must remain unreachable");
  },
  requestChange: async () => {
    requestRpcCalls += 1;
    throw new Error("access request RPC must remain unreachable");
  },
  reviewChange: async () => {
    reviewRpcCalls += 1;
    throw new Error("access review RPC must remain unreachable");
  }
};
const snapshotApi: OperationsDataApi = {
  fetchSnapshot: async () => snapshot,
  issueStepUpGrant: async () => { throw new Error("not used"); },
  requestCommand: async () => { throw new Error("not used"); },
  reviewCommand: async () => { throw new Error("not used"); },
  actOnIncident: async () => { throw new Error("not used"); }
};
const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false }, mutations: { retry: false } }
});
const container = dom.window.document.getElementById("root");
assert.ok(container);
const { fireEvent, render } = await import("@testing-library/react");
const rendered = render(
  <QueryClientProvider client={queryClient}>
    <AccessChangePanel
      dataApi={dataApi}
      snapshotApi={snapshotApi}
      onlineOverride
      nowFactory={() => now}
    />
  </QueryClientProvider>,
  { container }
);

await waitFor(() => container.textContent?.includes("접근권한 변경") === true);
const inputs = container.querySelectorAll("input");
assert.equal(inputs.length, 2);
await act(async () => {
  fireEvent.change(inputs[0], { target: { value: actorId.toUpperCase() } });
  fireEvent.change(inputs[1], { target: { value: "85858585-8585-4585-8585-858585858585" } });
});

const requestButton = buttonByText(dom, container, "변경 요청");
assert.equal(requestButton.disabled, true, "case-only UUID differences must block self-change requests in the UI");
assert.match(container.textContent ?? "", /본인의 역할은 변경 요청할 수 없습니다/);
await act(async () => requestButton.click());

const reviewButtons = Array.from(container.querySelectorAll("button")).filter((button) =>
  button.textContent?.includes("승인")
);
assert.equal(reviewButtons.length, 2);
for (const button of reviewButtons) {
  assert.equal(button.disabled, true, "maker and subject UUID casing must not bypass reviewer separation");
  await act(async () => button.click());
}

assert.equal(grantRpcCalls, 0, "role-separation mismatches must issue zero step-up grant RPCs");
assert.equal(requestRpcCalls, 0, "role-separation mismatches must issue zero access request RPCs");
assert.equal(reviewRpcCalls, 0, "role-separation mismatches must issue zero access review RPCs");

await act(async () => rendered.unmount());
queryClient.clear();
dom.window.close();

console.log("access change UUID identity guards keep grant and mutation RPC counts at zero");

function makeReceipt({
  requestId,
  subjectId,
  requesterId
}: {
  readonly requestId: string;
  readonly subjectId: string;
  readonly requesterId: string;
}): AccessChangeReceipt {
  return {
    schema_version: 1,
    request_id: requestId,
    subject_user_id: subjectId,
    requested_role: "viewer",
    change_type: "grant",
    state: "requested",
    requested_by: {
      actor_id: requesterId,
      display_name: "권한 요청자",
      roles: ["platform_admin"]
    },
    reviewed_by: null,
    requested_at: "2099-07-13T23:00:00.000Z",
    reviewed_at: null,
    applied_at: null,
    expires_at: "2099-07-15T00:00:00.000Z",
    reason_code: "role_required"
  };
}

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
    value(this: HTMLDialogElement) {
      this.setAttribute("open", "");
    }
  });
  Object.defineProperty(value.window.HTMLDialogElement.prototype, "close", {
    configurable: true,
    value(this: HTMLDialogElement) {
      this.removeAttribute("open");
    }
  });
}

function buttonByText(value: JSDOM, root: HTMLElement, label: string): HTMLButtonElement {
  const button = Array.from(root.querySelectorAll("button")).find((candidate) =>
    candidate.textContent?.includes(label)
  );
  assert.ok(button instanceof value.window.HTMLButtonElement, `Button not found: ${label}`);
  return button;
}

async function waitFor(predicate: () => boolean): Promise<void> {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (predicate()) {
      return;
    }
    await act(async () => new Promise((resolve) => setTimeout(resolve, 0)));
  }
  assert.ok(predicate(), "Timed out waiting for AccessChangePanel state");
}
