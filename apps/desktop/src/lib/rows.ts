import {
  booleanValue,
  integerValue,
  nullableString,
  numberValue,
  recordValue,
  stringValue
} from "./formatters";

export interface BotSettings {
  readonly id: string;
  readonly enabled: boolean;
  readonly mode: "paper" | "live";
  readonly liveOrderAllowed: boolean;
  readonly maxOrderAmountKrw: number;
  readonly maxDailyLossPct: number;
  readonly maxDailyOrderCount: number;
  readonly maxPositionPct: number;
  readonly maxSectorPct: number;
  readonly loopIntervalSec: number;
  readonly updatedAt: string | null;
}

export interface WorkerHeartbeat {
  readonly id: string;
  readonly status: string;
  readonly memoryMb: number | null;
  readonly lastLoopMs: number | null;
  readonly message: string | null;
  readonly createdAt: string | null;
}

export interface ApiHealth {
  readonly id: string;
  readonly provider: string;
  readonly healthy: boolean;
  readonly status: string;
  readonly latencyMs: number | null;
  readonly message: string | null;
  readonly errorCode: string | null;
  readonly checkedAt: string | null;
}

export interface WatchlistItem {
  readonly id: string;
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
  readonly updatedAt: string | null;
}

export interface PositionRow {
  readonly id: string;
  readonly symbol: string;
  readonly quantity: number;
  readonly avgPriceKrw: number;
  readonly currentPriceKrw: number;
  readonly marketValueKrw: number;
  readonly unrealizedPnlKrw: number;
  readonly unrealizedPnlPct: number;
  readonly sector: string;
  readonly syncedAt: string | null;
}

export interface DecisionSnapshot {
  readonly id: string;
  readonly symbol: string;
  readonly action: string;
  readonly finalScore: number | null;
  readonly confidence: number | null;
  readonly featureSnapshot: unknown;
  readonly riskSnapshot: unknown;
  readonly decidedAt: string | null;
}

export interface OrderRow {
  readonly id: string;
  readonly symbol: string;
  readonly side: string;
  readonly mode: string;
  readonly status: string;
  readonly amountKrw: number | null;
  readonly quantity: number | null;
  readonly priceKrw: number | null;
  readonly idempotencyKey: string | null;
  readonly providerOrderId: string | null;
  readonly reason: string | null;
  readonly reasonJson: unknown;
  readonly riskSnapshotJson: unknown;
  readonly createdAt: string | null;
}

export interface NewsEventRow {
  readonly id: string;
  readonly symbol: string;
  readonly title: string;
  readonly source: string;
  readonly sentiment: string | null;
  readonly eventType: string | null;
  readonly riskLevel: string | null;
  readonly summaryShort: string | null;
  readonly publishedAt: string | null;
  readonly createdAt: string | null;
}

export interface FundamentalRow {
  readonly id: string;
  readonly symbol: string;
  readonly fiscalYear: number | null;
  readonly fiscalQuarter: number | null;
  readonly per: number | null;
  readonly pbr: number | null;
  readonly roe: number | null;
  readonly operatingMargin: number | null;
  readonly debtRatio: number | null;
  readonly rawSnapshot: unknown;
  readonly updatedAt: string | null;
}

export interface EngineEventRow {
  readonly id: string;
  readonly level: string;
  readonly component: string;
  readonly message: string;
  readonly details: unknown;
  readonly createdAt: string | null;
}

export interface StrategyVersionRow {
  readonly id: string;
  readonly version: string;
  readonly versionName: string;
  readonly status: string;
  readonly strategyType: string;
  readonly weightsJson: Record<string, unknown>;
  readonly paramsJson: Record<string, unknown>;
  readonly approvedAt: string | null;
  readonly deployedAt: string | null;
  readonly createdAt: string | null;
}

export interface OutcomeRow {
  readonly id: string;
  readonly decisionId: string;
  readonly orderId: string | null;
  readonly symbol: string;
  readonly return1d: number | null;
  readonly return5d: number | null;
  readonly return20d: number | null;
  readonly maxDrawdown20d: number | null;
  readonly hitTarget: boolean | null;
  readonly hitStop: boolean | null;
  readonly realizedPnlKrw: number | null;
  readonly outcomeStatus: string;
  readonly updatedAt: string | null;
  readonly calculatedAt: string | null;
}

export interface BacktestRunRow {
  readonly id: string;
  readonly strategy: string;
  readonly strategyVersion: string;
  readonly periodStart: string | null;
  readonly periodEnd: string | null;
  readonly totalReturn: number | null;
  readonly cagr: number | null;
  readonly maxDrawdown: number | null;
  readonly sharpeLike: number | null;
  readonly winRate: number | null;
  readonly averageWin: number | null;
  readonly averageLoss: number | null;
  readonly turnover: number | null;
  readonly numberOfTrades: number;
  readonly transactionCostKrw: number | null;
  readonly blockedReasonCounts: Record<string, unknown>;
  readonly assumptions: Record<string, unknown>;
  readonly createdAt: string | null;
}

export interface AiUpgradeCandidateRow {
  readonly id: string;
  readonly baseStrategyVersionId: string | null;
  readonly candidateName: string;
  readonly candidateWeights: Record<string, unknown>;
  readonly candidateParams: Record<string, unknown>;
  readonly rationale: string;
  readonly expectedImprovement: string;
  readonly riskNotes: string;
  readonly requiredBacktests: readonly string[];
  readonly status: string;
  readonly approvalRequired: boolean;
  readonly createdAt: string | null;
  readonly reviewedAt: string | null;
}

export function mapBotSettings(value: unknown): BotSettings {
  const row = recordValue(value);
  return {
    id: stringValue(row.id, "singleton"),
    enabled: booleanValue(row.enabled),
    mode: stringValue(row.mode, "paper") === "live" ? "live" : "paper",
    liveOrderAllowed: booleanValue(row.live_order_allowed),
    maxOrderAmountKrw: integerValue(row.max_order_amount_krw) ?? 100000,
    maxDailyLossPct: numberValue(row.max_daily_loss_pct) ?? 0.02,
    maxDailyOrderCount: integerValue(row.max_daily_order_count) ?? 10,
    maxPositionPct: numberValue(row.max_position_pct) ?? 0.1,
    maxSectorPct: numberValue(row.max_sector_pct) ?? 0.3,
    loopIntervalSec: integerValue(row.loop_interval_sec) ?? 30,
    updatedAt: nullableString(row.updated_at)
  };
}

export function mapHeartbeat(value: unknown): WorkerHeartbeat {
  const row = recordValue(value);
  return {
    id: stringValue(row.id),
    status: stringValue(row.status, "unknown"),
    memoryMb: numberValue(row.memory_mb),
    lastLoopMs: integerValue(row.last_loop_ms),
    message: nullableString(row.message),
    createdAt: nullableString(row.created_at)
  };
}

export function mapApiHealth(value: unknown): ApiHealth {
  const row = recordValue(value);
  return {
    id: stringValue(row.id),
    provider: stringValue(row.provider, "unknown"),
    healthy: booleanValue(row.healthy),
    status: stringValue(row.status, "unknown"),
    latencyMs: integerValue(row.latency_ms),
    message: nullableString(row.message),
    errorCode: nullableString(row.error_code),
    checkedAt: nullableString(row.checked_at)
  };
}

export function mapWatchlistItem(value: unknown): WatchlistItem {
  const row = recordValue(value);
  return {
    id: stringValue(row.id),
    symbol: stringValue(row.symbol),
    name: nullableString(row.name),
    market: stringValue(row.market, "KR"),
    sector: stringValue(row.sector, "unknown"),
    enabled: booleanValue(row.enabled, true),
    targetBuyKrw: integerValue(row.target_buy_krw),
    targetSellKrw: integerValue(row.target_sell_krw),
    stopLossPct: numberValue(row.stop_loss_pct),
    maxPositionPct: numberValue(row.max_position_pct),
    notes: nullableString(row.notes),
    updatedAt: nullableString(row.updated_at)
  };
}

export function mapPosition(value: unknown): PositionRow {
  const row = recordValue(value);
  return {
    id: stringValue(row.id),
    symbol: stringValue(row.symbol),
    quantity: integerValue(row.quantity) ?? 0,
    avgPriceKrw: integerValue(row.avg_price_krw) ?? 0,
    currentPriceKrw: integerValue(row.current_price_krw) ?? 0,
    marketValueKrw: integerValue(row.market_value_krw) ?? 0,
    unrealizedPnlKrw: integerValue(row.unrealized_pnl_krw) ?? 0,
    unrealizedPnlPct: numberValue(row.unrealized_pnl_pct) ?? 0,
    sector: stringValue(row.sector, "unknown"),
    syncedAt: nullableString(row.synced_at)
  };
}

export function mapDecision(value: unknown): DecisionSnapshot {
  const row = recordValue(value);
  return {
    id: stringValue(row.id),
    symbol: stringValue(row.symbol),
    action: stringValue(row.action, "hold"),
    finalScore: numberValue(row.final_score),
    confidence: numberValue(row.confidence),
    featureSnapshot: row.feature_snapshot,
    riskSnapshot: row.risk_snapshot,
    decidedAt: nullableString(row.decided_at) ?? nullableString(row.created_at)
  };
}

export function mapOrder(value: unknown): OrderRow {
  const row = recordValue(value);
  return {
    id: stringValue(row.id),
    symbol: stringValue(row.symbol),
    side: stringValue(row.side),
    mode: stringValue(row.mode),
    status: stringValue(row.status),
    amountKrw: integerValue(row.amount_krw),
    quantity: integerValue(row.quantity),
    priceKrw: integerValue(row.price_krw) ?? integerValue(row.price),
    idempotencyKey: nullableString(row.idempotency_key),
    providerOrderId: nullableString(row.provider_order_id),
    reason: nullableString(row.reason),
    reasonJson: row.reason_json ?? row.provider_payload_summary ?? row.reason,
    riskSnapshotJson: row.risk_snapshot_json ?? row.risk_result,
    createdAt: nullableString(row.created_at)
  };
}

export function mapNewsEvent(value: unknown): NewsEventRow {
  const row = recordValue(value);
  return {
    id: stringValue(row.id),
    symbol: stringValue(row.symbol),
    title: stringValue(row.title),
    source: stringValue(row.source),
    sentiment: nullableString(row.sentiment),
    eventType: nullableString(row.event_type),
    riskLevel: nullableString(row.risk_level),
    summaryShort: nullableString(row.summary_short),
    publishedAt: nullableString(row.published_at),
    createdAt: nullableString(row.created_at)
  };
}

export function mapFundamental(value: unknown): FundamentalRow {
  const row = recordValue(value);
  return {
    id: stringValue(row.id),
    symbol: stringValue(row.symbol),
    fiscalYear: integerValue(row.fiscal_year),
    fiscalQuarter: integerValue(row.fiscal_quarter),
    per: numberValue(row.per),
    pbr: numberValue(row.pbr),
    roe: numberValue(row.roe),
    operatingMargin: numberValue(row.operating_margin),
    debtRatio: numberValue(row.debt_ratio),
    rawSnapshot: row.raw_snapshot,
    updatedAt: nullableString(row.updated_at)
  };
}

export function mapEngineEvent(value: unknown): EngineEventRow {
  const row = recordValue(value);
  return {
    id: stringValue(row.id),
    level: stringValue(row.level, "info"),
    component: stringValue(row.component),
    message: stringValue(row.message),
    details: row.details,
    createdAt: nullableString(row.created_at)
  };
}

export function mapStrategyVersion(value: unknown): StrategyVersionRow {
  const row = recordValue(value);
  const version = stringValue(row.version) || stringValue(row.version_name);
  return {
    id: stringValue(row.id),
    version: version || "-",
    versionName: stringValue(row.version_name, version || "-"),
    status: stringValue(row.status, "unknown"),
    strategyType: stringValue(row.strategy_type, "unknown"),
    weightsJson: recordValue(row.weights_json ?? row.weights),
    paramsJson: recordValue(row.params_json ?? row.params),
    approvedAt: nullableString(row.approved_at),
    deployedAt: nullableString(row.deployed_at),
    createdAt: nullableString(row.created_at)
  };
}

export function mapOutcome(value: unknown): OutcomeRow {
  const row = recordValue(value);
  return {
    id: stringValue(row.id),
    decisionId: stringValue(row.decision_id),
    orderId: nullableString(row.order_id),
    symbol: stringValue(row.symbol),
    return1d: numberValue(row.return_1d),
    return5d: numberValue(row.return_5d),
    return20d: numberValue(row.return_20d) ?? numberValue(row.return_pct),
    maxDrawdown20d: numberValue(row.max_drawdown_20d),
    hitTarget: typeof row.hit_target === "boolean" ? row.hit_target : null,
    hitStop: typeof row.hit_stop === "boolean" ? row.hit_stop : null,
    realizedPnlKrw: integerValue(row.realized_pnl_krw) ?? integerValue(row.pnl_krw),
    outcomeStatus: stringValue(row.outcome_status, "unknown"),
    updatedAt: nullableString(row.updated_at),
    calculatedAt: nullableString(row.calculated_at)
  };
}

export function mapBacktestRun(value: unknown): BacktestRunRow {
  const row = recordValue(value);
  return {
    id: stringValue(row.id),
    strategy: stringValue(row.strategy),
    strategyVersion: stringValue(row.strategy_version),
    periodStart: nullableString(row.period_start),
    periodEnd: nullableString(row.period_end),
    totalReturn: numberValue(row.total_return),
    cagr: numberValue(row.cagr),
    maxDrawdown: numberValue(row.max_drawdown),
    sharpeLike: numberValue(row.sharpe_like),
    winRate: numberValue(row.win_rate),
    averageWin: numberValue(row.average_win),
    averageLoss: numberValue(row.average_loss),
    turnover: numberValue(row.turnover),
    numberOfTrades: integerValue(row.number_of_trades) ?? 0,
    transactionCostKrw: integerValue(row.transaction_cost_krw),
    blockedReasonCounts: recordValue(row.blocked_reason_counts),
    assumptions: recordValue(row.assumptions),
    createdAt: nullableString(row.created_at)
  };
}

export function mapAiUpgradeCandidate(value: unknown): AiUpgradeCandidateRow {
  const row = recordValue(value);
  return {
    id: stringValue(row.id),
    baseStrategyVersionId: nullableString(row.base_strategy_version_id),
    candidateName: stringValue(row.candidate_name),
    candidateWeights: recordValue(row.candidate_weights),
    candidateParams: recordValue(row.candidate_params),
    rationale: stringValue(row.rationale),
    expectedImprovement: stringValue(row.expected_improvement),
    riskNotes: stringValue(row.risk_notes),
    requiredBacktests: stringArray(row.required_backtests),
    status: stringValue(row.status, "proposed"),
    approvalRequired: booleanValue(row.approval_required, true),
    createdAt: nullableString(row.created_at),
    reviewedAt: nullableString(row.reviewed_at)
  };
}

function stringArray(value: unknown): readonly string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map((item) => stringValue(item)).filter((item) => item.length > 0);
}
