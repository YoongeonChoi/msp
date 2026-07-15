import assert from "node:assert/strict";
import React, { act } from "react";
import { JSDOM } from "jsdom";

import { LazySurfaceBoundary } from "../src/components/LazySurfaceBoundary";

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  pretendToBeVisual: true,
  url: "http://localhost:1420/?page=control"
});
Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", { value: true, configurable: true });
Object.defineProperty(globalThis, "window", { value: dom.window, configurable: true });
Object.defineProperty(globalThis, "document", { value: dom.window.document, configurable: true });
Object.defineProperty(globalThis, "navigator", { value: dom.window.navigator, configurable: true });
Object.defineProperty(globalThis, "HTMLElement", { value: dom.window.HTMLElement, configurable: true });
Object.defineProperty(globalThis, "HTMLButtonElement", { value: dom.window.HTMLButtonElement, configurable: true });

const container = dom.window.document.getElementById("root");
assert.ok(container);
const { render } = await import("@testing-library/react");
const originalConsoleError = console.error;
console.error = () => undefined;

function BrokenDeferredSurface(): React.ReactNode {
  throw new Error("synthetic deferred chunk failure");
}

const rendered = render(
  <LazySurfaceBoundary
    title="운영 상세 화면을 안전하게 열지 못했습니다"
    detail="화면을 다시 불러오기 전까지 운영 변경 기능은 차단됩니다."
    logCode="test_lazy_surface_failure"
  >
    <BrokenDeferredSurface />
  </LazySurfaceBoundary>,
  { container }
);

assert.equal(container.querySelector('[role="alert"]') !== null, true);
assert.match(container.textContent ?? "", /운영 상세 화면을 안전하게 열지 못했습니다/);
assert.match(container.textContent ?? "", /운영 변경 기능은 차단됩니다/);
const reloadButton = [...container.querySelectorAll("button")].find(
  (button) => button.textContent?.includes("앱 새로고침")
);
assert.ok(reloadButton instanceof dom.window.HTMLButtonElement);

await act(async () => rendered.unmount());
console.error = originalConsoleError;
dom.window.close();

console.log("deferred UI failure remains fail-closed");
