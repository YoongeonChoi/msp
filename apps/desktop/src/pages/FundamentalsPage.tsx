import { useQuery } from "@tanstack/react-query";
import { fetchFundamentals } from "../lib/supabaseData";
import { formatKst, formatNumber, formatRatio, nestedNumber } from "../lib/formatters";
import type { FundamentalRow } from "../lib/rows";
import { EmptyState, ErrorState, LoadingState, Panel, SectionTitle } from "../components/ui";

export function FundamentalsPage() {
  const fundamentals = useQuery({ queryKey: ["fundamentals_quarterly"], queryFn: fetchFundamentals, refetchInterval: 300_000 });

  if (fundamentals.isLoading) {
    return <LoadingState label="fundamentals_quarterly를 불러오는 중" />;
  }
  if (fundamentals.error) {
    return <ErrorState message="fundamentals_quarterly를 읽지 못했습니다." />;
  }

  const rows = fundamentals.data ?? [];
  return (
    <Panel>
      <SectionTitle title="펀더멘털" />
      {rows.length === 0 ? (
        <EmptyState title="펀더멘털 없음" detail="OpenDART 수집 결과가 저장되면 표시됩니다." />
      ) : (
        <>
          <div className="hidden overflow-x-auto xl:block">
            <table className="min-w-full text-sm">
              <thead className="text-left text-muted">
                <tr>
                  <th className="py-2">종목</th>
                  <th>연도</th>
                  <th>분기</th>
                  <th>revenue</th>
                  <th>operating_income</th>
                  <th>net_income</th>
                  <th>equity</th>
                  <th>PER/PBR/ROE</th>
                  <th>op_margin</th>
                  <th>debt_ratio</th>
                  <th>KST</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {rows.map((row) => (
                  <tr key={row.id}>
                    <td className="py-2 font-medium text-ink">{row.symbol}</td>
                    <td>{row.fiscalYear ?? "-"}</td>
                    <td>{row.fiscalQuarter ?? "-"}</td>
                    <td>{formatNumber(rawMetric(row, "revenue"), 0)}</td>
                    <td>{formatNumber(rawMetric(row, "operating_income"), 0)}</td>
                    <td>{formatNumber(rawMetric(row, "net_income"), 0)}</td>
                    <td>{formatNumber(rawMetric(row, "equity"), 0)}</td>
                    <td>{formatNumber(row.per, 2)} / {formatNumber(row.pbr, 2)} / {formatRatio(row.roe)}</td>
                    <td>{formatRatio(row.operatingMargin)}</td>
                    <td>{formatRatio(row.debtRatio)}</td>
                    <td>{formatKst(row.updatedAt)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="grid gap-3 xl:hidden">
            {rows.map((row) => (
              <div key={row.id} className="rounded-md border border-line p-3">
                <p className="font-medium text-ink">{row.symbol} · {row.fiscalYear} Q{row.fiscalQuarter}</p>
                <p className="mt-2 text-sm text-muted">PER {formatNumber(row.per, 2)} · PBR {formatNumber(row.pbr, 2)} · ROE {formatRatio(row.roe)}</p>
                <p className="mt-1 text-sm text-muted">매출 {formatNumber(rawMetric(row, "revenue"), 0)} · 영업이익 {formatNumber(rawMetric(row, "operating_income"), 0)}</p>
              </div>
            ))}
          </div>
        </>
      )}
    </Panel>
  );
}

function rawMetric(row: FundamentalRow, key: string): number | null {
  return nestedNumber(row.rawSnapshot, [key]) ?? nestedNumber(row.rawSnapshot, ["canonical", key]);
}
