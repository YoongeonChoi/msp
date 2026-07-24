import assert from "node:assert/strict";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { AppLayout } from "../src/components/Layout";
import type {
  DeviceConnectionGuardSnapshot,
  DeviceConnectionGuardStore
} from "../src/lib/authData";
import type { OperationCommandRequest, StepUpGrantDraftRequest } from "../src/lib/operationsContracts";
import {
  OperationsTransportError,
  operationsSnapshotQueryKey,
  type OperationsDataApi,
  type UnknownResolutionDataApi
} from "../src/lib/operationsData";
import {
  OperationsSnapshotProvider,
  type OperationsSnapshotContextValue,
  useOperationsSnapshot
} from "../src/lib/operationsSnapshotContext";
import { OperationsPage } from "../src/pages/OperationsPage";
import { SettingsPage } from "../src/pages/SettingsPage";
import { makeOperationsSnapshot } from "./operationsFixture";

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  pretendToBeVisual: true,
  url: "http://localhost:1420/?page=control"
});
installDom(dom);
installDialogShim(dom);

const snapshot = makeOperationsSnapshot();
snapshot.runtime_health.execution_enabled = true;
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

await waitFor(() => container.textContent?.includes("현재 실행 상태") === true);
const offlinePause = buttonByText(dom, container, "모의거래 일시정지");
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
await waitFor(() => buttonByText(dom, container, "모의거래 일시정지").disabled === false);
assert.equal(requested.length, 0, "reconnection must not replay an offline action");
assert.equal(issued.length, 0, "reconnection must not issue an offline step-up grant");

await act(async () => buttonByText(dom, container, "모의거래 일시정지").click());
await waitFor(() => container.querySelector("dialog[open]") !== null);
await act(async () => buttonByText(dom, container, "요청 생성").click());
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

const effectiveGuardQueryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
const effectiveGuardContainer = dom.window.document.createElement("div");
dom.window.document.body.append(effectiveGuardContainer);
const commandsBeforeEffectiveGuard = requested.length;
const grantsBeforeEffectiveGuard = issued.length;
let blockedUnknownFetchCalls = 0;
const blockedUnknownDataApi: UnknownResolutionDataApi = {
  fetchSnapshot: async () => {
    blockedUnknownFetchCalls += 1;
    throw new Error("effective guard must block unknown-resolution reads");
  },
  issueStepUpGrant: async () => {
    throw new Error("not used");
  },
  requestResolution: async () => {
    throw new Error("not used");
  },
  reviewResolution: async () => {
    throw new Error("not used");
  }
};
const effectiveBlockedSource = {
  dataApi,
  query: {
    data: snapshot,
    error: null,
    isLoading: false
  },
  snapshot: undefined,
  error: new Error("discarded device connection cleanup is incomplete"),
  isLoading: false,
  isFetching: false,
  isOnline: true,
  realtime: {
    connected: true,
    connectedAt: "2099-07-14T00:00:00.000Z",
    lastSignalAt: "2099-07-14T00:00:00.000Z"
  },
  updatedAt: Date.now(),
  refetchSnapshot: async () => {
    throw new Error("effective guard must block refetch actions");
  },
  invalidateSnapshot: async () => undefined
} as unknown as OperationsSnapshotContextValue;
const effectiveGuardShell = render(
  <QueryClientProvider client={effectiveGuardQueryClient}>
    <OperationsPage
      snapshotSource={effectiveBlockedSource}
      dataApi={dataApi}
      unknownDataApi={blockedUnknownDataApi}
    />
  </QueryClientProvider>,
  { container: effectiveGuardContainer }
);
await waitFor(() => effectiveGuardContainer.textContent?.includes("운영 데이터 확인 실패") === true);
assert.equal(
  Array.from(effectiveGuardContainer.querySelectorAll("button")).some((button) =>
    button.textContent?.includes("모의거래 일시정지")
  ),
  false,
  "effective cleanup error must hide command actions even when the raw query still has data"
);
assert.equal(requested.length, commandsBeforeEffectiveGuard, "effective cleanup error must keep command RPC at zero");
assert.equal(issued.length, grantsBeforeEffectiveGuard, "effective cleanup error must keep grant RPC at zero");
assert.equal(
  blockedUnknownFetchCalls,
  0,
  "effective cleanup error must keep auxiliary unknown-resolution reads at zero"
);
await act(async () => effectiveGuardShell.unmount());
effectiveGuardQueryClient.clear();
effectiveGuardContainer.remove();

const disabledProviderClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
const disabledProviderContainer = dom.window.document.createElement("div");
dom.window.document.body.append(disabledProviderContainer);
let disabledFetchCalls = 0;
let observedDisabledSource: OperationsSnapshotContextValue | null = null;
const disabledProviderApi: OperationsDataApi = {
  ...dataApi,
  fetchSnapshot: async () => {
    disabledFetchCalls += 1;
    return snapshot;
  }
};
const disabledProviderShell = render(
  <QueryClientProvider client={disabledProviderClient}>
    <OperationsSnapshotProvider
      dataApi={disabledProviderApi}
      enabled={false}
      realtimeOverride={null}
      pollIntervalMs={false}
    >
      <SnapshotSourceProbe onValue={(value) => { observedDisabledSource = value; }} />
    </OperationsSnapshotProvider>
  </QueryClientProvider>,
  { container: disabledProviderContainer }
);
await act(async () => Promise.resolve());
assert.equal(disabledFetchCalls, 0, "a disconnected device must not fetch the operations snapshot");
assert.equal(observedDisabledSource?.snapshot, undefined);
assert.equal(observedDisabledSource?.error, null);
await act(async () => {
  disabledProviderShell.rerender(
    <QueryClientProvider client={disabledProviderClient}>
      <OperationsSnapshotProvider
        dataApi={disabledProviderApi}
        enabled
        realtimeOverride={null}
        pollIntervalMs={false}
      >
        <SnapshotSourceProbe onValue={(value) => { observedDisabledSource = value; }} />
      </OperationsSnapshotProvider>
    </QueryClientProvider>
  );
});
await waitFor(() => disabledFetchCalls === 1 && observedDisabledSource?.snapshot === snapshot);
await act(async () => disabledProviderShell.unmount());
disabledProviderClient.clear();
disabledProviderContainer.remove();

const principalScopedClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
const principalScopedContainer = dom.window.document.createElement("div");
dom.window.document.body.append(principalScopedContainer);
const viewerSnapshot = deferred<OperationsSnapshot>();
let principalScopedFetchCalls = 0;
let observedPrincipalScopedSource: OperationsSnapshotContextValue | null = null;
const principalScopedApi: OperationsDataApi = {
  ...dataApi,
  fetchSnapshot: async () => {
    principalScopedFetchCalls += 1;
    return principalScopedFetchCalls === 1 ? snapshot : viewerSnapshot.promise;
  }
};
const principalScopedShell = render(
  <QueryClientProvider client={principalScopedClient}>
    <OperationsSnapshotProvider
      dataApi={principalScopedApi}
      principalId={null}
      realtimeOverride={null}
      pollIntervalMs={false}
    >
      <SnapshotSourceProbe onValue={(value) => { observedPrincipalScopedSource = value; }} />
    </OperationsSnapshotProvider>
  </QueryClientProvider>,
  { container: principalScopedContainer }
);
await act(async () => Promise.resolve());
assert.equal(principalScopedFetchCalls, 0, "a missing principal must keep snapshot reads disabled");
assert.equal(observedPrincipalScopedSource?.snapshot, undefined);
await act(async () => {
  principalScopedShell.rerender(
    <QueryClientProvider client={principalScopedClient}>
      <OperationsSnapshotProvider
        dataApi={principalScopedApi}
        principalId="auditor-user-id"
        realtimeOverride={null}
        pollIntervalMs={false}
      >
        <SnapshotSourceProbe onValue={(value) => { observedPrincipalScopedSource = value; }} />
      </OperationsSnapshotProvider>
    </QueryClientProvider>
  );
});
await waitFor(() => principalScopedFetchCalls === 1 && observedPrincipalScopedSource?.snapshot === snapshot);
await act(async () => {
  principalScopedShell.rerender(
    <QueryClientProvider client={principalScopedClient}>
      <OperationsSnapshotProvider
        dataApi={principalScopedApi}
        principalId="viewer-user-id"
        realtimeOverride={null}
        pollIntervalMs={false}
      >
        <SnapshotSourceProbe onValue={(value) => { observedPrincipalScopedSource = value; }} />
      </OperationsSnapshotProvider>
    </QueryClientProvider>
  );
});
await waitFor(() => principalScopedFetchCalls === 2);
assert.equal(
  observedPrincipalScopedSource?.snapshot,
  undefined,
  "a direct principal switch must not expose the previous principal's cached snapshot"
);
await act(async () => viewerSnapshot.resolve(snapshot));
await waitFor(() => observedPrincipalScopedSource?.snapshot === snapshot);
await act(async () => principalScopedShell.unmount());
principalScopedClient.clear();
principalScopedContainer.remove();

const guardedProviderClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
const guardedProviderContainer = dom.window.document.createElement("div");
dom.window.document.body.append(guardedProviderContainer);
let guardedFetchCalls = 0;
let observedGuardedSource: OperationsSnapshotContextValue | null = null;
let guardSnapshot: DeviceConnectionGuardSnapshot = {
  shouldDiscardAuthenticatedSession: false,
  cleanupError: null
};
const guardListeners = new Set<() => void>();
const guardStore: DeviceConnectionGuardStore = {
  subscribe: (listener) => {
    guardListeners.add(listener);
    return () => guardListeners.delete(listener);
  },
  getSnapshot: () => guardSnapshot
};
const guardedProviderApi: OperationsDataApi = {
  ...dataApi,
  fetchSnapshot: async () => {
    guardedFetchCalls += 1;
    return snapshot;
  }
};
const guardedProviderShell = render(
  <QueryClientProvider client={guardedProviderClient}>
    <OperationsSnapshotProvider
      dataApi={guardedProviderApi}
      guardStore={guardStore}
      realtimeOverride={null}
      pollIntervalMs={false}
    >
      <SnapshotSourceProbe onValue={(value) => { observedGuardedSource = value; }} />
    </OperationsSnapshotProvider>
  </QueryClientProvider>,
  { container: guardedProviderContainer }
);
await waitFor(() => guardedFetchCalls === 1 && observedGuardedSource?.snapshot === snapshot);
const syntheticCleanupError = new Error("synthetic storage cleanup failure");
await act(async () => {
  guardSnapshot = {
    shouldDiscardAuthenticatedSession: true,
    cleanupError: syntheticCleanupError
  };
  guardListeners.forEach((listener) => listener());
});
await waitFor(() => observedGuardedSource?.error === syntheticCleanupError);
assert.equal(observedGuardedSource?.snapshot, undefined);
assert.equal(observedGuardedSource?.realtime.connected, false);
assert.equal(guardedFetchCalls, 1, "guard transition must not restart authenticated snapshot reads");
await act(async () => guardedProviderShell.unmount());
guardedProviderClient.clear();
guardedProviderContainer.remove();

const failingQueryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
const failingContainer = dom.window.document.createElement("div");
dom.window.document.body.append(failingContainer);
const failingDataApi: OperationsDataApi = {
  ...dataApi,
  fetchSnapshot: async () => {
    throw new OperationsTransportError("api.get_desktop_operations_snapshot_v1", "backend-setup");
  }
};
const failingShell = render(
  <QueryClientProvider client={failingQueryClient}>
    <OperationsSnapshotProvider dataApi={failingDataApi} realtimeOverride={null} pollIntervalMs={false}>
      <AppLayout page="control" setPage={() => undefined} connectionState="connected">
        <div />
      </AppLayout>
    </OperationsSnapshotProvider>
  </QueryClientProvider>,
  { container: failingContainer }
);
await waitFor(() => failingContainer.textContent?.includes("운영 API 준비 상태 확인 필요") === true);
assert.match(failingContainer.textContent ?? "", /운영 API 준비 상태 확인 필요/);
assert.match(failingContainer.textContent ?? "", /운영 권한 확인 대기/);
assert.match(failingContainer.textContent ?? "", /운영 API 준비 상태를 확인해야 해 모든 운영 변경을 차단했습니다/);
assert.doesNotMatch(failingContainer.textContent ?? "", /로그인 확인 중/);
await act(async () => failingShell.unmount());
failingQueryClient.clear();
failingContainer.remove();

const staleQueryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
const staleContainer = dom.window.document.createElement("div");
dom.window.document.body.append(staleContainer);
let staleFetchCount = 0;
const staleDataApi: OperationsDataApi = {
  ...dataApi,
  fetchSnapshot: async () => {
    staleFetchCount += 1;
    if (staleFetchCount === 1) {
      return snapshot;
    }
    throw new OperationsTransportError("api.get_desktop_operations_snapshot_v1", "request");
  }
};
const staleShell = render(
  <QueryClientProvider client={staleQueryClient}>
    <OperationsSnapshotProvider dataApi={staleDataApi} realtimeOverride={null} pollIntervalMs={false}>
      <AppLayout page="control" setPage={() => undefined} connectionState="connected">
        <div />
      </AppLayout>
    </OperationsSnapshotProvider>
  </QueryClientProvider>,
  { container: staleContainer }
);
await waitFor(() => staleContainer.textContent?.includes("위험 승인자") === true);
assert.match(staleContainer.textContent ?? "", /2단계 인증/);
await act(async () => {
  await staleQueryClient.invalidateQueries({ queryKey: operationsSnapshotQueryKey, exact: true });
});
await waitFor(() => staleContainer.textContent?.includes("운영 데이터 확인 실패") === true);
const staleText = staleContainer.textContent ?? "";
assert.match(staleText, /운영 데이터 확인 실패/);
assert.match(staleText, /운영 권한 확인 대기/);
assert.doesNotMatch(staleText, /위험 승인자/);
await act(async () => staleShell.unmount());
staleQueryClient.clear();
staleContainer.remove();

const settingsQueryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
const settingsContainer = dom.window.document.createElement("div");
dom.window.document.body.append(settingsContainer);
const settingsShell = render(
  <QueryClientProvider client={settingsQueryClient}>
    <OperationsSnapshotProvider dataApi={dataApi} realtimeOverride={null} pollIntervalMs={false}>
      <SettingsPage />
    </OperationsSnapshotProvider>
  </QueryClientProvider>,
  { container: settingsContainer }
);
await waitFor(() => settingsContainer.textContent?.includes("운영 연결이 설정되지 않았습니다") === true);
const settingsText = settingsContainer.textContent ?? "";
assert.match(settingsText, /다시 빌드·설치/);
assert.match(settingsText, /설치 후 \.env\.local을 추가해도 반영되지 않습니다/);
assert.match(settingsText, /VITE_SUPABASE_URL/);
assert.match(settingsText, /VITE_SUPABASE_PUBLISHABLE_KEY/);
assert.match(settingsText, /연결 설정미설정/);
assert.doesNotMatch(settingsText, /로그인|비밀번호/);
assert.equal(settingsContainer.querySelector('input[type="password"]'), null);
await act(async () => settingsShell.unmount());
settingsQueryClient.clear();
settingsContainer.remove();
dom.window.close();

console.log("operations hydrated mutation guards passed");

function SnapshotSourceProbe({
  onValue
}: {
  readonly onValue: (value: OperationsSnapshotContextValue) => void;
}) {
  onValue(useOperationsSnapshot());
  return null;
}

function deferred<T>(): {
  readonly promise: Promise<T>;
  readonly resolve: (value: T) => void;
} {
  let resolvePromise: ((value: T) => void) | undefined;
  const promise = new Promise<T>((resolve) => {
    resolvePromise = resolve;
  });
  return {
    promise,
    resolve: (value) => {
      assert.ok(resolvePromise);
      resolvePromise(value);
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
