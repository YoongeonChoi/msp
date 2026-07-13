import assert from "node:assert/strict";

import { parseWatchlistForm } from "../src/pages/WatchlistPage";
import type { WatchlistForm } from "../src/pages/WatchlistPage";

const validForm: WatchlistForm = {
  symbol: "005930",
  name: " 삼성전자 ",
  market: "KR",
  sector: " 반도체 ",
  enabled: true,
  targetBuyKrw: "70000",
  targetSellKrw: "85000",
  stopLossPct: "5",
  maxPositionPct: "10",
  notes: " 장기 관찰 "
};

assert.deepEqual(parseWatchlistForm(validForm), {
  symbol: "005930",
  name: "삼성전자",
  market: "KR",
  sector: "반도체",
  enabled: true,
  targetBuyKrw: 70000,
  targetSellKrw: 85000,
  stopLossPct: 0.05,
  maxPositionPct: 0.1,
  notes: "장기 관찰"
});

assert.equal(parseWatchlistForm({ ...validForm, targetBuyKrw: "not-a-number" }), null);
assert.equal(parseWatchlistForm({ ...validForm, targetSellKrw: "85000.5" }), null);
assert.equal(parseWatchlistForm({ ...validForm, stopLossPct: "invalid" }), null);
assert.equal(parseWatchlistForm({ ...validForm, maxPositionPct: "Infinity" }), null);

assert.deepEqual(parseWatchlistForm({
  ...validForm,
  targetBuyKrw: "",
  targetSellKrw: " ",
  stopLossPct: "",
  maxPositionPct: ""
}), {
  symbol: "005930",
  name: "삼성전자",
  market: "KR",
  sector: "반도체",
  enabled: true,
  targetBuyKrw: null,
  targetSellKrw: null,
  stopLossPct: null,
  maxPositionPct: null,
  notes: "장기 관찰"
});

console.log("watchlist validation fixtures passed");
