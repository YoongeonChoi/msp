import assert from "node:assert/strict";
import React, { act } from "react";
import { JSDOM } from "jsdom";

import { RecordsView } from "../src/components/operations/OperationsDataViews";
import { makeOperationsSnapshot } from "./operationsFixture";

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  pretendToBeVisual: true,
  url: "http://localhost:1420/?page=records"
});
installDom(dom);

const { fireEvent, render } = await import("@testing-library/react");
const container = dom.window.document.getElementById("root");
assert.ok(container);
const recordsRender = render(<RecordsView snapshot={makeOperationsSnapshot()} />, { container });

const auditFilter = container.querySelector('select[aria-label="감사 결과 필터"]');
assert.ok(auditFilter instanceof dom.window.HTMLSelectElement);
assert.match(container.textContent ?? "", /정책 충족|policy_satisfied/);

fireEvent.change(auditFilter, { target: { value: "failed" } });
assert.equal(auditFilter.value, "failed");
assert.match(container.textContent ?? "", /조건에 맞는 감사 이벤트가 없습니다/);

fireEvent.change(auditFilter, { target: { value: "success" } });
assert.equal(auditFilter.value, "success");
assert.doesNotMatch(container.textContent ?? "", /조건에 맞는 감사 이벤트가 없습니다/);
assert.match(container.textContent ?? "", /policy_satisfied/);

await act(async () => recordsRender.unmount());
dom.window.close();

console.log("audit record filter interaction passed");

function installDom(value: JSDOM): void {
  Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", { value: true, configurable: true, writable: true });
  Object.defineProperty(globalThis, "window", { value: value.window, configurable: true });
  Object.defineProperty(globalThis, "document", { value: value.window.document, configurable: true });
  Object.defineProperty(globalThis, "navigator", { value: value.window.navigator, configurable: true });
  Object.defineProperty(globalThis, "HTMLElement", { value: value.window.HTMLElement, configurable: true });
  Object.defineProperty(globalThis, "HTMLSelectElement", { value: value.window.HTMLSelectElement, configurable: true });
  Object.defineProperty(globalThis, "Event", { value: value.window.Event, configurable: true });
}
