import assert from "node:assert/strict";

import {
  queryKeyPrefixesForRealtimeTable,
  realtimeQueryKeyPrefixesByTable,
  realtimeTables
} from "../src/lib/realtimeInvalidation";

const expectedPrefixes = {
  bot_settings: [["bot_settings"], ["audit_logs"]],
  worker_heartbeats: [["worker_heartbeats"]],
  api_health: [["api_health"]],
  positions: [["positions"]],
  orders: [["orders"]],
  decision_snapshots: [["decision_snapshots"]],
  ai_upgrade_candidates: [["ai_upgrade_candidates"], ["audit_logs"]],
  engine_events: [["engine_events"]]
} as const;

assert.deepEqual(realtimeQueryKeyPrefixesByTable, expectedPrefixes);
assert.deepEqual(realtimeTables, Object.keys(expectedPrefixes));

for (const table of realtimeTables) {
  assert.deepEqual(queryKeyPrefixesForRealtimeTable(table), expectedPrefixes[table]);
  for (const queryKey of queryKeyPrefixesForRealtimeTable(table)) {
    assert.equal(queryKey.length, 1);
  }
}

console.log("realtime invalidation query-key prefixes passed");
