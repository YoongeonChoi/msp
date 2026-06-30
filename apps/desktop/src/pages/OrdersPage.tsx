import { AlertTriangle } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { fetchRecentOrders } from "../lib/supabaseData";
import { formatKrw, formatKst } from "../lib/formatters";
import { useAdminAccess } from "../lib/useAdminAccess";
import type { OrderRow } from "../lib/rows";
import { AuthRequiredBlock } from "../components/AuthRequiredState";
import { EmptyState, ErrorState, JsonSummary, LoadingState, Panel, Pill, SectionTitle } from "../components/ui";

const anomalyStatuses = ["sent", "filled", "partial_filled"];

export function OrdersPage() {
  const adminAccess = useAdminAccess();
  const orders = useQuery({ queryKey: ["orders", "recent"], queryFn: () => fetchRecentOrders(100), refetchInterval: 60_000 });

  if (orders.isLoading) {
    return <LoadingState label="orders를 불러오는 중" />;
  }
  if (orders.error) {
    return <ErrorState message="orders를 읽지 못했습니다." />;
  }

  const rows = orders.data ?? [];
  const anomalies = rows.filter((row) => anomalyStatuses.includes(row.status));
  return (
    <div className="space-y-4">
      {anomalies.length > 0 ? (
        <Panel className="border-red-200 bg-red-50">
          <div className="flex items-center gap-2 text-sm font-semibold text-red-900">
            <AlertTriangle size={16} aria-hidden="true" />
            sent/filled 계열 주문 상태가 감지되었습니다. Paper Trading 검증 중이면 즉시 확인하세요.
          </div>
        </Panel>
      ) : null}
      <Panel>
        <SectionTitle title="최근 주문" />
        {rows.length === 0 ? (
          adminAccess.isLimited ? (
            <AuthRequiredBlock surface="orders" />
          ) : (
            <EmptyState title="주문 없음" detail="paper/proposed/blocked orders가 아직 없습니다." />
          )
        ) : (
          <>
            <div className="hidden overflow-x-auto xl:block">
              <table className="min-w-full text-sm">
                <thead className="text-left text-muted">
                  <tr>
                    <th className="py-2">종목</th>
                    <th>side</th>
                    <th>status</th>
                    <th>quantity</th>
                    <th>price</th>
                    <th>amount</th>
                    <th>idempotency_key</th>
                    <th>reason_json</th>
                    <th>risk_snapshot_json</th>
                    <th>KST</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {rows.map((row) => (
                    <tr key={row.id}>
                      <td className="py-2 font-medium text-ink">{row.symbol}</td>
                      <td>{row.side}</td>
                      <td><StatusPill row={row} /></td>
                      <td>{row.quantity ?? "-"}</td>
                      <td>{formatKrw(row.priceKrw)}</td>
                      <td>{formatKrw(row.amountKrw)}</td>
                      <td className="max-w-48 break-all font-mono text-xs">{row.idempotencyKey ?? "-"}</td>
                      <td className="max-w-48"><JsonSummary value={row.reasonJson} /></td>
                      <td className="max-w-48"><JsonSummary value={row.riskSnapshotJson} /></td>
                      <td>{formatKst(row.createdAt)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="grid gap-3 xl:hidden">
              {rows.map((row) => (
                <div key={row.id} className="rounded-md border border-line p-3">
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <p className="font-medium text-ink">{row.symbol} · {row.side}</p>
                      <p className="text-sm text-muted">{formatKrw(row.amountKrw)} · {formatKst(row.createdAt)}</p>
                    </div>
                    <StatusPill row={row} />
                  </div>
                  <p className="mt-2 break-all text-xs text-muted">idempotency_key: {row.idempotencyKey ?? "-"}</p>
                  <div className="mt-2"><JsonSummary value={row.riskSnapshotJson} /></div>
                </div>
              ))}
            </div>
          </>
        )}
      </Panel>
    </div>
  );
}

function StatusPill({ row }: { readonly row: OrderRow }) {
  if (anomalyStatuses.includes(row.status)) {
    return <Pill tone="danger">{row.status} anomaly</Pill>;
  }
  const tone = row.status === "blocked" ? "warning" : row.status === "paper" ? "safe" : "neutral";
  return <Pill tone={tone}>{row.status}</Pill>;
}
