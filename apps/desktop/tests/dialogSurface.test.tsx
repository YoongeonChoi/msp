import assert from "node:assert/strict";
import React, { act } from "react";
import { JSDOM } from "jsdom";

import { ConfirmDialog, DrawerSurface, type DialogCloseReason } from "../src/components/DialogSurface";

const dom = new JSDOM(
  '<!doctype html><html><body><button id="opener">열기</button><div id="root"></div></body></html>',
  { pretendToBeVisual: true, url: "http://localhost:1420/?page=control" }
);
installDom(dom);
installDialogShim(dom);

const { fireEvent, render } = await import("@testing-library/react");
const container = dom.window.document.getElementById("root");
const opener = dom.window.document.getElementById("opener");
assert.ok(container);
assert.ok(opener instanceof dom.window.HTMLButtonElement);
opener.focus();

let resolveConfirm: (() => void) | null = null;
let confirmCount = 0;
let cancelCount = 0;
const confirmPromise = new Promise<void>((resolve) => {
  resolveConfirm = resolve;
});
const confirmRender = render(
  <ConfirmDialog
    open
    title="모의거래 일시정지"
    description="최신 상태를 다시 확인한 뒤 요청합니다."
    confirmLabel="일시정지 요청"
    onCancel={() => {
      cancelCount += 1;
    }}
    onConfirm={() => {
      confirmCount += 1;
      return confirmPromise;
    }}
  />,
  { container }
);

const confirmDialog = container.querySelector("dialog");
assert.ok(confirmDialog instanceof dom.window.HTMLDialogElement);
await waitFor(() => dom.window.document.activeElement?.textContent === "취소");

fireEvent(confirmDialog, new dom.window.Event("cancel", { cancelable: true }));
fireEvent.pointerDown(confirmDialog);
fireEvent.pointerUp(confirmDialog);
assert.equal(cancelCount, 0, "confirm dialog must ignore Escape and backdrop dismissal");

const confirmButton = buttonByText(dom, container, "일시정지 요청");
const cancelButton = buttonByText(dom, container, "취소");
confirmButton.focus();
fireEvent.keyDown(confirmDialog, { key: "Tab" });
assert.equal(dom.window.document.activeElement, cancelButton, "Tab wraps from the last action to the safe action");
cancelButton.focus();
fireEvent.keyDown(confirmDialog, { key: "Tab", shiftKey: true });
assert.equal(dom.window.document.activeElement, confirmButton, "Shift+Tab wraps to the last action");

await act(async () => {
  confirmButton.click();
  confirmButton.click();
  await Promise.resolve();
});
assert.equal(confirmCount, 1, "an in-flight confirmation must reject double submission");
assert.ok(resolveConfirm);
await act(async () => {
  resolveConfirm();
  await confirmPromise;
});

confirmRender.rerender(
  <ConfirmDialog
    open={false}
    title="모의거래 일시정지"
    onCancel={() => {
      cancelCount += 1;
    }}
    onConfirm={() => undefined}
  />
);
await waitFor(() => dom.window.document.activeElement === opener);
await act(async () => confirmRender.unmount());

const closeReasons: DialogCloseReason[] = [];
const drawerContainer = dom.window.document.createElement("div");
dom.window.document.body.append(drawerContainer);
const drawerRender = render(
  <DrawerSurface
    open
    readOnly
    title="명령 상세"
    onRequestClose={(reason) => closeReasons.push(reason)}
  >
    <button type="button">상세 내부 행동</button>
  </DrawerSurface>,
  { container: drawerContainer }
);
const readOnlyDrawer = drawerContainer.querySelector("dialog");
assert.ok(readOnlyDrawer instanceof dom.window.HTMLDialogElement);
fireEvent(readOnlyDrawer, new dom.window.Event("cancel", { cancelable: true }));
fireEvent.pointerDown(readOnlyDrawer);
fireEvent.pointerUp(readOnlyDrawer);
assert.deepEqual(closeReasons, ["escape", "backdrop"]);
await act(async () => drawerRender.unmount());

const dirtyContainer = dom.window.document.createElement("div");
dom.window.document.body.append(dirtyContainer);
let dirtyCloseCount = 0;
let discardPromptCount = 0;
const dirtyRender = render(
  <DrawerSurface
    open
    readOnly={false}
    dirty
    title="접근권한 변경"
    confirmDiscard={() => {
      discardPromptCount += 1;
      return true;
    }}
    onRequestClose={() => {
      dirtyCloseCount += 1;
    }}
  >
    <input aria-label="사용자 UUID" />
  </DrawerSurface>,
  { container: dirtyContainer }
);
const dirtyDrawer = dirtyContainer.querySelector("dialog");
assert.ok(dirtyDrawer instanceof dom.window.HTMLDialogElement);
fireEvent(dirtyDrawer, new dom.window.Event("cancel", { cancelable: true }));
fireEvent.pointerDown(dirtyDrawer);
fireEvent.pointerUp(dirtyDrawer);
assert.equal(dirtyCloseCount, 0, "form drawers must ignore Escape and backdrop dismissal");
await act(async () => buttonByLabel(dom, dirtyContainer, "상세 닫기").click());
assert.equal(discardPromptCount, 1);
assert.equal(dirtyCloseCount, 1, "explicit close proceeds only after discard confirmation");
await act(async () => dirtyRender.unmount());

const autoDirtyContainer = dom.window.document.createElement("div");
dom.window.document.body.append(autoDirtyContainer);
let autoDirtyCloseCount = 0;
const autoDirtyRender = render(
  <DrawerSurface
    open
    readOnly={false}
    title="수동 대사 증거"
    onRequestClose={() => {
      autoDirtyCloseCount += 1;
    }}
  >
    <input aria-label="증거 SHA" />
  </DrawerSurface>,
  { container: autoDirtyContainer }
);
const evidenceInput = autoDirtyContainer.querySelector('input[aria-label="증거 SHA"]');
assert.ok(evidenceInput instanceof dom.window.HTMLInputElement);
fireEvent.input(evidenceInput, { target: { value: "a".repeat(64) } });
await act(async () => buttonByLabel(dom, autoDirtyContainer, "상세 닫기").click());
assert.equal(autoDirtyCloseCount, 0, "edited form drawer must not close before discard confirmation");
await waitFor(() => [...autoDirtyContainer.querySelectorAll("button")].some((button) => button.textContent?.includes("계속 작성")));
await act(async () => buttonByText(dom, autoDirtyContainer, "계속 작성").click());
const outerDrawer = autoDirtyContainer.querySelector('dialog[data-variant="drawer"]');
assert.ok(outerDrawer instanceof dom.window.HTMLDialogElement);
const outerClose = buttonByLabel(dom, autoDirtyContainer, "상세 닫기");
outerClose.focus();
fireEvent.keyDown(outerDrawer, { key: "Tab", shiftKey: true });
assert.equal(dom.window.document.activeElement, evidenceInput, "closed nested confirm actions must not enter the drawer focus loop");
await act(async () => outerClose.click());
await act(async () => buttonByText(dom, autoDirtyContainer, "입력 삭제하고 닫기").click());
assert.equal(autoDirtyCloseCount, 1, "discard confirmation closes the edited form drawer exactly once");
await act(async () => autoDirtyRender.unmount());

dom.window.close();
console.log("Native dialog surface focus and dismissal policies passed");

function installDom(value: JSDOM): void {
  Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", { value: true, configurable: true, writable: true });
  Object.defineProperty(globalThis, "window", { value: value.window, configurable: true });
  Object.defineProperty(globalThis, "document", { value: value.window.document, configurable: true });
  Object.defineProperty(globalThis, "navigator", { value: value.window.navigator, configurable: true });
  Object.defineProperty(globalThis, "HTMLElement", { value: value.window.HTMLElement, configurable: true });
  Object.defineProperty(globalThis, "HTMLDialogElement", { value: value.window.HTMLDialogElement, configurable: true });
  Object.defineProperty(globalThis, "HTMLButtonElement", { value: value.window.HTMLButtonElement, configurable: true });
  Object.defineProperty(globalThis, "Event", { value: value.window.Event, configurable: true });
}

function installDialogShim(value: JSDOM): void {
  const prototype = value.window.HTMLDialogElement.prototype;
  Object.defineProperty(prototype, "showModal", {
    configurable: true,
    value(this: HTMLDialogElement) {
      this.setAttribute("open", "");
    }
  });
  Object.defineProperty(prototype, "close", {
    configurable: true,
    value(this: HTMLDialogElement) {
      this.removeAttribute("open");
    }
  });
}

function buttonByText(value: JSDOM, root: HTMLElement, label: string): HTMLButtonElement {
  const button = [...root.querySelectorAll("button")].find((candidate) => candidate.textContent?.includes(label));
  assert.ok(button instanceof value.window.HTMLButtonElement, `Button not found: ${label}`);
  return button;
}

function buttonByLabel(value: JSDOM, root: HTMLElement, label: string): HTMLButtonElement {
  const button = root.querySelector(`button[aria-label="${label}"]`);
  assert.ok(button instanceof value.window.HTMLButtonElement, `Button not found: ${label}`);
  return button;
}

async function waitFor(predicate: () => boolean): Promise<void> {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    if (predicate()) {
      return;
    }
    await act(async () => new Promise((resolve) => setTimeout(resolve, 2)));
  }
  assert.ok(predicate(), "Timed out waiting for dialog state");
}
