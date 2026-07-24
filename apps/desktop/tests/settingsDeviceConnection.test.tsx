import assert from "node:assert/strict";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type {
  AuthCredentials,
  AuthRoleState,
  DeviceConnectionGuardSnapshot,
  DeviceConnectionResult
} from "../src/lib/authData";
import type { OperationsDataApi } from "../src/lib/operationsData";
import { OperationsSnapshotProvider } from "../src/lib/operationsSnapshotContext";
import { SettingsPage, type SettingsAuthApi } from "../src/pages/SettingsPage";
import { makeOperationsSnapshot } from "./operationsFixture";

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  pretendToBeVisual: true,
  url: "http://localhost:1420/?page=settings"
});
installDom(dom);
installDialogShim(dom);

const snapshot = makeOperationsSnapshot();
const operationsApi: OperationsDataApi = {
  fetchSnapshot: async () => snapshot,
  issueStepUpGrant: async () => {
    throw new Error("not used");
  },
  requestCommand: async () => {
    throw new Error("not used");
  },
  reviewCommand: async () => {
    throw new Error("not used");
  },
  actOnIncident: async () => {
    throw new Error("not used");
  }
};

let roleState = signedOutRole();
let pendingConnection = deferred<DeviceConnectionResult>();
const connectionInputs: AuthCredentials[] = [];
let connectionInFlight = false;
let discardCalls = 0;
let disconnectCalls = 0;
let cleanupRetryCalls = 0;
let failNextDisconnect = true;
let cleanupError: Error | null = null;
const guardListeners = new Set<() => void>();
let guardSnapshot: DeviceConnectionGuardSnapshot = {
  shouldDiscardAuthenticatedSession: false,
  cleanupError: null
};
const authApi: SettingsAuthApi = {
  isReady: () => true,
  fetchRole: async () => roleState,
  connectDevice: async (input) => {
    connectionInputs.push(input);
    connectionInFlight = true;
    try {
      return await pendingConnection.promise;
    } finally {
      connectionInFlight = false;
    }
  },
  discardPendingConnection: () => {
    discardCalls += 1;
    setGuardState(connectionInFlight || cleanupError !== null, cleanupError);
  },
  getCleanupError: () => cleanupError,
  subscribeGuard: (listener) => {
    guardListeners.add(listener);
    return () => guardListeners.delete(listener);
  },
  getGuardSnapshot: () => guardSnapshot,
  retryCleanup: async () => {
    cleanupRetryCalls += 1;
    setGuardState(false, null);
  },
  disconnectDevice: async () => {
    disconnectCalls += 1;
    if (failNextDisconnect) {
      failNextDisconnect = false;
      setGuardState(true, new Error("synthetic persisted-session cleanup failure"));
      throw cleanupError;
    }
    setGuardState(false, null);
    roleState = signedOutRole();
  }
};

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false }, mutations: { retry: false } }
});
const container = dom.window.document.getElementById("root");
assert.ok(container);
const { fireEvent, render } = await import("@testing-library/react");
const rendered = renderSettings(container, queryClient, authApi);

await waitFor(() => container.textContent?.includes("이 기기는 아직 연결되지 않았습니다") === true);
await openConnectionDrawer(container);
setCredentials(container, "operator@example.test", "memory-only-password");
const firstForm = requiredElement(container.querySelector("form"), "connection form");
await act(async () => {
  fireEvent.submit(firstForm);
  fireEvent.submit(firstForm);
});
assert.equal(connectionInputs.length, 1, "same-tick double submit must call the auth API once");
assert.equal(
  requiredInput(container, 'input[type="password"]').value,
  "",
  "password state must be cleared as soon as the request starts"
);
assert.equal(
  queryClient.getMutationCache().getAll().length,
  0,
  "credentials must never enter the React Query mutation cache"
);

await act(async () => buttonByLabel(dom, container, "기기 연결 닫기").click());
await waitFor(() => container.textContent?.includes("입력 내용 삭제") === true);
await act(async () => buttonByText(dom, container, "입력 삭제하고 닫기").click());
await waitFor(() => container.querySelector('dialog[data-variant="drawer"]') === null);
assert.ok(discardCalls > 0, "closing a pending connection must invalidate the auth attempt");
assert.equal(buttonByText(dom, container, "이전 연결 요청 정리 중").disabled, true);

await act(async () => pendingConnection.resolve("connected"));
await waitFor(() => disconnectCalls === 1);
await waitFor(() => container.textContent?.includes("취소한 기기 연결 정리가 필요합니다") === true);
assert.equal(buttonByText(dom, container, "연결 정리 필요").disabled, true);
assert.equal(
  queryClient.getMutationCache().getAll().length,
  0,
  "late-success cleanup must purge volatile mutation state"
);
await act(async () => buttonByText(dom, container, "연결 정리 다시 시도").click());
await waitFor(() => cleanupRetryCalls === 1);
await waitFor(() => buttonByText(dom, container, "이 기기 연결").disabled === false);
await waitFor(() => dom.window.document.activeElement?.textContent?.trim() === "이 기기 연결");

pendingConnection = deferred<DeviceConnectionResult>();
await openConnectionDrawer(container);
setCredentials(container, "operator@example.test", "rejected-password");
await act(async () => fireEvent.submit(requiredElement(container.querySelector("form"), "connection form")));
await act(async () => pendingConnection.reject(new Error("연결 확인 실패")));
await waitFor(() => container.textContent?.includes("연결 확인 실패") === true);
assert.equal(requiredInput(container, 'input[type="password"]').value, "");
assert.equal(queryClient.getMutationCache().getAll().length, 0);
await act(async () => buttonByLabel(dom, container, "기기 연결 닫기").click());
await act(async () => buttonByText(dom, container, "입력 삭제하고 닫기").click());
await waitFor(() => container.querySelector('dialog[data-variant="drawer"]') === null);

pendingConnection = deferred<DeviceConnectionResult>();
await openConnectionDrawer(container);
setCredentials(container, "operator@example.test", "accepted-password");
await act(async () => fireEvent.submit(requiredElement(container.querySelector("form"), "connection form")));
roleState = signedInRole();
await act(async () => pendingConnection.resolve("connected"));
await waitFor(() => container.textContent?.includes("operator@example.test") === true);
await waitFor(() => dom.window.document.activeElement?.textContent?.includes("operator@example.test") === true);
assert.equal(container.querySelector('input[type="password"]'), null);
assert.equal(queryClient.getMutationCache().getAll().length, 0);

const connectionDetails = requiredElement(
  Array.from(container.querySelectorAll("details")).find((candidate) =>
    candidate.textContent?.includes("이 기기 연결 해제")
  ),
  "connection details"
);
connectionDetails.open = true;
await act(async () => buttonByText(dom, container, "이 기기 연결 해제").click());
await waitFor(() => container.textContent?.includes("이 기기 연결을 해제할까요?") === true);
await waitFor(() => dom.window.document.activeElement?.textContent?.includes("취소") === true);
await act(async () => buttonByExactText(dom, container, "연결 해제").click());
await waitFor(() => container.textContent?.includes("이 기기는 아직 연결되지 않았습니다") === true);
await waitFor(() => dom.window.document.activeElement?.textContent?.trim() === "이 기기 연결");
assert.equal(disconnectCalls, 2, "only explicit current-device disconnect follows late-success cleanup");
assert.equal(queryClient.getMutationCache().getAll().length, 0);

await act(async () => rendered.unmount());
queryClient.clear();

const cleanupBlockedClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
const cleanupBlockedContainer = dom.window.document.createElement("div");
dom.window.document.body.append(cleanupBlockedContainer);
roleState = signedInRole();
setGuardState(true, new Error("synthetic persisted-session cleanup failure"));
const unresolvedRole = deferred<AuthRoleState>();
const cleanupBlocked = renderSettings(cleanupBlockedContainer, cleanupBlockedClient, {
  ...authApi,
  fetchRole: async () => unresolvedRole.promise
});
await waitFor(() => cleanupBlockedContainer.textContent?.includes("취소한 기기 연결 정리가 필요합니다") === true);
assert.doesNotMatch(cleanupBlockedContainer.textContent ?? "", /TOTP 관리/);
assert.equal(buttonByText(dom, cleanupBlockedContainer, "연결 정리 필요").disabled, true);
await act(async () => cleanupBlocked.unmount());
cleanupBlockedClient.clear();
cleanupBlockedContainer.remove();
setGuardState(false, null);

const restoredClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
const restoredContainer = dom.window.document.createElement("div");
dom.window.document.body.append(restoredContainer);
roleState = signedInRole();
const connectionCountBeforeRestore = connectionInputs.length;
const restored = renderSettings(restoredContainer, restoredClient, authApi);
await waitFor(() => restoredContainer.textContent?.includes("operator@example.test") === true);
assert.equal(restoredContainer.querySelector('input[type="password"]'), null);
assert.equal(
  connectionInputs.length,
  connectionCountBeforeRestore,
  "a restored session must not ask for credentials again"
);
await act(async () => restored.unmount());
restoredClient.clear();
restoredContainer.remove();
dom.window.close();

console.log("settings device connection lifecycle passed");

function setGuardState(
  shouldDiscardAuthenticatedSession: boolean,
  error: Error | null
): void {
  cleanupError = error;
  guardSnapshot = { shouldDiscardAuthenticatedSession, cleanupError: error };
  guardListeners.forEach((listener) => listener());
}

function renderSettings(containerElement: HTMLElement, client: QueryClient, api: SettingsAuthApi) {
  return render(
    <QueryClientProvider client={client}>
      <OperationsSnapshotProvider dataApi={operationsApi} realtimeOverride={null} pollIntervalMs={false}>
        <SettingsPage authApi={api} />
      </OperationsSnapshotProvider>
    </QueryClientProvider>,
    { container: containerElement }
  );
}

async function openConnectionDrawer(root: HTMLElement): Promise<void> {
  await act(async () => buttonByText(dom, root, "이 기기 연결").click());
  await waitFor(() => root.querySelector('dialog[data-variant="drawer"]') !== null);
}

function setCredentials(root: HTMLElement, email: string, password: string): void {
  fireEvent.change(requiredInput(root, 'input[type="email"]'), { target: { value: email } });
  fireEvent.change(requiredInput(root, 'input[type="password"]'), { target: { value: password } });
}

function signedOutRole(): AuthRoleState {
  return { signedIn: false, email: null, role: null, roles: [], warning: null };
}

function signedInRole(): AuthRoleState {
  return {
    signedIn: true,
    email: "operator@example.test",
    role: null,
    roles: ["operator"],
    warning: null
  };
}

function requiredInput(root: HTMLElement, selector: string): HTMLInputElement {
  const input = root.querySelector(selector);
  assert.ok(input instanceof dom.window.HTMLInputElement, `Input not found: ${selector}`);
  return input;
}

function requiredElement<T>(value: T | null | undefined, label: string): T {
  assert.ok(value, `${label} not found`);
  return value;
}

function buttonByText(value: JSDOM, root: HTMLElement, label: string): HTMLButtonElement {
  const button = Array.from(root.querySelectorAll("button")).find((candidate) =>
    candidate.textContent?.includes(label)
  );
  assert.ok(button instanceof value.window.HTMLButtonElement, `Button not found: ${label}`);
  return button;
}

function buttonByLabel(value: JSDOM, root: HTMLElement, label: string): HTMLButtonElement {
  const button = root.querySelector(`button[aria-label="${label}"]`);
  assert.ok(button instanceof value.window.HTMLButtonElement, `Button not found: ${label}`);
  return button;
}

function buttonByExactText(value: JSDOM, root: HTMLElement, label: string): HTMLButtonElement {
  const button = Array.from(root.querySelectorAll("button")).find((candidate) =>
    candidate.textContent?.trim() === label
  );
  assert.ok(button instanceof value.window.HTMLButtonElement, `Button not found: ${label}`);
  return button;
}

function deferred<T>(): {
  readonly promise: Promise<T>;
  readonly resolve: (value: T) => void;
  readonly reject: (error: Error) => void;
} {
  let resolvePromise: ((value: T) => void) | undefined;
  let rejectPromise: ((error: Error) => void) | undefined;
  const promise = new Promise<T>((resolve, reject) => {
    resolvePromise = resolve;
    rejectPromise = reject;
  });
  return {
    promise,
    resolve: (value) => {
      assert.ok(resolvePromise);
      resolvePromise(value);
    },
    reject: (error) => {
      assert.ok(rejectPromise);
      rejectPromise(error);
    }
  };
}

async function waitFor(predicate: () => boolean): Promise<void> {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    if (predicate()) {
      return;
    }
    await act(async () => new Promise((resolve) => setTimeout(resolve, 0)));
  }
  assert.ok(predicate(), "Timed out waiting for SettingsPage state");
}

function installDom(value: JSDOM): void {
  Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", { value: true, configurable: true, writable: true });
  Object.defineProperty(globalThis, "window", { value: value.window, configurable: true });
  Object.defineProperty(globalThis, "document", { value: value.window.document, configurable: true });
  Object.defineProperty(globalThis, "navigator", { value: value.window.navigator, configurable: true });
  Object.defineProperty(globalThis, "HTMLElement", { value: value.window.HTMLElement, configurable: true });
  Object.defineProperty(globalThis, "HTMLButtonElement", { value: value.window.HTMLButtonElement, configurable: true });
  Object.defineProperty(globalThis, "HTMLInputElement", { value: value.window.HTMLInputElement, configurable: true });
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
