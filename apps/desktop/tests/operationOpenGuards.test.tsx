import assert from "node:assert/strict";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type { OperationsDataApi } from "../src/lib/operationsData";
import type { OperationsSnapshot } from "../src/lib/operationsContracts";
import { resetQueryCacheAfterSignOut } from "../src/lib/authSessionCache";
import { canPerformIncidentAction, OperationsPage } from "../src/pages/OperationsPage";
import { makeOperationsSnapshot } from "./operationsFixture";

const commandGuardScenarios: ReadonlyArray<{
  readonly label: string;
  readonly mutate: (snapshot: OperationsSnapshot) => void;
}> = [
  {
    label: "runtime state_version",
    mutate: (snapshot) => {
      snapshot.runtime_health.state_version += 1;
    }
  },
  {
    label: "assurance level",
    mutate: (snapshot) => {
      snapshot.access.assurance_level = "aal1";
      snapshot.access.active_step_up_grants = [];
    }
  },
  {
    label: "actor role",
    mutate: (snapshot) => {
      assert.ok(snapshot.access.actor);
      snapshot.access.actor.roles = ["auditor"];
    }
  }
];

for (const scenario of commandGuardScenarios) {
  await assertCommandDialogInvalidation(scenario.label, scenario.mutate);
}

await assertReviewRevisionInvalidation();
await assertSessionExpiryDuringGrantInvalidation();
assertIncidentRoleSeparation();

console.log("dialog-open safety guards and incident role separation passed");

async function assertCommandDialogInvalidation(
  label: string,
  mutate: (snapshot: OperationsSnapshot) => void
): Promise<void> {
  let latest = makeOperationsSnapshot();
  latest.runtime_health.execution_enabled = true;
  let grants = 0;
  let rpcCalls = 0;
  const dataApi = createDataApi(
    () => latest,
    () => {
      grants += 1;
    },
    () => {
      rpcCalls += 1;
    }
  );
  const harness = await renderOperationsPage(dataApi);

  await waitFor(() => harness.container.textContent?.includes("현재 실행 상태") === true);
  await act(async () => buttonByText(harness.dom, harness.container, "모의거래 일시정지").click());
  await waitFor(() => harness.container.querySelector("dialog[open]") !== null);

  const changed = structuredClone(latest);
  mutate(changed);
  latest = changed;

  await act(async () => buttonByText(harness.dom, harness.container, "요청 생성").click());
  await waitFor(() => harness.container.textContent?.includes("상태가 변경됨") === true);
  assert.equal(grants, 0, `${label} change after dialog open must not issue a grant`);
  assert.equal(rpcCalls, 0, `${label} change after dialog open must not call the command RPC`);

  await harness.dispose();
}

async function assertReviewRevisionInvalidation(): Promise<void> {
  let latest = makeOperationsSnapshot();
  let grants = 0;
  let rpcCalls = 0;
  const dataApi = createDataApi(
    () => latest,
    () => {
      grants += 1;
    },
    () => {
      rpcCalls += 1;
    }
  );
  const harness = await renderOperationsPage(dataApi);

  await waitFor(() => harness.container.textContent?.includes("현재 실행 상태") === true);
  await act(async () => buttonByText(harness.dom, harness.container, "승인 기록").click());
  await waitFor(() => harness.container.textContent?.includes("승인 대기함") === true);
  await act(async () => buttonByText(harness.dom, harness.container, "승인").click());
  await waitFor(() => buttonByText(harness.dom, harness.container, "승인 전송") !== null);

  const changed = structuredClone(latest);
  changed.pending_reviews[0].control_plane_receipt.revision += 1;
  latest = changed;

  await act(async () => buttonByText(harness.dom, harness.container, "승인 전송").click());
  await waitFor(() => harness.container.textContent?.includes("상태가 변경됨") === true);
  assert.equal(grants, 0, "control-plane revision change after dialog open must not issue a grant");
  assert.equal(rpcCalls, 0, "control-plane revision change after dialog open must not call the review RPC");

  await harness.dispose();
}

async function assertSessionExpiryDuringGrantInvalidation(): Promise<void> {
  const latest = makeOperationsSnapshot();
  latest.runtime_health.execution_enabled = true;
  const grant = latest.access.active_step_up_grants.find(
    (candidate) => candidate.bound_action === "request" && candidate.bound_command_type === "pause_paper"
  );
  assert.ok(grant);
  let releaseGrant: (() => void) | null = null;
  const grantGate = new Promise<void>((resolve) => {
    releaseGrant = resolve;
  });
  let grants = 0;
  let rpcCalls = 0;
  const dataApi: OperationsDataApi = {
    fetchSnapshot: async () => latest,
    issueStepUpGrant: async () => {
      grants += 1;
      await grantGate;
      return { schema_version: 1, ...grant };
    },
    requestCommand: async () => {
      rpcCalls += 1;
      return latest.commands[0];
    },
    reviewCommand: async () => latest.pending_reviews[0],
    actOnIncident: async (input) => ({
      schema_version: 1,
      incident_id: input.incident_id,
      status: input.action === "acknowledge" ? "acknowledged" : "resolved",
      action_id: input.action_id,
      acted_at: input.acted_at
    })
  };
  const harness = await renderOperationsPage(dataApi);

  await waitFor(() => harness.container.textContent?.includes("현재 실행 상태") === true);
  await act(async () => buttonByText(harness.dom, harness.container, "모의거래 일시정지").click());
  await waitFor(() => harness.container.querySelector("dialog[open]") !== null);
  await act(async () => buttonByText(harness.dom, harness.container, "요청 생성").click());
  await waitFor(() => grants === 1);

  resetQueryCacheAfterSignOut(harness.queryClient);
  assert.ok(releaseGrant);
  releaseGrant();
  await waitFor(() => harness.container.textContent?.includes("상태가 변경됨") === true);
  assert.equal(rpcCalls, 0, "session loss during grant issuance must prevent the command RPC");

  await harness.dispose();
}

function assertIncidentRoleSeparation(): void {
  const operatorSnapshot = makeOperationsSnapshot();
  const openIncident = operatorSnapshot.incidents[0];
  assert.equal(canPerformIncidentAction(operatorSnapshot, openIncident, "acknowledge"), true);

  const nonOperatorSnapshot = structuredClone(operatorSnapshot);
  assert.ok(nonOperatorSnapshot.access.actor);
  nonOperatorSnapshot.access.actor.roles = ["risk_approver"];
  assert.equal(
    canPerformIncidentAction(nonOperatorSnapshot, nonOperatorSnapshot.incidents[0], "acknowledge"),
    false,
    "incident acknowledgement is restricted to an operator"
  );

  const independentReviewerSnapshot = makeOperationsSnapshot();
  const acknowledgedIncident = independentReviewerSnapshot.incidents[0];
  acknowledgedIncident.status = "acknowledged";
  acknowledgedIncident.owner = {
    actor_id: "29292929-2929-4929-8929-292929292929",
    display_name: "사고 확인 담당자",
    roles: ["operator"]
  };
  assert.equal(
    canPerformIncidentAction(independentReviewerSnapshot, acknowledgedIncident, "resolve"),
    true,
    "a different risk approver may resolve an acknowledged incident"
  );

  const sameActorSnapshot = structuredClone(independentReviewerSnapshot);
  assert.ok(sameActorSnapshot.access.actor);
  assert.ok(sameActorSnapshot.incidents[0].owner);
  sameActorSnapshot.incidents[0].owner.actor_id = sameActorSnapshot.access.actor.actor_id;
  assert.equal(
    canPerformIncidentAction(sameActorSnapshot, sameActorSnapshot.incidents[0], "resolve"),
    false,
    "the acknowledgement actor cannot resolve the same incident"
  );

  const operatorOnlySnapshot = structuredClone(independentReviewerSnapshot);
  assert.ok(operatorOnlySnapshot.access.actor);
  operatorOnlySnapshot.access.actor.roles = ["operator"];
  assert.equal(
    canPerformIncidentAction(operatorOnlySnapshot, operatorOnlySnapshot.incidents[0], "resolve"),
    false,
    "incident resolution requires an independent risk approver"
  );
}

function createDataApi(
  snapshot: () => OperationsSnapshot,
  onGrant: () => void,
  onRpc: () => void
): OperationsDataApi {
  return {
    fetchSnapshot: async () => snapshot(),
    issueStepUpGrant: async () => {
      onGrant();
      throw new Error("grant must not be issued after a guarded state change");
    },
    requestCommand: async () => {
      onRpc();
      throw new Error("command RPC must not run after a guarded state change");
    },
    reviewCommand: async () => {
      onRpc();
      throw new Error("review RPC must not run after a guarded state change");
    },
    actOnIncident: async () => {
      onRpc();
      throw new Error("incident RPC is outside this fixture");
    }
  };
}

async function renderOperationsPage(dataApi: OperationsDataApi): Promise<{
  readonly dom: JSDOM;
  readonly container: HTMLElement;
  readonly queryClient: QueryClient;
  readonly dispose: () => Promise<void>;
}> {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    pretendToBeVisual: true,
    url: "http://localhost:1420/?page=control"
  });
  installDom(dom);
  installDialogShim(dom);
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
        onlineOverride
        nowFactory={() => new Date("2099-07-14T00:00:00.000Z")}
      />
    </QueryClientProvider>,
    { container }
  );
  return {
    dom,
    container,
    queryClient,
    dispose: async () => {
      await act(async () => rendered.unmount());
      queryClient.clear();
      dom.window.close();
    }
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
  const candidates = Array.from(root.querySelectorAll("button"));
  const button = candidates.find((candidate) => candidate.textContent?.trim() === label) ??
    candidates.find((candidate) => candidate.textContent?.includes(label));
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
  assert.ok(predicate(), "Timed out waiting for guarded OperationsPage state");
}
