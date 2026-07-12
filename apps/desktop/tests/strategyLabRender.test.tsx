import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { StrategyLabPage } from "../src/pages/StrategyLabPage";
import type { AuthRoleState } from "../src/lib/supabaseData";
import type { StrategyVersionRow } from "../src/lib/rows";

const baseWeights = {
  technical: 0.35,
  fundamental: 0.25,
  market_sector: 0.15,
  news_event: 0.15,
  portfolio: 0.1
};

function strategy(id: string, version: string, status: string): StrategyVersionRow {
  return {
    id,
    version,
    versionName: version,
    status,
    strategyType: "weighted_factor_v1",
    weightsJson: baseWeights,
    paramsJson: { buy_threshold: 0.7, sell_threshold: 0.3 },
    approvedAt: null,
    deployedAt: null,
    createdAt: "2026-07-12T00:00:00.000Z"
  };
}

const adminRole: AuthRoleState = {
  signedIn: true,
  email: "admin@example.com",
  role: "admin",
  warning: null
};
const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: false }
  }
});
queryClient.setQueryData(["auth_role"], adminRole);
queryClient.setQueryData(
  ["strategy_versions", "strategy_lab"],
  [
    strategy("paper-id", "paper-v1", "paper"),
    strategy("draft-id", "draft-v2", "draft"),
    strategy("proposed-id", "proposed-v3", "proposed"),
    strategy("retired-id", "retired-v4", "retired")
  ]
);
queryClient.setQueryData(["outcomes", "strategy_lab"], []);
queryClient.setQueryData(["orders", "strategy_lab"], []);
queryClient.setQueryData(["backtest_runs", "strategy_lab"], { available: true, warning: null, rows: [] });
queryClient.setQueryData(["ai_upgrade_candidates", "strategy_lab"], []);

const markup = renderToStaticMarkup(
  <QueryClientProvider client={queryClient}>
    <StrategyLabPage />
  </QueryClientProvider>
);

assert.match(markup, /현재 전략/);
assert.match(markup, /paper-v1/);
assert.match(markup, /읽기 전용/);
assert.match(markup, /Draft 가중치 편집/);
assert.match(markup, /draft-v2 · draft/);
assert.doesNotMatch(markup, /proposed-v3/);
assert.doesNotMatch(markup, /retired-v4/);
assert.match(markup, /기술적 분석/);
assert.match(markup, /펀더멘털/);
assert.match(markup, /시장·섹터/);
assert.match(markup, /뉴스·이벤트/);
assert.match(markup, /포트폴리오/);
assert.match(markup, /합계 1.000000 \/ 1.000000/);
assert.equal(markup.match(/<input type="number"/g)?.length ?? 0, 5, "다섯 factor를 각각 숫자 입력으로 제공해야 합니다.");
assert.equal(markup.match(/<textarea/g)?.length ?? 0, 1, "weights는 textarea가 아닌 구조화 입력이어야 합니다.");
assert.match(markup, /<button[^>]*disabled=""[^>]*>.*Draft 저장.*<\/button>/);

console.log("strategy lab draft editor render fixtures passed");
