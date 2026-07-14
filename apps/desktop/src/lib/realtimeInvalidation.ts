export type RealtimeQueryKeyPrefix = readonly [string];

export const realtimeQueryKeyPrefixesByTable = {
  bot_settings: [["bot_settings"], ["audit_logs"]],
  worker_heartbeats: [["worker_heartbeats"]],
  api_health: [["api_health"]],
  positions: [["positions"]],
  orders: [["orders"]],
  decision_snapshots: [["decision_snapshots"]],
  ai_upgrade_candidates: [["ai_upgrade_candidates"], ["audit_logs"]],
  engine_events: [["engine_events"]]
} as const satisfies Readonly<Record<string, readonly RealtimeQueryKeyPrefix[]>>;

export type RealtimeTable = keyof typeof realtimeQueryKeyPrefixesByTable;

export const realtimeTables = Object.keys(realtimeQueryKeyPrefixesByTable) as RealtimeTable[];

export function queryKeyPrefixesForRealtimeTable(
  table: RealtimeTable
): readonly RealtimeQueryKeyPrefix[] {
  return realtimeQueryKeyPrefixesByTable[table];
}
