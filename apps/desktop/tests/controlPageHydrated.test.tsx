import assert from "node:assert/strict";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type { ControlPageDataApi } from "../src/pages/ControlPage";
import type { BotSettings, ManualCommandRow } from "../src/lib/rows";
import type { BotSettingsPatch, LiveEnableRequestInput, LiveEnableReviewInput } from "../src/lib/supabaseData";

interface HydratedView {
  readonly dom: JSDOM;
  readonly container: HTMLElement;
  readonly calls: {
    readonly updates: BotSettingsPatch[];
    readonly liveRequests: LiveEnableRequestInput[];
    readonly reviews: LiveEnableReviewInput[];
    readonly alerts: string[];
  };
  readonly cleanup: () => Promise<void>;
}

interface HydratedOptions {
  readonly fetchBotSettings?: () => Promise<BotSettings | null>;
  readonly initialConfirmText?: string;
  readonly queryTimeoutMs?: number;
  readonly requestLiveEnable?: (input: LiveEnableRequestInput) => Promise<void>;
  readonly reviewLiveEnableCommand?: (input: LiveEnableReviewInput) => Promise<void>;
  readonly updateBotSettings?: (patch: BotSettingsPatch) => Promise<void>;
  readonly waitForReady?: boolean;
}

function settings(overrides: Partial<BotSettings> = {}): BotSettings {
  return {
    id: "singleton",
    enabled: false,
    mode: "paper",
    liveOrderAllowed: false,
    maxOrderAmountKrw: 100000,
    maxDailyLossPct: 0.02,
    maxDailyOrderCount: 10,
    maxPositionPct: 0.1,
    maxSectorPct: 0.3,
    loopIntervalSec: 30,
    updatedAt: new Date().toISOString(),
    ...overrides
  };
}

function command(overrides: Partial<ManualCommandRow>): ManualCommandRow {
  const nowMs = Date.now();
  return {
    id: "cmd-1",
    commandType: "request_live_enable",
    status: "pending",
    payload: {
      provider_contract_version: "toss-openapi-1.1.5",
      risk_report_id: "risk-2026-06-28",
      release_version: "release-1"
    },
    requestedBy: "requester",
    reviewedBy: null,
    rejectionReason: null,
    expiresAt: new Date(nowMs + 30 * 60_000).toISOString(),
    reviewedAt: null,
    createdAt: new Date(nowMs - 60_000).toISOString(),
    appliedAt: null,
    ...overrides
  };
}

async function renderHydratedControlPage(
  rows: readonly ManualCommandRow[],
  currentUserId: string | null,
  options: HydratedOptions = {}
): Promise<HydratedView> {
  const dom = new JSDOM("<!doctype html><html><body><div id=\"root\"></div></body></html>", {
    pretendToBeVisual: true,
    url: "http://localhost:1420/?page=control"
  });
  const calls = {
    updates: [] as BotSettingsPatch[],
    liveRequests: [] as LiveEnableRequestInput[],
    reviews: [] as LiveEnableReviewInput[],
    alerts: [] as string[]
  };
  installDom(dom);
  dom.window.confirm = () => true;
  dom.window.alert = (message) => {
    calls.alerts.push(String(message));
  };
  const dataApi: ControlPageDataApi = {
    fetchBotSettings: options.fetchBotSettings ?? (async () => settings()),
    fetchLiveEnableCommands: async () => [...rows],
    fetchCurrentUserId: async () => currentUserId,
    updateBotSettings: async (patch) => {
      calls.updates.push(patch);
      if (options.updateBotSettings) {
        await options.updateBotSettings(patch);
      }
    },
    requestLiveEnable: async (input) => {
      calls.liveRequests.push(input);
      if (options.requestLiveEnable) {
        await options.requestLiveEnable(input);
      }
    },
    reviewLiveEnableCommand: async (input) => {
      calls.reviews.push(input);
      if (options.reviewLiveEnableCommand) {
        await options.reviewLiveEnableCommand(input);
      }
    }
  };
  const queryClient = new QueryClient({
    defaultOptions: {
      mutations: { retry: false },
      queries: { retry: false }
    }
  });
  const container = dom.window.document.getElementById("root");
  assert.ok(container);
  const { ControlPage } = await import("../src/pages/ControlPage");
  const { render } = await import("@testing-library/react");

  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <ControlPage
        dataApi={dataApi}
        initialConfirmText={options.initialConfirmText}
        queryTimeoutMs={options.queryTimeoutMs}
      />
    </QueryClientProvider>,
    { container }
  );
  if (options.waitForReady !== false) {
    await waitFor(() => container.textContent?.includes("Live 승인 게이트") === true);
  }

  return {
    dom,
    container,
    calls,
    cleanup: async () => {
      await act(async () => {
        rendered.unmount();
      });
      queryClient.clear();
      dom.window.close();
    }
  };
}

function installDom(dom: JSDOM): void {
  Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", { value: true, configurable: true, writable: true });
  Object.defineProperty(globalThis, "window", { value: dom.window, configurable: true });
  Object.defineProperty(globalThis, "document", { value: dom.window.document, configurable: true });
  Object.defineProperty(globalThis, "navigator", { value: dom.window.navigator, configurable: true });
  Object.defineProperty(globalThis, "HTMLElement", { value: dom.window.HTMLElement, configurable: true });
  Object.defineProperty(globalThis, "HTMLButtonElement", { value: dom.window.HTMLButtonElement, configurable: true });
  Object.defineProperty(globalThis, "HTMLInputElement", { value: dom.window.HTMLInputElement, configurable: true });
  Object.defineProperty(globalThis, "Event", { value: dom.window.Event, configurable: true });
  Object.defineProperty(globalThis, "InputEvent", { value: dom.window.InputEvent, configurable: true });
}

async function waitFor(predicate: () => boolean): Promise<void> {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (predicate()) {
      return;
    }
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
  assert.ok(predicate(), "Timed out waiting for hydrated ControlPage state");
}

async function clickButton(dom: JSDOM, container: HTMLElement, label: string): Promise<void> {
  const button = buttonByText(dom, container, label);
  await act(async () => {
    button.click();
  });
}

async function setInputByLabel(
  dom: JSDOM,
  container: HTMLElement,
  label: string,
  value: string
): Promise<void> {
  const input = inputByLabel(dom, container, label);
  const { fireEvent } = await import("@testing-library/react");
  await act(async () => {
    fireEvent.change(input, { target: { value } });
  });
}

function buttonByText(dom: JSDOM, container: HTMLElement, label: string): HTMLButtonElement {
  const buttons = Array.from(container.querySelectorAll("button"));
  const button =
    buttons.find((candidate) => candidate.textContent?.trim() === label) ??
    buttons.find((candidate) => candidate.textContent?.includes(label));
  assert.ok(button instanceof dom.window.HTMLButtonElement, `Button not found: ${label}`);
  return button;
}

function inputByLabel(dom: JSDOM, container: HTMLElement, label: string): HTMLInputElement {
  const labels = Array.from(container.querySelectorAll("label"));
  const matchedLabel = labels.find((candidate) => candidate.textContent?.includes(label));
  const input = matchedLabel?.querySelector("input");
  assert.ok(input instanceof dom.window.HTMLInputElement, `Input not found: ${label}`);
  return input;
}

const requestView = await renderHydratedControlPage([], "requester");
try {
  await setInputByLabel(requestView.dom, requestView.container, "risk report id", "risk-2026-06-28");
  await setInputByLabel(requestView.dom, requestView.container, "release version", "release-2");
  await setInputByLabel(requestView.dom, requestView.container, "만료 분", "45");
  await clickButton(requestView.dom, requestView.container, "Live 승인 요청");
  await waitFor(() => requestView.calls.liveRequests.length === 1);
  assert.deepEqual(requestView.calls.liveRequests[0], {
    providerContractVersion: "toss-openapi-1.1.5",
    riskReportId: "risk-2026-06-28",
    releaseVersion: "release-2",
    expiresInMinutes: 45
  });
  assert.deepEqual(requestView.calls.alerts, []);
} finally {
  await requestView.cleanup();
}

const timeoutView = await renderHydratedControlPage([], "requester", {
  fetchBotSettings: () => new Promise<BotSettings | null>(() => undefined),
  queryTimeoutMs: 1,
  waitForReady: false
});
try {
  await waitFor(() => timeoutView.container.textContent?.includes("bot_settings를 읽지 못했습니다.") === true);
  assert.doesNotMatch(timeoutView.container.textContent ?? "", /bot_settings를 불러오는 중/);
} finally {
  await timeoutView.cleanup();
}

const invalidRequestView = await renderHydratedControlPage([], "requester");
try {
  await setInputByLabel(invalidRequestView.dom, invalidRequestView.container, "risk report id", "risk-2026-06-28");
  await setInputByLabel(invalidRequestView.dom, invalidRequestView.container, "release version", "");
  await clickButton(invalidRequestView.dom, invalidRequestView.container, "Live 승인 요청");
  assert.equal(invalidRequestView.calls.liveRequests.length, 0);
  assert.deepEqual(invalidRequestView.calls.alerts, ["Live 승인 요청 값을 확인하세요."]);

  invalidRequestView.calls.alerts.length = 0;
  await setInputByLabel(invalidRequestView.dom, invalidRequestView.container, "release version", "release-2");
  await setInputByLabel(invalidRequestView.dom, invalidRequestView.container, "만료 분", "5");
  await clickButton(invalidRequestView.dom, invalidRequestView.container, "Live 승인 요청");
  assert.equal(invalidRequestView.calls.liveRequests.length, 0);
  assert.deepEqual(invalidRequestView.calls.alerts, ["Live 승인 요청 값을 확인하세요."]);

  invalidRequestView.calls.alerts.length = 0;
  await setInputByLabel(invalidRequestView.dom, invalidRequestView.container, "만료 분", "241");
  await clickButton(invalidRequestView.dom, invalidRequestView.container, "Live 승인 요청");
  assert.equal(invalidRequestView.calls.liveRequests.length, 0);
  assert.deepEqual(invalidRequestView.calls.alerts, ["Live 승인 요청 값을 확인하세요."]);
} finally {
  await invalidRequestView.cleanup();
}

const failedRequestView = await renderHydratedControlPage([], "requester", {
  requestLiveEnable: async () => {
    throw new Error("mock_insert_failed");
  }
});
try {
  await setInputByLabel(failedRequestView.dom, failedRequestView.container, "risk report id", "risk-2026-06-28");
  await setInputByLabel(failedRequestView.dom, failedRequestView.container, "release version", "release-2");
  await clickButton(failedRequestView.dom, failedRequestView.container, "Live 승인 요청");
  await waitFor(() => failedRequestView.container.textContent?.includes("Live 승인 요청 저장에 실패했습니다.") === true);
} finally {
  await failedRequestView.cleanup();
}

const reviewView = await renderHydratedControlPage([command({ requestedBy: "requester" })], "reviewer");
try {
  await clickButton(reviewView.dom, reviewView.container, "승인");
  await waitFor(() => reviewView.calls.reviews.length === 1);
  await clickButton(reviewView.dom, reviewView.container, "거절");
  await waitFor(() => reviewView.calls.reviews.length === 2);
  assert.deepEqual(reviewView.calls.reviews, [
    { id: "cmd-1", status: "accepted" },
    { id: "cmd-1", status: "rejected", rejectionReason: "operator_rejected" }
  ]);
} finally {
  await reviewView.cleanup();
}

const failedReviewView = await renderHydratedControlPage([command({ requestedBy: "requester" })], "reviewer", {
  reviewLiveEnableCommand: async () => {
    throw new Error("mock_review_failed");
  }
});
try {
  await clickButton(failedReviewView.dom, failedReviewView.container, "승인");
  await waitFor(() => failedReviewView.container.textContent?.includes("Live 승인 검토 저장에 실패했습니다.") === true);
} finally {
  await failedReviewView.cleanup();
}

const selfReviewView = await renderHydratedControlPage([command({ requestedBy: "requester" })], "requester");
try {
  const acceptButton = buttonByText(selfReviewView.dom, selfReviewView.container, "승인");
  assert.equal(acceptButton.disabled, true);
  assert.match(selfReviewView.container.textContent ?? "", /본인 요청은 다른 admin 승인 필요/);
} finally {
  await selfReviewView.cleanup();
}

const activationView = await renderHydratedControlPage(
  [
    command({
      status: "accepted",
      reviewedBy: "reviewer",
      reviewedAt: new Date().toISOString()
    })
  ],
  "requester",
  { initialConfirmText: "실주문 위험을 이해했습니다" }
);
try {
  const activationButton = buttonByText(activationView.dom, activationView.container, "실주문 허용 활성화");
  assert.equal(activationButton.disabled, false);
  await clickButton(activationView.dom, activationView.container, "실주문 허용 활성화");
  await waitFor(() => activationView.calls.updates.length === 1);
  assert.deepEqual(activationView.calls.updates[0], {
    enabled: true,
    mode: "live",
    liveOrderAllowed: true
  });
} finally {
  await activationView.cleanup();
}

const failedActivationView = await renderHydratedControlPage(
  [
    command({
      status: "accepted",
      reviewedBy: "reviewer",
      reviewedAt: new Date().toISOString()
    })
  ],
  "requester",
  {
    initialConfirmText: "실주문 위험을 이해했습니다",
    updateBotSettings: async () => {
      throw new Error("mock_update_failed");
    }
  }
);
try {
  await clickButton(failedActivationView.dom, failedActivationView.container, "실주문 허용 활성화");
  await waitFor(() => failedActivationView.container.textContent?.includes("bot_settings 변경에 실패했습니다.") === true);
} finally {
  await failedActivationView.cleanup();
}

console.log("controlPage hydrated mutation fixtures passed");
