import assert from "node:assert/strict";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { MfaSecurityPanel } from "../src/components/operations/MfaSecurityPanel";
import type { MfaDataApi, MfaStatus } from "../src/lib/mfa";

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  pretendToBeVisual: true,
  url: "http://localhost:1420/?page=settings"
});
installDom(dom);

let status: MfaStatus = {
  signedIn: true,
  currentLevel: "aal1",
  nextLevel: "aal2",
  verifiedTotpFactors: [
    {
      id: "factor-verified",
      friendlyName: "운영 인증 앱",
      status: "verified",
      createdAt: "2026-07-14T00:00:00.000Z"
    }
  ],
  unverifiedTotpFactors: []
};
const verified: Array<{ readonly factorId: string; readonly code: string }> = [];
const dataApi: MfaDataApi = {
  fetchStatus: async () => status,
  enrollTotp: async () => ({
    factorId: "factor-new",
    qrCodeDataUrl: "data:image/svg+xml;utf-8,%3Csvg%3Eqr%3C/svg%3E"
  }),
  verifyTotp: async (input) => {
    verified.push(input);
    status = { ...status, currentLevel: "aal2", nextLevel: "aal2" };
  }
};

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
const container = dom.window.document.getElementById("root");
assert.ok(container);
const { fireEvent, render } = await import("@testing-library/react");
const rendered = render(
  <QueryClientProvider client={queryClient}>
    <MfaSecurityPanel dataApi={dataApi} />
  </QueryClientProvider>,
  { container }
);

await waitFor(() => container.textContent?.includes("현재 인증") === true);
assert.match(container.textContent ?? "", /기본 인증/);
assert.match(container.textContent ?? "", /2단계 인증/);
assert.doesNotMatch(container.textContent ?? "", /AAL[12]/);
assert.match(container.textContent ?? "", /운영 변경 차단/);
const codeInput = container.querySelector('[aria-label="TOTP 6자리 코드"]');
assert.ok(codeInput instanceof dom.window.HTMLInputElement);
fireEvent.change(codeInput, { target: { value: "123456" } });
const verifyButton = buttonByText(dom, container, "TOTP로 2단계 인증");
await act(async () => verifyButton.click());
await waitFor(() => verified.length === 1);
assert.deepEqual(verified[0], { factorId: "factor-verified", code: "123456" });
await waitFor(() => container.textContent?.includes("2단계 인증 확인") === true);

await act(async () => rendered.unmount());
queryClient.clear();

let enrollmentStatus: MfaStatus = {
  signedIn: true,
  currentLevel: "aal1",
  nextLevel: "aal1",
  verifiedTotpFactors: [],
  unverifiedTotpFactors: []
};
const enrollmentVerifications: Array<{ readonly factorId: string; readonly code: string }> = [];
const enrollmentApi: MfaDataApi = {
  fetchStatus: async () => enrollmentStatus,
  enrollTotp: async () => ({
    factorId: "factor-new",
    qrCodeDataUrl: "data:image/svg+xml;utf-8,%3Csvg%3Eenrollment-qr%3C/svg%3E"
  }),
  verifyTotp: async (input) => {
    enrollmentVerifications.push(input);
    enrollmentStatus = {
      signedIn: true,
      currentLevel: "aal2",
      nextLevel: "aal2",
      verifiedTotpFactors: [
        {
          id: "factor-new",
          friendlyName: "KR Trading Lab Desktop",
          status: "verified",
          createdAt: "2026-07-14T00:00:00.000Z"
        }
      ],
      unverifiedTotpFactors: []
    };
  }
};
const enrollmentClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
const enrollmentContainer = dom.window.document.createElement("div");
dom.window.document.body.append(enrollmentContainer);
const enrollmentRender = render(
  <QueryClientProvider client={enrollmentClient}>
    <MfaSecurityPanel dataApi={enrollmentApi} />
  </QueryClientProvider>,
  { container: enrollmentContainer }
);
await waitFor(() => enrollmentContainer.textContent?.includes("TOTP 등록 시작") === true);
await act(async () => buttonByText(dom, enrollmentContainer, "TOTP 등록 시작").click());
await waitFor(() => enrollmentContainer.querySelector('[alt="TOTP 인증 앱 등록 QR"]') !== null);
assert.doesNotMatch(enrollmentContainer.textContent ?? "", /secret=|otpauth:/i, "TOTP seed must not render as text");
const enrollmentCode = enrollmentContainer.querySelector('[aria-label="TOTP 6자리 코드"]');
assert.ok(enrollmentCode instanceof dom.window.HTMLInputElement);
fireEvent.change(enrollmentCode, { target: { value: "654321" } });
await act(async () => buttonByText(dom, enrollmentContainer, "등록 및 2단계 인증").click());
await waitFor(() => enrollmentVerifications.length === 1);
assert.deepEqual(enrollmentVerifications[0], { factorId: "factor-new", code: "654321" });
await act(async () => enrollmentRender.unmount());
enrollmentClient.clear();
dom.window.close();

console.log("TOTP MFA component verification flow passed");

function installDom(value: JSDOM): void {
  Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", { value: true, configurable: true, writable: true });
  Object.defineProperty(globalThis, "window", { value: value.window, configurable: true });
  Object.defineProperty(globalThis, "document", { value: value.window.document, configurable: true });
  Object.defineProperty(globalThis, "navigator", { value: value.window.navigator, configurable: true });
  Object.defineProperty(globalThis, "HTMLElement", { value: value.window.HTMLElement, configurable: true });
  Object.defineProperty(globalThis, "HTMLInputElement", { value: value.window.HTMLInputElement, configurable: true });
  Object.defineProperty(globalThis, "HTMLButtonElement", { value: value.window.HTMLButtonElement, configurable: true });
  Object.defineProperty(globalThis, "Event", { value: value.window.Event, configurable: true });
}

function buttonByText(value: JSDOM, root: HTMLElement, label: string): HTMLButtonElement {
  const button = [...root.querySelectorAll("button")].find((candidate) => candidate.textContent?.includes(label));
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
  assert.ok(predicate(), "Timed out waiting for MFA component state");
}
