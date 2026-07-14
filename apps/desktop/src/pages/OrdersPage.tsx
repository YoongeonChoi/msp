import { AlertTriangle } from "lucide-react";
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  fetchManualCheckOrderCount,
  fetchOrdersRequiringManualCheck,
  fetchRecentOrders
} from "../lib/supabaseData";
import { formatKrw, formatKst } from "../lib/formatters";
import { useAdminAccess } from "../lib/useAdminAccess";
import type { OrderRow } from "../lib/rows";
import { AuthRequiredBlock } from "../components/AuthRequiredState";
import {
  EmptyState,
  ErrorState,
  JsonSummary,
  LoadingState,
  pageButtonClass,
  Panel,
  Pill,
  SectionTitle
} from "../components/ui";

const liveActivityStatuses = new Set(["sent", "filled", "partial_filled"]);
const manualCheckStatus = "unknown_requires_manual_check";
const manualCheckPageSize = 25;

export function OrdersPage() {
  const adminAccess = useAdminAccess();
  const [manualCheckPage, setManualCheckPage] = useState(0);
  const orders = useQuery({ queryKey: ["orders", "recent"], queryFn: () => fetchRecentOrders(100), refetchInterval: 60_000 });
  const manualCheckCount = useQuery({
    queryKey: ["orders", "manual_check", "count"],
    queryFn: fetchManualCheckOrderCount,
    enabled: adminAccess.isAdmin,
    refetchInterval: adminAccess.isAdmin ? 30_000 : false
  });
  const manualCheckOrders = useQuery({
    queryKey: ["orders", "manual_check", "page", manualCheckPage, manualCheckPageSize],
    queryFn: () => fetchOrdersRequiringManualCheck(manualCheckPage, manualCheckPageSize),
    enabled: adminAccess.isAdmin,
    refetchInterval: adminAccess.isAdmin ? 30_000 : false
  });

  const totalManualCheckCount = adminAccess.isAdmin ? manualCheckCount.data ?? 0 : 0;
  const maximumManualCheckPage = Math.max(0, Math.ceil(totalManualCheckCount / manualCheckPageSize) - 1);
  useEffect(() => {
    if (manualCheckPage > maximumManualCheckPage) {
      setManualCheckPage(maximumManualCheckPage);
    }
  }, [manualCheckPage, maximumManualCheckPage]);

  const rows = orders.data ?? [];
  const liveActivityRows = rows.filter((row) => liveActivityStatuses.has(row.status));
  return (
    <div className="space-y-4">
      <ManualCheckQueue
        rows={adminAccess.isAdmin ? manualCheckOrders.data ?? [] : []}
        totalCount={totalManualCheckCount}
        page={manualCheckPage}
        pageSize={manualCheckPageSize}
        isLoading={adminAccess.isAdmin && (manualCheckCount.isLoading || manualCheckOrders.isLoading)}
        isError={adminAccess.isAdmin && (manualCheckCount.isError || manualCheckOrders.isError)}
        dataAccessLimited={!adminAccess.isAdmin}
        onPageChange={setManualCheckPage}
      />
      {liveActivityRows.length > 0 ? (
        <Panel className="border-red-200 bg-red-50">
          <div className="flex items-center gap-2 text-sm font-semibold text-red-900" role="alert">
            <AlertTriangle size={16} aria-hidden="true" />
            sent/filled 계열 주문 상태가 감지되었습니다. Paper Trading 검증 중이면 즉시 확인하세요.
          </div>
        </Panel>
      ) : null}
      <Panel>
        <SectionTitle title="최근 주문" />
        {orders.isLoading ? <LoadingState label="orders를 불러오는 중" /> : null}
        {orders.error ? <ErrorState message="orders를 읽지 못했습니다." /> : null}
        {!orders.isLoading && !orders.error && rows.length === 0 ? (
          adminAccess.isLimited ? (
            <AuthRequiredBlock surface="orders" />
          ) : (
            <EmptyState title="주문 없음" detail="paper/proposed/blocked orders가 아직 없습니다." />
          )
        ) : null}
        {!orders.isLoading && !orders.error && rows.length > 0 ? (
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
        ) : null}
      </Panel>
    </div>
  );
}

function ManualCheckQueue({
  rows,
  totalCount,
  page,
  pageSize,
  isLoading,
  isError,
  dataAccessLimited,
  onPageChange
}: {
  readonly rows: readonly OrderRow[];
  readonly totalCount: number;
  readonly page: number;
  readonly pageSize: number;
  readonly isLoading: boolean;
  readonly isError: boolean;
  readonly dataAccessLimited: boolean;
  readonly onPageChange: (page: number) => void;
}) {
  const rangeStart = totalCount === 0 ? 0 : page * pageSize + 1;
  const rangeEnd = Math.min(totalCount, page * pageSize + rows.length);
  return (
    <Panel className={totalCount > 0 || isError ? "border-red-200 bg-red-50" : ""}>
      <div
        role={totalCount > 0 || isError ? "alert" : "status"}
        aria-live="polite"
        aria-atomic="true"
      >
        <SectionTitle
          title={isLoading ? "수동 확인 필요 주문 조회 중" : `수동 확인 필요 주문 ${totalCount}건`}
          detail={<Pill tone={isLoading ? "warning" : totalCount > 0 || isError ? "danger" : "safe"}>자동 상태 변경 없음</Pill>}
        />
      </div>
      <p className="mb-3 text-sm text-muted">
        워커가 확정하지 못한 주문입니다. 이 화면은 조회만 하며 상태를 바꾸거나 브로커 API를 호출하지 않습니다.
      </p>
      {isLoading ? <LoadingState label="수동 확인 필요 주문을 불러오는 중" /> : null}
      {isError ? <ErrorState message="unknown_requires_manual_check 주문을 읽지 못했습니다." /> : null}
      {!isLoading && !isError && totalCount === 0 ? (
        dataAccessLimited ? (
          <AuthRequiredBlock surface="수동 확인 필요 orders" />
        ) : (
          <EmptyState title="미해결 주문 없음" detail="unknown_requires_manual_check 상태의 주문이 없습니다." />
        )
      ) : null}
      {!isLoading && !isError && totalCount > 0 ? (
        <>
          <div className="grid gap-3 lg:grid-cols-2" role="list" aria-label="수동 확인 필요 주문 목록">
            {rows.map((row) => (
              <article key={row.id} className="rounded-md border border-red-200 bg-white p-3" role="listitem">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div>
                  <p className="font-semibold text-ink">
                    {row.symbol} · {row.side}
                  </p>
                  <p className="mt-1 text-sm text-muted">
                    {formatKrw(row.amountKrw)} · {formatKst(row.createdAt)}
                  </p>
                </div>
                <StatusPill row={row} />
              </div>
              <dl className="mt-3 grid gap-2 text-xs">
                <div>
                  <dt className="font-medium text-muted">확인 사유</dt>
                  <dd className="mt-1 break-words text-ink">{row.reason ?? "사유 미기록"}</dd>
                </div>
                <div>
                  <dt className="font-medium text-muted">로컬 주문 ID</dt>
                  <dd className="mt-1 break-all font-mono text-ink">{row.id}</dd>
                </div>
                <div>
                  <dt className="font-medium text-muted">멱등성 키</dt>
                  <dd className="mt-1 break-all font-mono text-ink">{row.idempotencyKey ?? "-"}</dd>
                </div>
              </dl>
              </article>
            ))}
          </div>
          <nav className="mt-3 flex items-center justify-end gap-2" aria-label="수동 확인 필요 주문 페이지">
            <button
              className={pageButtonClass()}
              type="button"
              disabled={page === 0}
              onClick={() => onPageChange(page - 1)}
            >
              이전
            </button>
            <span className="text-sm text-muted" aria-live="polite">
              {rangeStart}-{rangeEnd} / {totalCount}건
            </span>
            <button
              className={pageButtonClass()}
              type="button"
              disabled={rangeEnd >= totalCount}
              onClick={() => onPageChange(page + 1)}
            >
              다음
            </button>
          </nav>
        </>
      ) : null}
    </Panel>
  );
}

function StatusPill({ row }: { readonly row: OrderRow }) {
  if (row.status === manualCheckStatus) {
    return <Pill tone="danger">unknown_requires_manual_check · 수동 확인</Pill>;
  }
  if (liveActivityStatuses.has(row.status)) {
    return <Pill tone="danger">{row.status} anomaly</Pill>;
  }
  const tone = row.status === "blocked" ? "warning" : row.status === "paper" ? "safe" : "neutral";
  return <Pill tone={tone}>{row.status}</Pill>;
}
