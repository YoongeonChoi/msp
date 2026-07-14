import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { AppLayout } from "../src/components/Layout";
import { DashboardPage } from "../src/pages/DashboardPage";
import { OrdersPage } from "../src/pages/OrdersPage";
import type { AuthRoleState } from "../src/lib/supabaseData";
import type { BotSettings, OrderRow } from "../src/lib/rows";

const adminRole: AuthRoleState = {
  signedIn: true,
  email: "admin@example.com",
  role: "admin",
  warning: null
};

const settings: BotSettings = {
  id: "singleton",
  enabled: false,
  mode: "paper",
  liveOrderAllowed: false,
  deploymentLock: false,
  deploymentTargetSha: null,
  deploymentStartedAt: null,
  deploymentCompletedAt: null,
  maxOrderAmountKrw: 100000,
  maxDailyLossPct: 0.02,
  maxDailyOrderCount: 10,
  maxPositionPct: 0.1,
  maxSectorPct: 0.3,
  loopIntervalSec: 30,
  updatedAt: "2026-07-14T00:00:00.000Z"
};

const unknownOrder: OrderRow = {
  id: "order-unknown-1",
  symbol: "005930",
  side: "buy",
  mode: "live",
  status: "unknown_requires_manual_check",
  amountKrw: 100000,
  quantity: 1,
  priceKrw: 100000,
  idempotencyKey: "live-order-unknown-1",
  providerOrderId: null,
  reason: "live_broker_order_result_pending",
  reasonJson: {},
  riskSnapshotJson: {},
  createdAt: "2026-07-14T00:00:00.000Z"
};

const dashboardClient = createSeededClient();
const dashboardMarkup = renderToStaticMarkup(
  <QueryClientProvider client={dashboardClient}>
    <AppLayout page="dashboard" setPage={() => undefined}>
      <DashboardPage />
    </AppLayout>
  </QueryClientProvider>
);
const dashboardText = new JSDOM(dashboardMarkup).window.document.body.textContent ?? "";
assert.match(dashboardText, /수동 확인 주문: 1건/, "status bar must show the unresolved order count");
assert.match(dashboardText, /자동으로 해소할 수 없는 주문 1건/, "dashboard must surface the unresolved order warning");

const ordersClient = createSeededClient();
const ordersMarkup = renderToStaticMarkup(
  <QueryClientProvider client={ordersClient}>
    <OrdersPage />
  </QueryClientProvider>
);
const ordersDocument = new JSDOM(ordersMarkup).window.document;
const ordersText = ordersDocument.body.textContent ?? "";
assert.match(ordersText, /수동 확인 필요 주문 1건/);
assert.match(ordersText, /unknown_requires_manual_check · 수동 확인/);
assert.match(ordersText, /상태를 바꾸거나 브로커 API를 호출하지 않습니다/);
assert.match(ordersText, /order-unknown-1/);
const safetyQueue = ordersDocument.querySelector('[aria-label="수동 확인 필요 주문 목록"]');
assert.ok(safetyQueue, "manual-check safety queue must be rendered");
assert.equal(safetyQueue.querySelectorAll("button").length, 0, "safety queue must not expose mutation controls");
const safetyAnnouncement = ordersDocument.querySelector('[aria-live="polite"][role="alert"]');
assert.ok(safetyAnnouncement, "manual-check count must be announced as an alert");
assert.match(ordersText, /1-1 \/ 1건/, "queue must show the bounded detail range and exact total");

const testDir = dirname(fileURLToPath(import.meta.url));
const dataSource = readFileSync(resolve(testDir, "../src/lib/supabaseData.ts"), "utf8");
const countStart = dataSource.indexOf("export async function fetchManualCheckOrderCount");
const countEnd = dataSource.indexOf("\nexport async function", countStart + 1);
assert.ok(countStart >= 0, "manual-check count query source must be present");
const countSource = dataSource.slice(countStart, countEnd < 0 ? dataSource.length : countEnd);
assert.match(countSource, /count: "exact"/);
assert.match(countSource, /head: true/);
assert.match(countSource, /\.eq\("mode", "live"\)/);
assert.match(countSource, /\.eq\("status", "unknown_requires_manual_check"\)/);
assert.match(countSource, /result\.count === null/);

const fetchStart = dataSource.indexOf("export async function fetchOrdersRequiringManualCheck");
const fetchEnd = dataSource.indexOf("\nexport async function", fetchStart + 1);
assert.ok(fetchStart >= 0, "manual-check query source must be present");
const fetchSource = dataSource.slice(fetchStart, fetchEnd < 0 ? dataSource.length : fetchEnd);
assert.doesNotMatch(fetchSource, /\.select\("\*"\)/);
assert.match(fetchSource, /\.eq\("mode", "live"\)/);
assert.match(fetchSource, /\.eq\("status", "unknown_requires_manual_check"\)/);
assert.match(fetchSource, /\.range\(rangeStart, rangeStart \+ normalizedLimit - 1\)/);

console.log("order safety queue fixtures passed");

function createSeededClient(): QueryClient {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  queryClient.setQueryData(["auth_role"], adminRole);
  queryClient.setQueryData(["bot_settings"], settings);
  queryClient.setQueryData(["worker_heartbeats", "latest"], null);
  queryClient.setQueryData(["api_health"], []);
  queryClient.setQueryData(["decision_snapshots", "today"], []);
  queryClient.setQueryData(["orders", "today"], []);
  queryClient.setQueryData(["orders", "recent"], []);
  queryClient.setQueryData(["orders", "manual_check", "count"], 1);
  queryClient.setQueryData(["orders", "manual_check", "page", 0, 25], [unknownOrder]);
  queryClient.setQueryData(["engine_events"], []);
  return queryClient;
}
