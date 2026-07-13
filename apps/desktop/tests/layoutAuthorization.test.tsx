import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { AppLayout } from "../src/components/Layout";
import type { AuthRoleState } from "../src/lib/supabaseData";
import type { BotSettings } from "../src/lib/rows";

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
  updatedAt: "2026-07-13T00:00:00.000Z"
};

const viewerRole: AuthRoleState = {
  signedIn: true,
  email: "viewer@example.com",
  role: "viewer",
  warning: "admin role이 아니면 cockpit 데이터 접근이 제한됩니다."
};

const adminRole: AuthRoleState = {
  signedIn: true,
  email: "admin@example.com",
  role: "admin",
  warning: null
};

assert.equal(renderEmergencyStop(viewerRole).disabled, true);
assert.equal(renderEmergencyStop(adminRole).disabled, false);

console.log("layout authorization fixtures passed");

function renderEmergencyStop(role: AuthRoleState): HTMLButtonElement {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  queryClient.setQueryData(["auth_role"], role);
  queryClient.setQueryData(["bot_settings"], settings);
  queryClient.setQueryData(["worker_heartbeats", "latest"], null);
  queryClient.setQueryData(["api_health"], []);
  queryClient.setQueryData(["decision_snapshots", "today"], []);
  queryClient.setQueryData(["orders", "today"], []);
  const markup = renderToStaticMarkup(
    <QueryClientProvider client={queryClient}>
      <AppLayout page="dashboard" setPage={() => undefined}>
        <div>content</div>
      </AppLayout>
    </QueryClientProvider>
  );
  const dom = new JSDOM(markup);
  const button = Array.from(dom.window.document.querySelectorAll("button")).find((item) =>
    item.textContent?.includes("Emergency Stop")
  );
  assert.ok(button instanceof dom.window.HTMLButtonElement);
  return button;
}
