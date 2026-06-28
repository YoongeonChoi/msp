import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { DashboardPage } from "../src/pages/DashboardPage";
import type { ApiHealth, BotSettings, EngineEventRow, WorkerHeartbeat } from "../src/lib/rows";

const nowIso = "2026-06-28T00:00:00.000Z";

const settings: BotSettings = {
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
  updatedAt: nowIso
};

const heartbeat: WorkerHeartbeat = {
  id: "heartbeat-1",
  status: "ok",
  memoryMb: 128,
  lastLoopMs: 42,
  message: null,
  createdAt: nowIso
};

const apiHealth: ApiHealth[] = [
  {
    id: "api-1",
    provider: "toss",
    healthy: true,
    status: "ok",
    latencyMs: 20,
    message: null,
    errorCode: null,
    checkedAt: nowIso
  }
];

const criticalEvent: EngineEventRow = {
  id: "event-1",
  level: "critical",
  component: "live_reconciliation",
  message: "live_order_manual_check_still_unknown",
  details: {
    order_id: "order-1",
    symbol: "005930",
    status: "unknown_requires_manual_check"
  },
  createdAt: nowIso
};

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: false
    }
  }
});
queryClient.setQueryData(["bot_settings"], settings);
queryClient.setQueryData(["worker_heartbeats", "latest"], heartbeat);
queryClient.setQueryData(["api_health"], apiHealth);
queryClient.setQueryData(["decision_snapshots", "today"], []);
queryClient.setQueryData(["orders", "today"], []);
queryClient.setQueryData(["engine_events"], [criticalEvent]);

const markup = renderToStaticMarkup(
  <QueryClientProvider client={queryClient}>
    <DashboardPage />
  </QueryClientProvider>
);

assert.match(markup, /최근 경고 이벤트/);
assert.match(markup, /critical/);
assert.match(markup, /live_order_manual_check_still_unknown/);
assert.match(markup, /unknown_requires_manual_check/);

console.log("dashboard render fixtures passed");
