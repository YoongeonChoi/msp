import assert from "node:assert/strict";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type { OperationCommandRequest, StepUpGrantDraftRequest } from "../src/lib/operationsContracts";
import type { OperationsDataApi } from "../src/lib/operationsData";
import { OperationsPage } from "../src/pages/OperationsPage";
import { makeOperationsSnapshot } from "./operationsFixture";

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  pretendToBeVisual: true,
  url: "http://localhost:1420/?page=control"
});
installDom(dom);
dom.window.confirm = () => true;

const snapshot = makeOperationsSnapshot();
const requested: OperationCommandRequest[] = [];
const issued: StepUpGrantDraftRequest[] = [];
const dataApi: OperationsDataApi = {
  fetchSnapshot: async () => snapshot,
  issueStepUpGrant: async (input) => {
    issued.push(input);
    const grant = snapshot.access.active_step_up_grants.find(
      (candidate) => candidate.bound_action === input.bound_action && candidate.bound_command_type === input.bound_command_type
    );
    assert.ok(grant);
    return { schema_version: 1, ...grant };
  },
  requestCommand: async (input) => {
    requested.push(input);
    return snapshot.commands[0];
  },
  reviewCommand: async () => snapshot.pending_reviews[0],
  actOnIncident: async (input) => ({
    schema_version: 1,
    incident_id: input.incident_id,
    status: input.action === "acknowledge" ? "acknowledged" : "resolved",
    action_id: input.action_id,
    acted_at: input.acted_at
  })
};
const generatedIds = [
  "21212121-2121-4121-8121-212121212121",
  "23232323-2323-4323-8323-232323232323"
];
let generatedIndex = 0;
const idFactory = () => generatedIds[generatedIndex++] ?? "24242424-2424-4424-8424-242424242424";
const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false }, mutations: { retry: false } }
});
const container = dom.window.document.getElementById("root");
assert.ok(container);
const { render } = await import("@testing-library/react");
const rendered = render(
  <QueryClientProvider client={queryClient}>
    <OperationsPage
      dataApi={dataApi}
      onlineOverride={false}
      idFactory={idFactory}
      nowFactory={() => new Date("2099-07-14T00:00:00.000Z")}
    />
  </QueryClientProvider>,
  { container }
);

await waitFor(() => container.textContent?.includes("안전 명령 센터") === true);
const offlinePause = buttonByText(dom, container, "PAPER 일시정지");
assert.equal(offlinePause.disabled, true);
await act(async () => offlinePause.click());
assert.equal(requested.length, 0, "offline click must not create a queued mutation");
assert.equal(issued.length, 0, "offline click must not issue a step-up grant");
assert.match(container.textContent ?? "", /오프라인 작업은 전송되지 않으며/);

await act(async () => {
  rendered.rerender(
    <QueryClientProvider client={queryClient}>
      <OperationsPage
        dataApi={dataApi}
        onlineOverride
        idFactory={idFactory}
        nowFactory={() => new Date("2099-07-14T00:00:00.000Z")}
      />
    </QueryClientProvider>
  );
});
await waitFor(() => buttonByText(dom, container, "PAPER 일시정지").disabled === false);
assert.equal(requested.length, 0, "reconnection must not replay an offline action");
assert.equal(issued.length, 0, "reconnection must not issue an offline step-up grant");

await act(async () => buttonByText(dom, container, "PAPER 일시정지").click());
await waitFor(() => requested.length === 1);
assert.equal(issued.length, 1);
assert.equal(issued[0].bound_action, "request");
assert.equal(issued[0].command_payload.command_type, "pause_paper");
assert.equal("command_hash" in issued[0].command_payload, false, "hash target draft excludes grant metadata");
assert.equal(requested[0].command_type, "pause_paper");
assert.equal(requested[0].environment, "paper");
assert.equal(requested[0].schema_version, 1);

await act(async () => rendered.unmount());
queryClient.clear();
dom.window.close();

console.log("operations hydrated mutation guards passed");

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

async function waitFor(predicate: () => boolean): Promise<void> {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (predicate()) {
      return;
    }
    await act(async () => new Promise((resolve) => setTimeout(resolve, 0)));
  }
  assert.ok(predicate(), "Timed out waiting for OperationsPage state");
}
