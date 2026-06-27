import { useQuery } from "@tanstack/react-query";
import { fetchPositions } from "../lib/supabaseData";
import { formatAge, formatKrw, formatRatio } from "../lib/formatters";
import { EmptyState, ErrorState, LoadingState, Panel, Pill, SectionTitle } from "../components/ui";

export function PortfolioPage() {
  const positions = useQuery({ queryKey: ["positions"], queryFn: fetchPositions, refetchInterval: 60_000 });

  if (positions.isLoading) {
    return <LoadingState label="포지션을 불러오는 중" />;
  }
  if (positions.error) {
    return <ErrorState message="positions를 읽지 못했습니다." />;
  }

  const rows = positions.data ?? [];
  return (
    <Panel>
      <SectionTitle title="포트폴리오" />
      {rows.length === 0 ? (
        <EmptyState title="보유 포지션 없음" detail="계좌 sync가 완료되면 포지션이 표시됩니다." />
      ) : (
        <>
          <div className="hidden overflow-x-auto md:block">
            <table className="min-w-full text-sm">
              <thead className="text-left text-muted">
                <tr>
                  <th className="py-2">종목</th>
                  <th>수량</th>
                  <th>평균가</th>
                  <th>현재가</th>
                  <th>평가금액</th>
                  <th>PnL</th>
                  <th>섹터</th>
                  <th>동기화</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {rows.map((row) => (
                  <tr key={row.id}>
                    <td className="py-2 font-medium text-ink">{row.symbol}</td>
                    <td>{row.quantity}</td>
                    <td>{formatKrw(row.avgPriceKrw)}</td>
                    <td>{formatKrw(row.currentPriceKrw)}</td>
                    <td>{formatKrw(row.marketValueKrw)}</td>
                    <td>
                      <Pill tone={row.unrealizedPnlKrw >= 0 ? "safe" : "danger"}>
                        {formatKrw(row.unrealizedPnlKrw)} / {formatRatio(row.unrealizedPnlPct)}
                      </Pill>
                    </td>
                    <td>{row.sector}</td>
                    <td>{formatAge(row.syncedAt)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="grid gap-3 md:hidden">
            {rows.map((row) => (
              <div key={row.id} className="rounded-md border border-line p-3">
                <div className="flex items-center justify-between">
                  <span className="font-medium text-ink">{row.symbol}</span>
                  <Pill tone={row.unrealizedPnlKrw >= 0 ? "safe" : "danger"}>{formatRatio(row.unrealizedPnlPct)}</Pill>
                </div>
                <p className="mt-2 text-sm text-muted">{row.sector} · {row.quantity}주 · {formatKrw(row.marketValueKrw)}</p>
              </div>
            ))}
          </div>
        </>
      )}
    </Panel>
  );
}
