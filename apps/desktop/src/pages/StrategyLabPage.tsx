import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  FlaskConical,
  Lock,
  Save,
  ShieldCheck,
  XCircle
} from "lucide-react";
import {
  fetchAiUpgradeCandidates,
  fetchRecentBacktestRuns,
  fetchRecentOrders,
  fetchRecentOutcomes,
  fetchStrategyVersions,
  reviewAiUpgradeCandidate,
  updateDraftStrategyJson
} from "../lib/supabaseData";
import {
  formatKst,
  formatNumber,
  formatRatio,
  isRecord,
  numberValue,
  recordValue,
  summarizeJson
} from "../lib/formatters";
import { useAdminAccess } from "../lib/useAdminAccess";
import type {
  AiUpgradeCandidateRow,
  BacktestRunRow,
  OrderRow,
  OutcomeRow,
  StrategyVersionRow
} from "../lib/rows";
import { AuthRequiredBlock } from "../components/AuthRequiredState";
import {
  EmptyState,
  ErrorState,
  KeyValue,
  LoadingState,
  pageButtonClass,
  Panel,
  Pill,
  SectionTitle
} from "../components/ui";

const editableStrategyStatuses = new Set(["draft", "proposed"]);
const reviewableCandidateStatuses = new Set(["proposed", "backtesting"]);

export function StrategyLabPage() {
  const queryClient = useQueryClient();
  const adminAccess = useAdminAccess();
  const strategies = useQuery({
    queryKey: ["strategy_versions", "strategy_lab"],
    queryFn: () => fetchStrategyVersions(30),
    refetchInterval: 60_000
  });
  const outcomes = useQuery({
    queryKey: ["outcomes", "strategy_lab"],
    queryFn: () => fetchRecentOutcomes(120),
    refetchInterval: 60_000
  });
  const orders = useQuery({
    queryKey: ["orders", "strategy_lab"],
    queryFn: () => fetchRecentOrders(200),
    refetchInterval: 60_000
  });
  const backtests = useQuery({
    queryKey: ["backtest_runs", "strategy_lab"],
    queryFn: () => fetchRecentBacktestRuns(20),
    refetchInterval: 120_000
  });
  const candidates = useQuery({
    queryKey: ["ai_upgrade_candidates", "strategy_lab"],
    queryFn: () => fetchAiUpgradeCandidates(50),
    refetchInterval: 60_000
  });

  const currentStrategy = useMemo(
    () => selectCurrentStrategy(strategies.data ?? []),
    [strategies.data]
  );
  const performance = useMemo(
    () => summarizePerformance(outcomes.data ?? [], orders.data ?? []),
    [outcomes.data, orders.data]
  );

  const [weightsText, setWeightsText] = useState("{}");
  const [paramsText, setParamsText] = useState("{}");

  useEffect(() => {
    if (!currentStrategy) {
      return;
    }
    setWeightsText(formatJson(currentStrategy.weightsJson));
    setParamsText(formatJson(currentStrategy.paramsJson));
  }, [currentStrategy]);

  const strategyMutation = useMutation({
    mutationFn: updateDraftStrategyJson,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["strategy_versions"] })
  });

  const candidateMutation = useMutation({
    mutationFn: reviewAiUpgradeCandidate,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["ai_upgrade_candidates"] })
  });

  return (
    <div className="space-y-4">
      {adminAccess.isLimited ? (
        <AuthRequiredBlock surface="Strategy Lab 데이터와 후보 검토" />
      ) : null}
      <Panel className="border-red-200 bg-red-50">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="flex items-center gap-2 font-semibold text-red-900">
              <Lock size={17} aria-hidden="true" />
              Strategy Lab 안전 잠금
            </div>
            <p className="mt-1 text-sm text-red-800">
              이 화면은 Supabase RLS로 후보 검토 상태만 바꿉니다. live_order_allowed, broker order, 외부 API 호출은 실행하지 않습니다.
            </p>
          </div>
          <Pill tone="danger">Live 승격 비활성</Pill>
        </div>
      </Panel>

      <div className="grid gap-4 xl:grid-cols-[1.05fr_0.95fr]">
        <CurrentStrategySection
          strategy={currentStrategy}
          isLoading={strategies.isLoading}
          isError={strategies.isError}
          weightsText={weightsText}
          paramsText={paramsText}
          onWeightsTextChange={setWeightsText}
          onParamsTextChange={setParamsText}
          isSaving={strategyMutation.isPending}
          onSave={() => {
            if (!currentStrategy) {
              return;
            }
            const weightsJson = parseJsonRecord(weightsText);
            const paramsJson = parseJsonRecord(paramsText);
            if (!weightsJson || !paramsJson) {
              window.alert("weights_json 또는 params_json이 올바른 JSON object가 아닙니다.");
              return;
            }
            if (window.confirm("draft/proposed 전략 JSON만 저장합니다. live 배포나 주문 권한은 변경하지 않습니다.")) {
              strategyMutation.mutate({ id: currentStrategy.id, weightsJson, paramsJson });
            }
          }}
        />
        <PaperPerformanceSection
          performance={performance}
          outcomes={outcomes.data ?? []}
          isLoading={outcomes.isLoading || orders.isLoading}
          isError={outcomes.isError || orders.isError}
        />
      </div>

      <BacktestSection data={backtests.data} isLoading={backtests.isLoading} isError={backtests.isError} />

      <AiCandidatesSection
        candidates={candidates.data ?? []}
        isLoading={candidates.isLoading}
        isError={candidates.isError}
        isPending={candidateMutation.isPending}
        onReview={(candidate, status) => {
          const statusLabel = status === "approved_for_paper" ? "Paper 검증 승인" : "거절";
          if (
            window.confirm(
              `${candidate.candidateName} 후보를 ${statusLabel} 상태로 변경할까요? live 배포와 strategy_versions 변경은 실행되지 않습니다.`
            )
          ) {
            candidateMutation.mutate({ id: candidate.id, status });
          }
        }}
      />
    </div>
  );
}

function CurrentStrategySection({
  strategy,
  isLoading,
  isError,
  weightsText,
  paramsText,
  onWeightsTextChange,
  onParamsTextChange,
  isSaving,
  onSave
}: {
  readonly strategy: StrategyVersionRow | null;
  readonly isLoading: boolean;
  readonly isError: boolean;
  readonly weightsText: string;
  readonly paramsText: string;
  readonly onWeightsTextChange: (value: string) => void;
  readonly onParamsTextChange: (value: string) => void;
  readonly isSaving: boolean;
  readonly onSave: () => void;
}) {
  const canEdit = strategy ? editableStrategyStatuses.has(strategy.status) : false;
  return (
    <Panel>
      <SectionTitle
        title="현재 전략"
        detail={strategy ? <StatusPill status={strategy.status} /> : <Pill tone="warning">전략 없음</Pill>}
      />
      {isLoading ? (
        <LoadingState label="strategy_versions를 불러오는 중" />
      ) : null}
      {isError ? (
        <ErrorState message="strategy_versions를 읽지 못했습니다." />
      ) : null}
      {!isLoading && !isError && !strategy ? (
        <EmptyState title="전략 버전 없음" detail="strategy_v1_weighted_factor seed 또는 paper 전략을 먼저 추가하세요." />
      ) : null}
      {!isLoading && !isError && strategy ? (
        <div className="space-y-4">
          <div className="grid gap-x-6 sm:grid-cols-2">
            <KeyValue label="active paper strategy" value={strategy.version} />
            <KeyValue label="version_name" value={strategy.versionName} />
            <KeyValue label="strategy_type" value={strategy.strategyType} />
            <KeyValue label="deployed_at" value={formatKst(strategy.deployedAt ?? strategy.approvedAt)} />
            <KeyValue label="created_at" value={formatKst(strategy.createdAt)} />
            <KeyValue
              label="편집 가능"
              value={canEdit ? <Pill tone="info">draft/proposed</Pill> : <Pill tone="neutral">읽기 전용</Pill>}
            />
          </div>

          {canEdit ? (
            <div className="grid gap-3 lg:grid-cols-2">
              <JsonEditor label="weights_json" value={weightsText} onChange={onWeightsTextChange} />
              <JsonEditor label="params_json" value={paramsText} onChange={onParamsTextChange} />
              <button className={pageButtonClass("neutral")} disabled={isSaving} onClick={onSave}>
                <Save size={16} aria-hidden="true" />
                JSON 저장
              </button>
            </div>
          ) : (
            <div className="grid gap-3 lg:grid-cols-2">
              <ReadonlyJson title="weights_json" value={strategy.weightsJson} />
              <ReadonlyJson title="params_json" value={strategy.paramsJson} />
            </div>
          )}
        </div>
      ) : null}
    </Panel>
  );
}

function PaperPerformanceSection({
  performance,
  outcomes,
  isLoading,
  isError
}: {
  readonly performance: PerformanceSummary;
  readonly outcomes: readonly OutcomeRow[];
  readonly isLoading: boolean;
  readonly isError: boolean;
}) {
  return (
    <Panel>
      <SectionTitle title="Paper 성과" detail={<Pill tone="safe">orders/outcomes only</Pill>} />
      {isLoading ? (
        <LoadingState label="outcomes와 orders를 불러오는 중" />
      ) : null}
      {isError ? (
        <ErrorState message="outcomes 또는 orders를 읽지 못했습니다." />
      ) : null}
      {!isLoading && !isError ? (
        <>
      <div className="grid gap-3 sm:grid-cols-2">
        <MetricBox label="return_1d 평균" value={formatRatio(performance.return1dAvg, 2)} />
        <MetricBox label="return_5d 평균" value={formatRatio(performance.return5dAvg, 2)} />
        <MetricBox label="return_20d 평균" value={formatRatio(performance.return20dAvg, 2)} />
        <MetricBox label="win rate" value={formatRatio(performance.winRate, 1)} />
        <MetricBox label="max drawdown" value={formatRatio(performance.maxDrawdown, 2)} />
        <MetricBox label="최근 주문 수" value={String(performance.orderCount)} />
      </div>

      <div className="mt-4 rounded-md border border-line p-3">
        <h3 className="text-sm font-semibold text-ink">blocked reason count</h3>
        {performance.blockedReasons.length === 0 ? (
          <p className="mt-2 text-sm text-muted">최근 blocked order reason이 없습니다.</p>
        ) : (
          <div className="mt-2 flex flex-wrap gap-2">
            {performance.blockedReasons.map(([reason, count]) => (
              <Pill key={reason} tone="warning">
                {reason}: {count}
              </Pill>
            ))}
          </div>
        )}
      </div>

      <div className="mt-4">
        <h3 className="mb-2 text-sm font-semibold text-ink">최근 outcomes</h3>
        {outcomes.length === 0 ? (
          <EmptyState title="outcomes 없음" detail="update_outcomes_once 실행 후 Paper 성과가 채워집니다." />
        ) : (
          <div className="grid gap-2 md:grid-cols-2">
            {outcomes.slice(0, 6).map((outcome) => (
              <div key={outcome.id} className="rounded-md border border-line p-3 text-sm">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-semibold text-ink">{outcome.symbol}</span>
                  <Pill tone={outcome.outcomeStatus === "complete" ? "safe" : "neutral"}>{outcome.outcomeStatus}</Pill>
                </div>
                <p className="mt-2 text-muted">
                  1d {formatRatio(outcome.return1d, 2)} · 5d {formatRatio(outcome.return5d, 2)} · 20d{" "}
                  {formatRatio(outcome.return20d, 2)}
                </p>
                <p className="mt-1 text-muted">MDD {formatRatio(outcome.maxDrawdown20d, 2)} · {formatKst(outcome.updatedAt ?? outcome.calculatedAt)}</p>
              </div>
            ))}
          </div>
        )}
      </div>
        </>
      ) : null}
    </Panel>
  );
}

function BacktestSection({
  data,
  isLoading,
  isError
}: {
  readonly data: { readonly available: boolean; readonly warning: string | null; readonly rows: readonly BacktestRunRow[] } | undefined;
  readonly isLoading: boolean;
  readonly isError: boolean;
}) {
  return (
    <Panel>
      <SectionTitle title="Backtest" detail={<Pill tone={data?.available === false ? "warning" : "info"}>cached data</Pill>} />
      {isLoading ? <LoadingState label="backtest_runs를 불러오는 중" /> : null}
      {isError ? <ErrorState message="backtest_runs를 읽지 못했습니다." /> : null}
      {data?.warning ? (
        <div className="mb-3 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
          {data.warning} `0007_backtest_runs.sql`과 `0008_backtest_runs_rls.sql` 적용 상태를 확인하세요.
        </div>
      ) : null}
      {data && data.rows.length === 0 && !isLoading ? (
        <EmptyState title="backtest_runs 없음" detail="run_backtest 명령 실행 후 최근 결과가 표시됩니다." />
      ) : null}
      {data && data.rows.length > 0 ? (
        <div className="grid gap-3 xl:grid-cols-2">
          {data.rows.map((run) => (
            <BacktestCard key={run.id} run={run} />
          ))}
        </div>
      ) : null}
    </Panel>
  );
}

function BacktestCard({ run }: { readonly run: BacktestRunRow }) {
  const status = backtestStatus(run);
  return (
    <div className="rounded-md border border-line p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="font-semibold text-ink">{run.strategyVersion || run.strategy}</p>
          <p className="text-sm text-muted">
            {run.periodStart ?? "-"} ~ {run.periodEnd ?? "-"} · {formatKst(run.createdAt)}
          </p>
        </div>
        <Pill tone={status === "passed" ? "safe" : "warning"}>{status === "passed" ? "통과" : "검토 필요"}</Pill>
      </div>
      <div className="mt-3 grid gap-2 text-sm sm:grid-cols-3">
        <KeyValue label="return" value={formatRatio(run.totalReturn, 2)} />
        <KeyValue label="CAGR" value={formatRatio(run.cagr, 2)} />
        <KeyValue label="MDD" value={formatRatio(run.maxDrawdown, 2)} />
        <KeyValue label="win_rate" value={formatRatio(run.winRate, 1)} />
        <KeyValue label="turnover" value={formatNumber(run.turnover, 3)} />
        <KeyValue label="trades" value={String(run.numberOfTrades)} />
      </div>
      <div className="mt-3 grid gap-2 text-xs text-muted sm:grid-cols-2">
        <span>fee: {formatRatio(numberValue(run.assumptions.transaction_fee_rate), 3)}</span>
        <span>slippage: {formatRatio(numberValue(run.assumptions.slippage_rate), 3)}</span>
      </div>
      <p className="mt-2 text-xs text-muted">blocked: {summarizeJson(run.blockedReasonCounts)}</p>
    </div>
  );
}

function AiCandidatesSection({
  candidates,
  isLoading,
  isError,
  isPending,
  onReview
}: {
  readonly candidates: readonly AiUpgradeCandidateRow[];
  readonly isLoading: boolean;
  readonly isError: boolean;
  readonly isPending: boolean;
  readonly onReview: (candidate: AiUpgradeCandidateRow, status: "approved_for_paper" | "rejected") => void;
}) {
  return (
    <Panel>
      <SectionTitle title="AI 후보" detail={<Pill tone="info">OpenAI research only</Pill>} />
      {isLoading ? (
        <LoadingState label="ai_upgrade_candidates를 불러오는 중" />
      ) : null}
      {isError ? (
        <ErrorState message="ai_upgrade_candidates를 읽지 못했습니다." />
      ) : null}
      {!isLoading && !isError && candidates.length === 0 ? (
        <EmptyState title="AI 후보 없음" detail="generate_monthly_ai_candidate 실행 후 proposed 후보가 표시됩니다." />
      ) : null}
      {!isLoading && !isError && candidates.length > 0 ? (
        <div className="grid gap-3 xl:grid-cols-2">
          {candidates.map((candidate) => (
            <div key={candidate.id} className="rounded-md border border-line p-4">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div>
                  <h3 className="font-semibold text-ink">{candidate.candidateName}</h3>
                  <p className="text-sm text-muted">생성 {formatKst(candidate.createdAt)} · 검토 {formatKst(candidate.reviewedAt)}</p>
                </div>
                <StatusPill status={candidate.status} />
              </div>

              <div className="mt-3 space-y-2 text-sm">
                <CandidateText label="rationale" value={candidate.rationale} />
                <CandidateText label="expected_improvement" value={candidate.expectedImprovement} />
                <CandidateText label="risk_notes" value={candidate.riskNotes} warning />
              </div>

              <div className="mt-3 grid gap-3 md:grid-cols-2">
                <ReadonlyJson title="candidate_weights" value={candidate.candidateWeights} compact />
                <ReadonlyJson title="candidate_params" value={candidate.candidateParams} compact />
              </div>

              <div className="mt-3">
                <h4 className="text-sm font-semibold text-ink">required_backtests</h4>
                {candidate.requiredBacktests.length === 0 ? (
                  <p className="mt-1 text-sm text-muted">명시된 required_backtests가 없습니다.</p>
                ) : (
                  <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-muted">
                    {candidate.requiredBacktests.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                )}
              </div>

              <div className="mt-4 flex flex-wrap gap-2">
                <button
                  className={pageButtonClass("safe")}
                  disabled={isPending || !reviewableCandidateStatuses.has(candidate.status)}
                  onClick={() => onReview(candidate, "approved_for_paper")}
                >
                  <CheckCircle2 size={16} aria-hidden="true" />
                  Paper 검증 승인
                </button>
                <button
                  className={pageButtonClass("warning")}
                  disabled={isPending || candidate.status === "rejected"}
                  onClick={() => onReview(candidate, "rejected")}
                >
                  <XCircle size={16} aria-hidden="true" />
                  거절
                </button>
                <button
                  className={pageButtonClass("neutral")}
                  onClick={() => {
                    if (window.confirm("Paper 승격은 아직 자동 실행되지 않습니다. live_order_allowed는 변경하지 않습니다.")) {
                      window.alert("Paper 승격은 저장되지 않았습니다. RUNBOOK의 수동 검증 절차를 따르세요.");
                    }
                  }}
                >
                  <FlaskConical size={16} aria-hidden="true" />
                  Paper 승격 절차 확인
                </button>
                <button className={pageButtonClass("danger")} disabled>
                  <ShieldCheck size={16} aria-hidden="true" />
                  Live 승격 비활성
                </button>
              </div>
            </div>
          ))}
        </div>
      ) : null}
    </Panel>
  );
}

function JsonEditor({
  label,
  value,
  onChange
}: {
  readonly label: string;
  readonly value: string;
  readonly onChange: (value: string) => void;
}) {
  return (
    <label className="block text-sm">
      <span className="font-semibold text-ink">{label}</span>
      <textarea
        value={value}
        onChange={(event) => onChange(event.currentTarget.value)}
        className="mt-2 min-h-56 w-full rounded-md border border-line bg-slate-50 px-3 py-2 font-mono text-xs focus:outline-none focus:ring-2 focus:ring-slate-400"
        spellCheck={false}
      />
    </label>
  );
}

function ReadonlyJson({
  title,
  value,
  compact = false
}: {
  readonly title: string;
  readonly value: Record<string, unknown>;
  readonly compact?: boolean;
}) {
  return (
    <div className="rounded-md border border-line bg-slate-50 p-3">
      <h3 className="mb-2 text-sm font-semibold text-ink">{title}</h3>
      <pre className={`${compact ? "max-h-36" : "max-h-72"} overflow-auto whitespace-pre-wrap break-words text-xs text-muted`}>
        {formatJson(value)}
      </pre>
    </div>
  );
}

function MetricBox({ label, value }: { readonly label: string; readonly value: string }) {
  return (
    <div className="rounded-md border border-line p-3">
      <p className="text-xs font-medium text-muted">{label}</p>
      <p className="mt-1 text-lg font-semibold text-ink">{value}</p>
    </div>
  );
}

function StatusPill({ status }: { readonly status: string }) {
  const tone =
    status === "paper" ||
    status === "active" ||
    status === "approved_for_paper" ||
    status === "approved"
      ? "safe"
      : status === "live" || status === "deployed"
        ? "danger"
        : status === "rejected" || status === "retired"
          ? "warning"
          : "neutral";
  return <Pill tone={tone}>{statusLabel(status)}</Pill>;
}

function CandidateText({
  label,
  value,
  warning = false
}: {
  readonly label: string;
  readonly value: string;
  readonly warning?: boolean;
}) {
  return (
    <div className={`rounded-md border p-3 ${warning ? "border-amber-200 bg-amber-50" : "border-line bg-white"}`}>
      <p className="text-xs font-semibold text-muted">{label}</p>
      <p className="mt-1 text-ink">{value || "-"}</p>
    </div>
  );
}

function selectCurrentStrategy(rows: readonly StrategyVersionRow[]): StrategyVersionRow | null {
  return (
    rows.find((row) => row.status === "paper") ??
    rows.find((row) => row.status === "active") ??
    rows.find((row) => row.version === "strategy_v1_weighted_factor") ??
    rows[0] ??
    null
  );
}

interface PerformanceSummary {
  readonly return1dAvg: number | null;
  readonly return5dAvg: number | null;
  readonly return20dAvg: number | null;
  readonly winRate: number | null;
  readonly maxDrawdown: number | null;
  readonly orderCount: number;
  readonly blockedReasons: readonly [string, number][];
}

function summarizePerformance(outcomes: readonly OutcomeRow[], orders: readonly OrderRow[]): PerformanceSummary {
  const return20dValues = compactNumbers(outcomes.map((outcome) => outcome.return20d));
  return {
    return1dAvg: average(compactNumbers(outcomes.map((outcome) => outcome.return1d))),
    return5dAvg: average(compactNumbers(outcomes.map((outcome) => outcome.return5d))),
    return20dAvg: average(return20dValues),
    winRate: return20dValues.length === 0 ? null : return20dValues.filter((value) => value > 0).length / return20dValues.length,
    maxDrawdown: minNumber(compactNumbers(outcomes.map((outcome) => outcome.maxDrawdown20d))),
    orderCount: orders.length,
    blockedReasons: countBlockedReasons(orders)
  };
}

function countBlockedReasons(orders: readonly OrderRow[]): readonly [string, number][] {
  const counts = new Map<string, number>();
  for (const order of orders) {
    if (order.status !== "blocked") {
      continue;
    }
    const reason = extractReason(order);
    counts.set(reason, (counts.get(reason) ?? 0) + 1);
  }
  return Array.from(counts.entries()).sort((a, b) => b[1] - a[1]).slice(0, 8);
}

function extractReason(order: OrderRow): string {
  if (order.reason) {
    return order.reason;
  }
  const reasonJson = recordValue(order.reasonJson);
  const riskJson = recordValue(order.riskSnapshotJson);
  return (
    stringFromJson(reasonJson, ["reason", "safe_message", "message"]) ??
    stringFromJson(riskJson, ["reason", "safe_message", "message"]) ??
    "unknown"
  );
}

function stringFromJson(value: Record<string, unknown>, keys: readonly string[]): string | null {
  for (const key of keys) {
    const item = value[key];
    if (typeof item === "string" && item.length > 0) {
      return item;
    }
  }
  return null;
}

function backtestStatus(run: BacktestRunRow): "passed" | "review" {
  const totalReturn = run.totalReturn ?? 0;
  const maxDrawdown = run.maxDrawdown ?? 0;
  if (run.numberOfTrades <= 0 || totalReturn <= 0 || maxDrawdown < -0.2) {
    return "review";
  }
  return "passed";
}

function statusLabel(status: string): string {
  if (status === "approved_for_paper") {
    return "approved";
  }
  if (status === "deployed") {
    return "deployed";
  }
  return status;
}

function parseJsonRecord(value: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(value);
    return isRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function formatJson(value: Record<string, unknown>): string {
  return JSON.stringify(value, null, 2);
}

function compactNumbers(values: readonly (number | null)[]): readonly number[] {
  return values.filter((value): value is number => value !== null && Number.isFinite(value));
}

function average(values: readonly number[]): number | null {
  if (values.length === 0) {
    return null;
  }
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function minNumber(values: readonly number[]): number | null {
  if (values.length === 0) {
    return null;
  }
  return Math.min(...values);
}
