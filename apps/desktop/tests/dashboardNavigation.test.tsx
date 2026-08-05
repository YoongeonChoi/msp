import assert from "node:assert/strict";
import React, { act } from "react";
import { JSDOM } from "jsdom";

import { AppSidebar } from "../src/components/shell/AppSidebar";
import { navItems, type PageKey } from "../src/lib/navigation";

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  pretendToBeVisual: true,
  url: "http://localhost:1420/?page=control"
});
installDom(dom);

const { fireEvent, render } = await import("@testing-library/react");
const container = dom.window.document.getElementById("root");
assert.ok(container);

const navigated: PageKey[] = [];
let refreshCount = 0;
const connectedRender = render(
  <AppSidebar
    page="control"
    accountLabel="운영자"
    accountDetail="운영 담당자"
    assuranceLabel="2단계 인증 확인"
    environmentLabel="PAPER"
    connected
    refreshing={false}
    onNavigate={(page) => navigated.push(page)}
    onRefresh={() => { refreshCount += 1; }}
  />,
  { container }
);

for (const item of navItems) {
  const button = buttonByExactText(dom, container, item.label);
  assert.equal(button.disabled, false, `${item.label} workspace is available to a connected device`);
  fireEvent.click(button);
}
assert.deepEqual(navigated, navItems.map((item) => item.key), "all nine workspace buttons call the real navigation handler");
fireEvent.click(buttonByExactText(dom, container, "최신 상태 새로고침"));
assert.equal(refreshCount, 1, "the shared refresh button calls the snapshot refresh handler once");

await act(async () => connectedRender.unmount());
const disconnectedContainer = dom.window.document.createElement("div");
dom.window.document.body.append(disconnectedContainer);
const disconnectedRender = render(
  <AppSidebar
    page="settings"
    accountLabel="기기 연결 필요"
    accountDetail="운영 세션 없음"
    assuranceLabel="기기 연결 필요"
    environmentLabel="환경 확인 필요"
    connected={false}
    refreshing={false}
    onNavigate={(page) => navigated.push(page)}
    onRefresh={() => { refreshCount += 1; }}
  />,
  { container: disconnectedContainer }
);

for (const item of navItems.filter((item) => item.requiresConnection)) {
  assert.equal(buttonByExactText(dom, disconnectedContainer, item.label).disabled, true, `${item.label} fails closed before device connection`);
}
assert.equal(buttonByExactText(dom, disconnectedContainer, "계정·보안").disabled, false);
assert.equal(
  [...disconnectedContainer.querySelectorAll("button")].some((button) => button.textContent?.trim() === "이 기기 연결"),
  false,
  "the settings sidebar does not duplicate the main connection action with a no-op footer button"
);

await act(async () => disconnectedRender.unmount());
dom.window.close();

console.log("dashboard navigation and disconnected-state guards passed");

function installDom(value: JSDOM): void {
  Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", { value: true, configurable: true, writable: true });
  Object.defineProperty(globalThis, "window", { value: value.window, configurable: true });
  Object.defineProperty(globalThis, "document", { value: value.window.document, configurable: true });
  Object.defineProperty(globalThis, "navigator", { value: value.window.navigator, configurable: true });
  Object.defineProperty(globalThis, "HTMLElement", { value: value.window.HTMLElement, configurable: true });
  Object.defineProperty(globalThis, "HTMLButtonElement", { value: value.window.HTMLButtonElement, configurable: true });
  Object.defineProperty(globalThis, "Event", { value: value.window.Event, configurable: true });
}

function buttonByExactText(value: JSDOM, root: HTMLElement, label: string): HTMLButtonElement {
  const button = [...root.querySelectorAll("button")].find((candidate) => candidate.textContent?.trim() === label);
  assert.ok(button instanceof value.window.HTMLButtonElement, `Button not found: ${label}`);
  return button;
}
