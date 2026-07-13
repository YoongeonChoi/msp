import assert from "node:assert/strict";

import {
  botSettingsSchema,
  orderStatusSchema,
  tradingModeSchema
} from "../../../packages/shared/src/index";

const defaults = botSettingsSchema.parse({});
assert.equal(defaults.enabled, false);
assert.equal(defaults.mode, "paper");
assert.equal(defaults.live_order_allowed, false);
assert.equal(defaults.deployment_lock, false);
assert.equal(defaults.deployment_target_sha, null);
assert.equal(defaults.max_order_amount_krw, 100000);

assert.equal(tradingModeSchema.safeParse("live").success, true);
assert.equal(tradingModeSchema.safeParse("simulation").success, false);
assert.equal(orderStatusSchema.safeParse("unknown_requires_manual_check").success, true);
assert.equal(orderStatusSchema.safeParse("partial_filled").success, true);
assert.equal(orderStatusSchema.safeParse("canceled").success, true);
assert.equal(botSettingsSchema.safeParse({ deployment_target_sha: "not-a-sha" }).success, false);
assert.equal(botSettingsSchema.safeParse({ max_daily_order_count: 0 }).success, false);
assert.equal(botSettingsSchema.safeParse({ max_daily_loss_pct: 0.21 }).success, false);

console.log("shared schema fixtures passed");
