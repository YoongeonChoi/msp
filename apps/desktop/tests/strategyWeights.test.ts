import assert from "node:assert/strict";

import {
  strategyWeightFormFromRecord,
  strategyWeightSumTolerance,
  validateStrategyWeightForm
} from "../src/lib/strategyWeights";

const valid = validateStrategyWeightForm({
  technical: "0.35",
  fundamental: "0.25",
  market_sector: "0.15",
  news_event: "0.15",
  portfolio: "0.10"
});
assert.equal(valid.ok, true);
if (valid.ok) {
  assert.deepEqual(valid.values, {
    technical: 0.35,
    fundamental: 0.25,
    market_sector: 0.15,
    news_event: 0.15,
    portfolio: 0.1
  });
  assert.ok(Math.abs(valid.sum - 1) <= strategyWeightSumTolerance);
}

const tolerant = validateStrategyWeightForm({
  technical: "0.3500005",
  fundamental: "0.25",
  market_sector: "0.15",
  news_event: "0.15",
  portfolio: "0.1"
});
assert.equal(tolerant.ok, true);

const invalidRange = validateStrategyWeightForm({
  technical: "1.01",
  fundamental: "0",
  market_sector: "0",
  news_event: "0",
  portfolio: "0"
});
if (invalidRange.ok) {
  assert.fail("범위를 벗어난 가중치는 거부해야 합니다.");
}
assert.match(invalidRange.message, /0에서 1 사이/);

const invalidSum = validateStrategyWeightForm({
  technical: "0.2",
  fundamental: "0.2",
  market_sector: "0.2",
  news_event: "0.2",
  portfolio: "0.1"
});
if (invalidSum.ok) {
  assert.fail("합계가 1이 아닌 가중치는 거부해야 합니다.");
}
assert.ok(invalidSum.sum !== null && Math.abs(invalidSum.sum - 0.9) < Number.EPSILON * 10);
assert.match(invalidSum.message, /합계는 1/);

const invalidBlank = validateStrategyWeightForm({
  technical: "",
  fundamental: "0.25",
  market_sector: "0.25",
  news_event: "0.25",
  portfolio: "0.25"
});
if (invalidBlank.ok) {
  assert.fail("빈 가중치는 거부해야 합니다.");
}
assert.equal(invalidBlank.sum, null);

assert.deepEqual(
  strategyWeightFormFromRecord({
    technical: 0.35,
    fundamental: 0.25,
    market_sector: 0.15,
    news_event: 0.15,
    portfolio: 0.1,
    ignored: 10
  }),
  {
    technical: "0.35",
    fundamental: "0.25",
    market_sector: "0.15",
    news_event: "0.15",
    portfolio: "0.1"
  }
);

console.log("strategy weight validation fixtures passed");
