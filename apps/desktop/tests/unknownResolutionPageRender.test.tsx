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
assert.ok(requestedDocument.querySelector('ol[aria-label="조정 요청·승인·Worker 적용·회계 반영 타임라인"]'));
assert.ok(requestedDocument.querySelector('ol[aria-label="제출된 누락 체결 목록"]') === null);
assert.match(requestedDocument.body.textContent ?? "", /누락 체결 없음/);
assert.equal(buttonByText(requestedDocument, "증거 승인").disabled, false);
assert.equal(buttonByText(requestedDocument, "증거 거절").disabled, false);
assert.doesNotMatch(requestedDocument.body.textContent ?? "", /Worker 적용·회계 확인/);

const claimedUnknown = makeClaimedUnknownResolutionSnapshot();
const claimedDocument = renderCase(makeOperationsSnapshot(), claimedUnknown, true);
assert.match(
  timelineStepByLabel(claimedDocument, "Worker 적용 확인").textContent ?? "",
  /미확인.*적용 대기/,
  "Worker claim alone must not complete the application step"
);
assert.doesNotMatch(
  claimedDocument.body.textContent ?? "",
  /Worker 적용·회계 반영 확인/,
  "claimed work must not render the final completion label"
);

const appliedWithoutAccountingUnknown = makeClaimedUnknownResolutionSnapshot();
const appliedWithoutAccountingCase = appliedWithoutAccountingUnknown.cases[0];
assert.ok(appliedWithoutAccountingCase.request && appliedWithoutAccountingCase.work_receipt);
appliedWithoutAccountingCase.request.state = "applied";
appliedWithoutAccountingCase.work_receipt.state = "applied";
appliedWithoutAccountingCase.work_receipt.applied_at = "2099-07-14T00:01:00.000Z";
appliedWithoutAccountingCase.break_state = "resolved";
appliedWithoutAccountingCase.resolved_at = "2099-07-14T00:01:00.000Z";
const appliedWithoutAccountingDocument = renderCase(
  makeOperationsSnapshot(),
  appliedWithoutAccountingUnknown,
  true
);
assert.match(
  timelineStepByLabel(appliedWithoutAccountingDocument, "Worker 적용 확인").textContent ?? "",
  /확인됨/,
  "an applied Worker receipt completes only the Worker step"
);
assert.match(
  timelineStepByLabel(appliedWithoutAccountingDocument, "회계 반영 결과").textContent ?? "",
  /미확인/,
  "the accounting step must remain incomplete without its postcondition"
);
assert.match(
  appliedWithoutAccountingDocument.body.textContent ?? "",
  /Worker 적용 확인 · 회계 반영 검증 대기/,
  "applied work without accounting verification must render a waiting label"
);
assert.doesNotMatch(
  appliedWithoutAccountingDocument.body.textContent ?? "",
  /Worker 적용·회계 반영 확인/,
  "applied work without accounting verification must not render final completion"
);

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
const offlineRequest = buttonByText(offlineDocument, "추가 본인 확인 후 조정 요청");
assert.equal(offlineRequest.disabled, true, "offline/stale boundary must disable, not queue, the request form");
assert.ok(offlineDocument.querySelector("fieldset"));
for (const label of [
  "증거 자료 위치",
  "증거 SHA-256",
  "증거 수집 시각",
  "확정된 최종 주문 상태",
  "누락 체결 목록 (엄격한 JSON 배열)"
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
  /추가 본인 확인 후 조정 요청/,
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

function timelineStepByLabel(document: Document, label: string): HTMLLIElement {
  const timeline = document.querySelector('ol[aria-label="조정 요청·승인·Worker 적용·회계 반영 타임라인"]');
  assert.ok(timeline, "reconciliation timeline not found");
  const step = Array.from(timeline.querySelectorAll("li")).find((candidate) =>
    candidate.textContent?.includes(label)
  );
  assert.ok(step, `Timeline step not found: ${label}`);
  return step as HTMLLIElement;
}

function makeClaimedUnknownResolutionSnapshot(): ReturnType<typeof makeRequestedUnknownResolutionSnapshot> {
  const snapshot = makeRequestedUnknownResolutionSnapshot();
  const context = snapshot.cases[0];
  assert.ok(context.request);
  context.request.state = "claimed";
  context.break_revision = 6;
  context.review = {
    schema_version: 2,
    review_id: "45454545-4545-4545-8545-454545454545",
    decision: "approved",
    reason_code: "evidence_sufficient",
    reviewed_by: {
      actor_id: "46464646-4646-4646-8646-464646464646",
      display_name: "독립 검토자",
      roles: ["risk_approver"]
    },
    reviewed_at: "2099-07-14T00:00:00.000Z",
    request_digest_sha256: "e".repeat(64),
    review_digest_sha256: "f".repeat(64),
    evidence_sha256: "d".repeat(64)
  };
  context.work_receipt = {
    schema_version: 2,
    state: "claimed",
    work_revision: 1,
    claim_token: "47474747-4747-4747-8747-474747474747",
    claimed_at: "2099-07-14T00:00:30.000Z",
    claim_expires_at: "2099-07-14T00:05:30.000Z",
    applied_at: null,
    worker_release_sha: "a".repeat(40),
    fencing_token: 1
  };
  return snapshot;
}
