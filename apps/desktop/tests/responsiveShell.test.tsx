import assert from "node:assert/strict";
import React, { act } from "react";
import { JSDOM } from "jsdom";

import { AppLayout } from "../src/components/Layout";

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  pretendToBeVisual: true,
  url: "http://localhost:1420/?page=settings"
});
installDom(dom);

const { fireEvent, render } = await import("@testing-library/react");
const container = dom.window.document.getElementById("root");
assert.ok(container);

const shellRender = render(
  <AppLayout
    page="settings"
    setPage={() => undefined}
    connectionState="disconnected"
  >
    <div>계정·보안 콘텐츠</div>
  </AppLayout>,
  { container }
);

const menuButton = buttonByLabel(dom, container, "메뉴 열기");
assert.equal(menuButton.getAttribute("aria-expanded"), "false");
await act(async () => fireEvent.click(menuButton));

const menuDialog = container.querySelector('[role="dialog"][aria-label="대시보드 메뉴"]');
assert.ok(menuDialog instanceof dom.window.HTMLElement);
assert.equal(menuButton.getAttribute("aria-expanded"), "true");
assert.equal(dom.window.document.body.style.overflow, "hidden", "open mobile navigation locks background scrolling");

const currentPageButton = menuDialog.querySelector<HTMLButtonElement>('button[aria-current="page"]');
assert.ok(currentPageButton instanceof dom.window.HTMLButtonElement);
assert.equal(currentPageButton.textContent?.trim(), "계정·보안");
assert.equal(dom.window.document.activeElement, currentPageButton, "the current workspace receives initial menu focus");

const focusable = [...menuDialog.querySelectorAll<HTMLButtonElement>('button:not([disabled])')];
assert.ok(focusable.length >= 2, "the disconnected menu keeps its close control and current settings destination focusable");
const firstFocusable = focusable[0];
const lastFocusable = focusable[focusable.length - 1];
lastFocusable.focus();
fireEvent.keyDown(dom.window, { key: "Tab" });
assert.equal(dom.window.document.activeElement, firstFocusable, "Tab wraps from the final menu control to the close button");
fireEvent.keyDown(dom.window, { key: "Tab", shiftKey: true });
assert.equal(dom.window.document.activeElement, lastFocusable, "Shift+Tab wraps back to the final menu control");

fireEvent.keyDown(dom.window, { key: "Escape" });
assert.equal(container.querySelector('[role="dialog"][aria-label="대시보드 메뉴"]'), null);
assert.equal(dom.window.document.body.style.overflow, "", "closing mobile navigation restores background scrolling");
assert.equal(dom.window.document.activeElement, menuButton, "closing the menu restores focus to its trigger");

await act(async () => shellRender.unmount());
dom.window.close();

console.log("responsive shell focus trap and scroll lock passed");

function installDom(value: JSDOM): void {
  Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", { value: true, configurable: true, writable: true });
  Object.defineProperty(globalThis, "window", { value: value.window, configurable: true });
  Object.defineProperty(globalThis, "document", { value: value.window.document, configurable: true });
  Object.defineProperty(globalThis, "navigator", { value: value.window.navigator, configurable: true });
  Object.defineProperty(globalThis, "HTMLElement", { value: value.window.HTMLElement, configurable: true });
  Object.defineProperty(globalThis, "HTMLButtonElement", { value: value.window.HTMLButtonElement, configurable: true });
  Object.defineProperty(globalThis, "Event", { value: value.window.Event, configurable: true });
  Object.defineProperty(globalThis, "KeyboardEvent", { value: value.window.KeyboardEvent, configurable: true });
}

function buttonByLabel(value: JSDOM, root: HTMLElement, label: string): HTMLButtonElement {
  const button = root.querySelector(`button[aria-label="${label}"]`);
  assert.ok(button instanceof value.window.HTMLButtonElement, `Button not found: ${label}`);
  return button;
}
