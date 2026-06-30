import { hasSupabaseConfig, supabase } from "./supabaseClient";
import { isRecord, startOfKstTodayIso, stringValue } from "./formatters";
import {
  mapApiHealth,
  mapAiUpgradeCandidate,
  mapBacktestRun,
  mapBotSettings,
  mapDecision,
  mapEngineEvent,
  mapFundamental,
  mapHeartbeat,
  mapManualCommand,
  mapNewsEvent,
  mapOrder,
  mapOutcome,
  mapPosition,
  mapStrategyVersion,
  mapWatchlistItem
} from "./rows";
import type {
  AiUpgradeCandidateRow,
  ApiHealth,
  BacktestRunRow,
  BotSettings,
  DecisionSnapshot,
  EngineEventRow,
  FundamentalRow,
  ManualCommandRow,
  NewsEventRow,
  OrderRow,
  OutcomeRow,
  PositionRow,
  StrategyVersionRow,
  WatchlistItem,
  WorkerHeartbeat
} from "./rows";

export class CockpitDataError extends Error {
  readonly table: string;

  constructor(table: string, message: string) {
    super(message);
    this.name = "CockpitDataError";
    this.table = table;
  }
}

export interface WatchlistInput {
  readonly symbol: string;
  readonly name: string | null;
  readonly market: string;
  readonly sector: string;
  readonly enabled: boolean;
  readonly targetBuyKrw: number | null;
  readonly targetSellKrw: number | null;
  readonly stopLossPct: number | null;
  readonly maxPositionPct: number | null;
  readonly notes: string | null;
}

export interface BotSettingsPatch {
  readonly enabled?: boolean;
  readonly mode?: "paper" | "live";
  readonly liveOrderAllowed?: boolean;
  readonly maxOrderAmountKrw?: number;
  readonly maxDailyLossPct?: number;
  readonly maxDailyOrderCount?: number;
  readonly maxPositionPct?: number;
  readonly maxSectorPct?: number;
}

export interface AuthRoleState {
  readonly signedIn: boolean;
  readonly email: string | null;
  readonly role: string | null;
  readonly warning: string | null;
}

export interface AuthCredentials {
  readonly email: string;
  readonly password: string;
}

export interface OptionalBacktestRuns {
  readonly available: boolean;
  readonly warning: string | null;
  readonly rows: readonly BacktestRunRow[];
}

export interface StrategyJsonPatch {
  readonly id: string;
  readonly weightsJson: Record<string, unknown>;
  readonly paramsJson: Record<string, unknown>;
}

export type AiCandidateReviewStatus = "approved_for_paper" | "rejected";

export interface LiveEnableRequestInput {
  readonly providerContractVersion: string;
  readonly riskReportId: string;
  readonly releaseVersion: string;
  readonly expiresInMinutes: number;
}

export interface LiveEnableReviewInput {
  readonly id: string;
  readonly status: "accepted" | "rejected";
  readonly rejectionReason?: string;
}

export function isSupabaseReady(): boolean {
  return hasSupabaseConfig && supabase !== null;
}

export async function fetchBotSettings(): Promise<BotSettings | null> {
  const client = requireClient();
  const result = await client.from("bot_settings").select("*").eq("id", "singleton").maybeSingle();
  failOnError("bot_settings", result.error);
  return result.data ? mapBotSettings(result.data) : null;
}

export async function fetchLatestHeartbeat(): Promise<WorkerHeartbeat | null> {
  const client = requireClient();
  const result = await client
    .from("worker_heartbeats")
    .select("*")
    .order("created_at", { ascending: false })
    .limit(1);
  failOnError("worker_heartbeats", result.error);
  return result.data?.[0] ? mapHeartbeat(result.data[0]) : null;
}

export async function fetchApiHealth(): Promise<ApiHealth[]> {
  const client = requireClient();
  const result = await client
    .from("api_health")
    .select("*")
    .order("provider", { ascending: true })
    .order("checked_at", { ascending: false })
    .limit(100);
  failOnError("api_health", result.error);
  const latestByProvider = new Map<string, ApiHealth>();
  for (const item of (result.data ?? []).map(mapApiHealth)) {
    const key = item.provider.toLowerCase();
    if (!latestByProvider.has(key)) {
      latestByProvider.set(key, item);
    }
  }
  return Array.from(latestByProvider.values());
}

export async function fetchTodayDecisions(): Promise<DecisionSnapshot[]> {
  const client = requireClient();
  const result = await client
    .from("decision_snapshots")
    .select("*")
    .gte("created_at", startOfKstTodayIso())
    .order("created_at", { ascending: false })
    .limit(500);
  failOnError("decision_snapshots", result.error);
  return (result.data ?? []).map(mapDecision);
}

export async function fetchRecentDecisions(limit = 50): Promise<DecisionSnapshot[]> {
  const client = requireClient();
  const result = await client
    .from("decision_snapshots")
    .select("*")
    .order("created_at", { ascending: false })
    .limit(limit);
  failOnError("decision_snapshots", result.error);
  return (result.data ?? []).map(mapDecision);
}

export async function fetchTodayOrders(): Promise<OrderRow[]> {
  const client = requireClient();
  const result = await client
    .from("orders")
    .select("*")
    .gte("created_at", startOfKstTodayIso())
    .order("created_at", { ascending: false })
    .limit(500);
  failOnError("orders", result.error);
  return (result.data ?? []).map(mapOrder);
}

export async function fetchRecentOrders(limit = 80): Promise<OrderRow[]> {
  const client = requireClient();
  const result = await client.from("orders").select("*").order("created_at", { ascending: false }).limit(limit);
  failOnError("orders", result.error);
  return (result.data ?? []).map(mapOrder);
}

export async function fetchWatchlist(): Promise<WatchlistItem[]> {
  const client = requireClient();
  const result = await client.from("watchlist").select("*").order("symbol", { ascending: true });
  failOnError("watchlist", result.error);
  return (result.data ?? []).map(mapWatchlistItem);
}

export async function upsertWatchlistItem(input: WatchlistInput): Promise<void> {
  const client = requireClient();
  const payload = {
    symbol: input.symbol,
    name: input.name,
    market: input.market,
    sector: input.sector,
    enabled: input.enabled,
    target_buy_krw: input.targetBuyKrw,
    target_sell_krw: input.targetSellKrw,
    stop_loss_pct: input.stopLossPct,
    max_position_pct: input.maxPositionPct,
    notes: input.notes,
    updated_at: new Date().toISOString()
  };
  const result = await client.from("watchlist").upsert(payload, { onConflict: "symbol,market" });
  failOnError("watchlist", result.error);
}

export async function fetchPositions(): Promise<PositionRow[]> {
  const client = requireClient();
  const result = await client.from("positions").select("*").order("market_value_krw", { ascending: false }).limit(100);
  failOnError("positions", result.error);
  return (result.data ?? []).map(mapPosition);
}

export async function fetchNewsEvents(): Promise<NewsEventRow[]> {
  const client = requireClient();
  const result = await client.from("news_events").select("*").order("created_at", { ascending: false }).limit(80);
  failOnError("news_events", result.error);
  return (result.data ?? []).map(mapNewsEvent);
}

export async function fetchFundamentals(): Promise<FundamentalRow[]> {
  const client = requireClient();
  const result = await client
    .from("fundamentals_quarterly")
    .select("*")
    .order("updated_at", { ascending: false })
    .limit(80);
  failOnError("fundamentals_quarterly", result.error);
  return (result.data ?? []).map(mapFundamental);
}

export async function fetchEngineEvents(limit = 50): Promise<EngineEventRow[]> {
  const client = requireClient();
  const result = await client
    .from("engine_events")
    .select("*")
    .order("created_at", { ascending: false })
    .limit(limit);
  failOnError("engine_events", result.error);
  return (result.data ?? []).map(mapEngineEvent);
}

export async function fetchStrategyVersions(limit = 20): Promise<StrategyVersionRow[]> {
  const client = requireClient();
  const result = await client
    .from("strategy_versions")
    .select("*")
    .order("created_at", { ascending: false })
    .limit(limit);
  failOnError("strategy_versions", result.error);
  return (result.data ?? []).map(mapStrategyVersion);
}

export async function fetchRecentOutcomes(limit = 100): Promise<OutcomeRow[]> {
  const client = requireClient();
  const result = await client
    .from("outcomes")
    .select("*")
    .order("updated_at", { ascending: false })
    .limit(limit);
  failOnError("outcomes", result.error);
  return (result.data ?? []).map(mapOutcome);
}

export async function fetchRecentBacktestRuns(limit = 20): Promise<OptionalBacktestRuns> {
  const client = requireClient();
  const result = await client
    .from("backtest_runs")
    .select("*")
    .order("created_at", { ascending: false })
    .limit(limit);
  if (result.error) {
    if (isOptionalTableUnavailable(result.error)) {
      return {
        available: false,
        warning: "backtest_runs 테이블 또는 admin read policy가 아직 적용되지 않았습니다.",
        rows: []
      };
    }
    failOnError("backtest_runs", result.error);
  }
  return {
    available: true,
    warning: null,
    rows: (result.data ?? []).map(mapBacktestRun)
  };
}

export async function fetchAiUpgradeCandidates(limit = 50): Promise<AiUpgradeCandidateRow[]> {
  const client = requireClient();
  const result = await client
    .from("ai_upgrade_candidates")
    .select("*")
    .order("created_at", { ascending: false })
    .limit(limit);
  failOnError("ai_upgrade_candidates", result.error);
  return (result.data ?? []).map(mapAiUpgradeCandidate);
}

export async function fetchLiveEnableCommands(limit = 10): Promise<ManualCommandRow[]> {
  const client = requireClient();
  const result = await client
    .from("manual_commands")
    .select("*")
    .eq("command_type", "request_live_enable")
    .order("created_at", { ascending: false })
    .limit(limit);
  failOnError("manual_commands", result.error);
  return (result.data ?? []).map(mapManualCommand);
}

export async function fetchCurrentUserId(): Promise<string | null> {
  const client = requireClient();
  const userResult = await client.auth.getUser();
  if (userResult.error || !userResult.data.user) {
    return null;
  }
  return userResult.data.user.id;
}

export async function requestLiveEnable(input: LiveEnableRequestInput): Promise<void> {
  const client = requireClient();
  const userId = await requireUserId();
  const expiresAt = new Date(Date.now() + input.expiresInMinutes * 60_000).toISOString();
  const result = await client.from("manual_commands").insert({
    command_type: "request_live_enable",
    payload: {
      provider_contract_version: input.providerContractVersion,
      risk_report_id: input.riskReportId,
      release_version: input.releaseVersion
    },
    requested_by: userId,
    expires_at: expiresAt,
    status: "pending"
  });
  failOnError("manual_commands", result.error);
}

export async function reviewLiveEnableCommand(input: LiveEnableReviewInput): Promise<void> {
  const client = requireClient();
  const userId = await requireUserId();
  const result = await client
    .from("manual_commands")
    .update({
      status: input.status,
      reviewed_by: userId,
      reviewed_at: new Date().toISOString(),
      rejection_reason: input.status === "rejected" ? input.rejectionReason ?? "rejected" : null
    })
    .eq("id", input.id)
    .eq("command_type", "request_live_enable")
    .eq("status", "pending")
    .select("id");
  failOnError("manual_commands", result.error);
  if ((result.data ?? []).length !== 1) {
    throw new CockpitDataError("manual_commands", "검토할 pending Live 승인 요청을 찾지 못했습니다.");
  }
}

export async function updateDraftStrategyJson(input: StrategyJsonPatch): Promise<void> {
  const client = requireClient();
  const result = await client
    .from("strategy_versions")
    .update({ weights: input.weightsJson, params: input.paramsJson })
    .eq("id", input.id)
    .in("status", ["draft", "proposed"]);
  failOnError("strategy_versions", result.error);
}

export async function reviewAiUpgradeCandidate(input: {
  readonly id: string;
  readonly status: AiCandidateReviewStatus;
}): Promise<void> {
  const client = requireClient();
  const result = await client
    .from("ai_upgrade_candidates")
    .update({ status: input.status, reviewed_at: new Date().toISOString() })
    .eq("id", input.id);
  failOnError("ai_upgrade_candidates", result.error);
}

export async function updateBotSettings(patch: BotSettingsPatch): Promise<void> {
  const client = requireClient();
  const payload = {
    ...(patch.enabled !== undefined ? { enabled: patch.enabled } : {}),
    ...(patch.mode !== undefined ? { mode: patch.mode } : {}),
    ...(patch.liveOrderAllowed !== undefined ? { live_order_allowed: patch.liveOrderAllowed } : {}),
    ...(patch.maxOrderAmountKrw !== undefined ? { max_order_amount_krw: patch.maxOrderAmountKrw } : {}),
    ...(patch.maxDailyLossPct !== undefined ? { max_daily_loss_pct: patch.maxDailyLossPct } : {}),
    ...(patch.maxDailyOrderCount !== undefined ? { max_daily_order_count: patch.maxDailyOrderCount } : {}),
    ...(patch.maxPositionPct !== undefined ? { max_position_pct: patch.maxPositionPct } : {}),
    ...(patch.maxSectorPct !== undefined ? { max_sector_pct: patch.maxSectorPct } : {}),
    updated_at: new Date().toISOString()
  };
  const result = await client.from("bot_settings").update(payload).eq("id", "singleton");
  failOnError("bot_settings", result.error);
}

export async function fetchAuthRole(): Promise<AuthRoleState> {
  const client = requireClient();
  const userResult = await client.auth.getUser();
  if (userResult.error || !userResult.data.user) {
    return {
      signedIn: false,
      email: null,
      role: null,
      warning: "Supabase Auth 로그인 세션이 필요합니다."
    };
  }
  const user = userResult.data.user;
  const roleResult = await client.from("user_roles").select("role").eq("user_id", user.id).maybeSingle();
  if (roleResult.error) {
    return {
      signedIn: true,
      email: user.email ?? null,
      role: null,
      warning: "RLS 또는 admin role 설정 때문에 역할을 읽을 수 없습니다."
    };
  }
  const role = isRecord(roleResult.data) ? stringValue(roleResult.data.role, "") : "";
  return {
    signedIn: true,
    email: user.email ?? null,
    role: role || null,
    warning: role === "admin" ? null : "admin role이 아니면 cockpit 데이터 접근이 제한됩니다."
  };
}

export async function signInWithPassword(input: AuthCredentials): Promise<void> {
  const client = requireClient();
  const result = await client.auth.signInWithPassword({
    email: input.email,
    password: input.password
  });
  failOnError("auth", result.error);
}

export async function signOut(): Promise<void> {
  const client = requireClient();
  const result = await client.auth.signOut();
  failOnError("auth", result.error);
}

function requireClient() {
  if (!isSupabaseReady() || supabase === null) {
    throw new CockpitDataError("supabase", "Supabase URL 또는 publishable key가 설정되지 않았습니다.");
  }
  return supabase;
}

function failOnError(table: string, error: unknown): void {
  if (!error) {
    return;
  }
  if (isRecord(error)) {
    throw new CockpitDataError(table, stringValue(error.message, `${table} query failed`));
  }
  throw new CockpitDataError(table, `${table} query failed`);
}

async function requireUserId(): Promise<string> {
  const client = requireClient();
  const userResult = await client.auth.getUser();
  if (userResult.error || !userResult.data.user) {
    throw new CockpitDataError("auth", "Supabase Auth 로그인 세션이 필요합니다.");
  }
  return userResult.data.user.id;
}

function isOptionalTableUnavailable(error: unknown): boolean {
  if (!isRecord(error)) {
    return false;
  }
  const code = stringValue(error.code);
  const message = stringValue(error.message).toLowerCase();
  return (
    code === "42P01" ||
    code === "PGRST205" ||
    message.includes("does not exist") ||
    message.includes("could not find") ||
    message.includes("permission denied")
  );
}
