import type { ReactNode } from "react";
import { AlertTriangle, WifiOff } from "lucide-react";
import type { RuntimeHealth } from "../../lib/operationsContracts";
import { formatKst } from "../../lib/formatters";

export function StaleDataBoundary({
  health,
  isOnline,
  clientFresh,
  children
}: {
  readonly health: RuntimeHealth;
  readonly isOnline: boolean;
  readonly clientFresh: boolean;
  readonly children: ReactNode;
}) {
  const blocked = !isOnline || health.overall_state !== "fresh" || !clientFresh;
  const title = !isOnline
    ? "네트워크 오프라인"
    : health.overall_state !== "fresh"
      ? runtimeStateLabel(health.overall_state)
      : "클라이언트 신선도 검증 실패";

  return (
    <div className="space-y-4">
      {blocked ? (
        <div
          className="rounded-md border border-amber-300 bg-amber-50 p-4 text-sm text-amber-950"
          role="alert"
          aria-live="assertive"
        >
          <div className="flex items-center gap-2 font-semibold">
            {!isOnline ? <WifiOff size={17} aria-hidden="true" /> : <AlertTriangle size={17} aria-hidden="true" />}
            {title} — 거래 제어 변경 차단
          </div>
          <p className="mt-1">
            마지막 검증 시각 {formatKst(health.as_of)} · 오프라인 작업은 전송되지 않으며 재연결 후 자동 실행되는 큐를 만들지 않습니다.
            온라인이고 제어면 상태가 최신이면 사고 접수·해결만 별도 권한으로 허용될 수 있습니다.
          </p>
        </div>
      ) : null}
      {children}
    </div>
  );
}

export function runtimeStateLabel(state: RuntimeHealth["overall_state"]): string {
  const labels: Record<RuntimeHealth["overall_state"], string> = {
    fresh: "정상",
    degraded: "성능 저하",
    stale: "데이터 지연",
    offline: "서비스 오프라인",
    session_expired: "세션 만료",
    contract_error: "데이터 계약 오류"
  };
  return labels[state];
}
