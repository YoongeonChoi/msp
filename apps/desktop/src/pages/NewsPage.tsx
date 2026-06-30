import { useQuery } from "@tanstack/react-query";
import { fetchNewsEvents } from "../lib/supabaseData";
import { formatKst } from "../lib/formatters";
import { useAdminAccess } from "../lib/useAdminAccess";
import { AuthRequiredBlock } from "../components/AuthRequiredState";
import { EmptyState, ErrorState, LoadingState, Panel, Pill, SectionTitle } from "../components/ui";

export function NewsPage() {
  const adminAccess = useAdminAccess();
  const news = useQuery({ queryKey: ["news_events"], queryFn: fetchNewsEvents, refetchInterval: 120_000 });

  if (news.isLoading) {
    return <LoadingState label="news_events를 불러오는 중" />;
  }
  if (news.error) {
    return <ErrorState message="news_events를 읽지 못했습니다." />;
  }

  const rows = news.data ?? [];
  return (
    <Panel>
      <SectionTitle title="뉴스 이벤트" />
      {rows.length === 0 ? (
        adminAccess.isLimited ? (
          <AuthRequiredBlock surface="news_events" />
        ) : (
          <EmptyState title="뉴스 없음" detail="Naver/OpenAI 수집 결과가 저장되면 표시됩니다." />
        )
      ) : (
        <>
          <div className="hidden overflow-x-auto lg:block">
            <table className="min-w-full text-sm">
              <thead className="text-left text-muted">
                <tr>
                  <th className="py-2">종목</th>
                  <th>제목</th>
                  <th>출처</th>
                  <th>sentiment</th>
                  <th>event_type</th>
                  <th>risk_level</th>
                  <th>요약</th>
                  <th>KST</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {rows.map((row) => (
                  <tr key={row.id}>
                    <td className="py-2 font-medium text-ink">{row.symbol}</td>
                    <td className="max-w-72">{row.title}</td>
                    <td>{row.source}</td>
                    <td>{row.sentiment ?? "-"}</td>
                    <td>{row.eventType ?? "-"}</td>
                    <td><RiskPill value={row.riskLevel} /></td>
                    <td className="max-w-72 text-muted">{row.summaryShort ?? "-"}</td>
                    <td>{formatKst(row.publishedAt ?? row.createdAt)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="grid gap-3 lg:hidden">
            {rows.map((row) => (
              <div key={row.id} className="rounded-md border border-line p-3">
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <p className="font-medium text-ink">{row.symbol}</p>
                    <p className="text-sm text-muted">{row.source} · {formatKst(row.publishedAt ?? row.createdAt)}</p>
                  </div>
                  <RiskPill value={row.riskLevel} />
                </div>
                <p className="mt-2 text-sm font-medium text-ink">{row.title}</p>
                <p className="mt-1 text-sm text-muted">{row.summaryShort ?? "-"}</p>
              </div>
            ))}
          </div>
        </>
      )}
    </Panel>
  );
}

function RiskPill({ value }: { readonly value: string | null }) {
  if (value === "critical" || value === "high") {
    return <Pill tone="danger">{value}</Pill>;
  }
  if (value === "medium" || value === "unknown") {
    return <Pill tone="warning">{value ?? "unknown"}</Pill>;
  }
  return <Pill tone="safe">{value ?? "low"}</Pill>;
}
