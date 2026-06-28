import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ControlPage } from "../src/pages/ControlPage";
import type { BotSettings, ManualCommandRow } from "../src/lib/rows";

const nowMs = Date.parse("2099-06-28T00:00:00.000Z");

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
    updatedAt: new Date(nowMs).toISOString(),
    ...overrides
  };
}

function command(overrides: Partial<ManualCommandRow>): ManualCommandRow {
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

function renderControlPage(rows: readonly ManualCommandRow[], currentUserId: string | null): string {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false
      }
    }
  });
  queryClient.setQueryData(["bot_settings"], settings());
  queryClient.setQueryData(["manual_commands", "request_live_enable"], rows);
  queryClient.setQueryData(["auth", "current_user_id"], currentUserId);
  return renderToStaticMarkup(
    <QueryClientProvider client={queryClient}>
      <ControlPage />
    </QueryClientProvider>
  );
}

const pendingOwnRequest = renderControlPage([command({ requestedBy: "requester" })], "requester");
assert.match(pendingOwnRequest, /Live 승인 게이트/);
assert.match(pendingOwnRequest, /fresh 승인/);
assert.match(pendingOwnRequest, />없음</);
assert.match(pendingOwnRequest, /본인 요청은 다른 admin 승인 필요/);
assert.match(pendingOwnRequest, /<button disabled=""[^>]*>실주문 허용 활성화<\/button>/);

const acceptedByReviewer = renderControlPage(
  [
    command({
      status: "accepted",
      reviewedBy: "reviewer",
      reviewedAt: new Date(nowMs).toISOString()
    })
  ],
  "requester"
);
assert.match(acceptedByReviewer, />있음</);
assert.match(acceptedByReviewer, /release-1 · risk-2026-06-28/);
assert.match(acceptedByReviewer, /<button disabled=""[^>]*>실주문 허용 활성화<\/button>/);

const appliedCommand = renderControlPage(
  [
    command({
      status: "applied",
      reviewedBy: "reviewer",
      reviewedAt: new Date(nowMs).toISOString(),
      appliedAt: new Date(nowMs + 60_000).toISOString()
    })
  ],
  "requester"
);
assert.match(appliedCommand, />없음</);
assert.match(appliedCommand, /applied/);
assert.match(appliedCommand, /<button disabled=""[^>]*>실주문 허용 활성화<\/button>/);

const rejectedCommand = renderControlPage(
  [
    command({
      status: "rejected",
      reviewedBy: "reviewer",
      rejectionReason: "operator_rejected"
    })
  ],
  "requester"
);
assert.match(rejectedCommand, />없음</);
assert.match(rejectedCommand, /rejected/);
assert.match(rejectedCommand, /<button disabled=""[^>]*>실주문 허용 활성화<\/button>/);

console.log("controlPage render fixtures passed");
