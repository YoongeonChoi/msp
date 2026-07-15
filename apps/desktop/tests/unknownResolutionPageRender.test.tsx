import assert from "node:assert/strict";
import React from "react";
import { JSDOM } from "jsdom";
import { renderToStaticMarkup } from "react-dom/server";

import { ManualReconciliationCase } from "../src/components/operations/ManualReconciliationCase";
import { makeOperationsSnapshot } from "./operationsFixture";
import {
  makeRequestedUnknownResolutionSnapshot,
  makeUnknownResolutionSnapshot
} from "./unknownResolutionFixture";

const now = new Date("2099-07-14T00:00:00.000Z");
const noopRequest = () => undefined;
const noopReview = () => undefined;

const requestedOperations = makeOperationsSnapshot();
const requestedUnknown = makeRequestedUnknownResolutionSnapshot();
const requestedDocument = renderCase(requestedOperations, requestedUnknown, true);
assert.ok(requestedDocument.querySelector('ol[aria-label="조정 요청·승인·Worker ACK·postcondition 타임라인"]'));
assert.ok(requestedDocument.querySelector('ol[aria-label="제출된 누락 체결 목록"]') === null);
assert.match(requestedDocument.body.textContent ?? "", /누락 체결 없음/);
assert.equal(buttonByText(requestedDocument, "증거 승인").disabled, false);
assert.equal(buttonByText(requestedDocument, "증거 거절").disabled, false);
assert.doesNotMatch(requestedDocument.body.textContent ?? "", /Worker 적용·회계 확인/);

const selfReviewOperations = makeOperationsSnapshot();
assert.ok(selfReviewOperations.access.actor && requestedUnknown.cases[0].request);
selfReviewOperations.access.actor.actor_id = requestedUnknown.cases[0].request.requested_by.actor_id;
const selfReviewDocument = renderCase(selfReviewOperations, requestedUnknown, true);
assert.match(selfReviewDocument.body.textContent ?? "", /본인이 요청한 회계 조정은 승인하거나 거절할 수 없습니다/);
assert.equal(buttonByText(selfReviewDocument, "증거 승인").disabled, true);
assert.equal(buttonByText(selfReviewDocument, "증거 거절").disabled, true);

const offlineOperations = makeOperationsSnapshot();
const openUnknown = makeUnknownResolutionSnapshot();
const offlineDocument = renderCase(offlineOperations, openUnknown, false);
const offlineRequest = buttonByText(offlineDocument, "AAL2 step-up 후 조정 요청");
assert.equal(offlineRequest.disabled, true, "offline/stale boundary must disable, not queue, the request form");
assert.ok(offlineDocument.querySelector("fieldset"));
for (const label of [
  "Evidence artifact URI",
  "Evidence SHA-256",
  "Evidence captured at",
  "확정 terminal 상태",
  "누락 체결 manifest (strict JSON array)"
]) {
  assert.ok(
    Array.from(offlineDocument.querySelectorAll("label")).some((element) => element.textContent?.includes(label)),
    `accessible explicit input label missing: ${label}`
  );
}

const expiredSession = makeOperationsSnapshot();
expiredSession.access.session_state = "expired";
const expiredDocument = renderCase(expiredSession, openUnknown, true);
assert.doesNotMatch(
  expiredDocument.body.textContent ?? "",
  /AAL2 step-up 후 조정 요청/,
  "expired sessions must not expose a mutable operator form"
);

console.log("unknown-resolution V2 role/self-review/offline/session render guards passed");

function renderCase(
  snapshot: ReturnType<typeof makeOperationsSnapshot>,
  unknownSnapshot: ReturnType<typeof makeUnknownResolutionSnapshot>,
  mutationsAllowed: boolean
): Document {
  const markup = renderToStaticMarkup(
    <ManualReconciliationCase
      snapshot={snapshot}
      unknownSnapshot={unknownSnapshot}
      mutationsAllowed={mutationsAllowed}
      pending={false}
      now={now}
      onRequest={noopRequest}
      onReview={noopReview}
    />
  );
  return new JSDOM(markup).window.document;
}

function buttonByText(document: Document, label: string): HTMLButtonElement {
  const button = Array.from(document.querySelectorAll("button")).find((candidate) =>
    candidate.textContent?.includes(label)
  );
  assert.ok(button, `Button not found: ${label}`);
  return button as HTMLButtonElement;
}
