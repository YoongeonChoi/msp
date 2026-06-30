import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ControlPage } from "../src/pages/ControlPage";
import { FundamentalsPage } from "../src/pages/FundamentalsPage";
import { LogsPage } from "../src/pages/LogsPage";
import { NewsPage } from "../src/pages/NewsPage";
import { OrdersPage } from "../src/pages/OrdersPage";
import { PortfolioPage } from "../src/pages/PortfolioPage";
import { SettingsPage } from "../src/pages/SettingsPage";
import { SignalsPage } from "../src/pages/SignalsPage";
import { StrategyLabPage } from "../src/pages/StrategyLabPage";
import { WatchlistPage } from "../src/pages/WatchlistPage";
import type { AuthRoleState } from "../src/lib/supabaseData";

const loggedOutRole: AuthRoleState = {
  signedIn: false,
  email: null,
  role: null,
  warning: "Supabase Auth 로그인 세션이 필요합니다."
};

const authRequiredCases: Array<readonly [string, React.ReactElement, Array<readonly [readonly unknown[], unknown]>]> = [
  ["portfolio", <PortfolioPage />, [[["positions"], []]]],
  ["orders", <OrdersPage />, [[["orders", "recent"], []]]],
  ["signals", <SignalsPage />, [[["decision_snapshots", "recent"], []]]],
  ["fundamentals", <FundamentalsPage />, [[["fundamentals_quarterly"], []]]],
  ["news", <NewsPage />, [[["news_events"], []]]],
  ["logs", <LogsPage />, [[["engine_events"], []]]],
  ["watchlist", <WatchlistPage />, [[["watchlist"], []]]],
  [
    "strategy",
    <StrategyLabPage />,
    [
      [["strategy_versions", "strategy_lab"], []],
      [["outcomes", "strategy_lab"], []],
      [["orders", "strategy_lab"], []],
      [["backtest_runs", "strategy_lab"], { available: true, warning: null, rows: [] }],
      [["ai_upgrade_candidates", "strategy_lab"], []]
    ]
  ],
  [
    "control",
    <ControlPage />,
    [
      [["bot_settings"], null],
      [["manual_commands", "request_live_enable"], []],
      [["auth", "current_user_id"], null]
    ]
  ]
];

for (const [label, element, seeds] of authRequiredCases) {
  const markup = renderWithSeededQueries(element, seeds);
  assert.match(markup, /admin 권한 필요/, `${label} should show auth-required state`);
  assert.match(markup, /Settings에서 admin 계정으로 로그인/, `${label} should point to Settings login`);
}

const settingsMarkup = renderWithSeededQueries(<SettingsPage />, [[["bot_settings"], null]]);
assert.match(settingsMarkup, /bot_settings 접근/);
assert.match(settingsMarkup, /권한 필요/);
assert.match(settingsMarkup, /Supabase Auth 로그인/);

console.log("auth RLS visibility fixtures passed");

function renderWithSeededQueries(
  element: React.ReactElement,
  seeds: Array<readonly [readonly unknown[], unknown]>
): string {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false
      }
    }
  });
  queryClient.setQueryData(["auth_role"], loggedOutRole);
  for (const [key, value] of seeds) {
    queryClient.setQueryData(key, value);
  }
  return renderToStaticMarkup(<QueryClientProvider client={queryClient}>{element}</QueryClientProvider>);
}
