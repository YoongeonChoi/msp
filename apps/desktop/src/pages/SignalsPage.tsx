import { useQuery } from "@tanstack/react-query";
import { fetchRecentDecisions } from "../lib/supabaseData";
import { formatKst, formatNumber, nestedNumber, recordValue } from "../lib/formatters";
import { useAdminAccess } from "../lib/useAdminAccess";
import type { DecisionSnapshot } from "../lib/rows";
import { AuthRequiredBlock } from "../components/AuthRequiredState";
import { EmptyState, ErrorState, JsonSummary, LoadingState, Panel, Pill, SectionTitle } from "../components/ui";

const componentKeys = [
  ["technical_score", "technical"],
  ["fundamental_score", "fundamental"],
  ["market_sector_score", "market_sector"],
  ["news_event_score", "news_event"],
  ["portfolio_score", "portfolio"]
];

export function SignalsPage() {
  const adminAccess = useAdminAccess();
  const decisions = useQuery({ queryKey: ["decision_snapshots", "recent"], queryFn: () => fetchRecentDecisions(80), refetchInterval: 60_000 });

  if (decisions.isLoading) {
    return <LoadingState label="decision_snapshots를 불러오는 중" />;
  }
  if (decisions.error) {
    return <ErrorState message="decision_snapshots를 읽지 못했습니다." />;
  }

  const rows = decisions.data ?? [];
  return (
    <Panel>
      <SectionTitle title="최근 시그널" />
      {rows.length === 0 ? (
        adminAccess.isLimited ? (
          <AuthRequiredBlock surface="decision_snapshots" />
        ) : (
          <EmptyState title="시그널 없음" detail="Worker cycle이 한 번 실행되면 표시됩니다." />
        )
      ) : (
        <>
          <div className="hidden overflow-x-auto lg:block">
            <table className="min-w-full text-sm">
              <thead className="text-left text-muted">
                <tr>
                  <th className="py-2">종목</th>
                  <th>액션</th>
                  <th>최종</th>
                  <th>Technical</th>
                  <th>Fundamental</th>
                  <th>Market</th>
                  <th>News</th>
                  <th>Portfolio</th>
                  <th>reason_json</th>
                  <th>risk_json</th>
                  <th>KST</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {rows.map((row) => (
                  <tr key={row.id}>
                    <td className="py-2 font-medium text-ink">{row.symbol}</td>
                    <td><ActionPill action={row.action} /></td>
                    <td>{formatNumber(row.finalScore, 3)}</td>
                    {componentKeys.map(([key, fallback]) => (
                      <td key={key}>{formatNumber(scoreFrom(row, key, fallback), 3)}</td>
                    ))}
                    <td className="max-w-48"><JsonSummary value={row.featureSnapshot} /></td>
                    <td className="max-w-48"><JsonSummary value={row.riskSnapshot} /></td>
                    <td>{formatKst(row.decidedAt)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="grid gap-3 lg:hidden">
            {rows.map((row) => (
              <div key={row.id} className="rounded-md border border-line p-3">
                <div className="flex items-center justify-between">
                  <span className="font-medium text-ink">{row.symbol}</span>
                  <ActionPill action={row.action} />
                </div>
                <p className="mt-2 text-sm text-muted">최종 {formatNumber(row.finalScore, 3)} · {formatKst(row.decidedAt)}</p>
                <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-muted">
                  {componentKeys.map(([key, fallback]) => (
                    <span key={key}>{fallback}: {formatNumber(scoreFrom(row, key, fallback), 2)}</span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </Panel>
  );
}

function ActionPill({ action }: { readonly action: string }) {
  const tone = action === "buy" ? "safe" : action === "sell" ? "warning" : "neutral";
  return <Pill tone={tone}>{action}</Pill>;
}

function scoreFrom(row: DecisionSnapshot, directKey: string, fallbackKey: string): number | null {
  const snapshot = recordValue(row.featureSnapshot);
  return (
    nestedNumber(snapshot, [directKey]) ??
    nestedNumber(snapshot, ["component_scores", directKey]) ??
    nestedNumber(snapshot, ["component_scores", fallbackKey]) ??
    nestedNumber(snapshot, ["scores", directKey]) ??
    nestedNumber(snapshot, ["scores", fallbackKey])
  );
}
