import { useQuery } from "@tanstack/react-query";
import { fetchAuditLogs, fetchEngineEvents } from "../lib/supabaseData";
import { formatKst } from "../lib/formatters";
import { useAdminAccess } from "../lib/useAdminAccess";
import type { AuditLogRow, EngineEventRow } from "../lib/rows";
import { AuthRequiredBlock } from "../components/AuthRequiredState";
import { EmptyState, ErrorState, JsonSummary, LoadingState, Panel, Pill, SectionTitle } from "../components/ui";

const auditTableLabels: Readonly<Record<string, string>> = {
  bot_settings: "거래 봇 설정",
  watchlist: "관심종목",
  strategy_versions: "전략 버전",
  ai_upgrade_candidates: "AI 후보",
  manual_commands: "수동 명령"
};

const auditFieldLabels: Readonly<Record<string, string>> = {
  enabled: "거래 봇 실행",
  mode: "거래 모드",
  live_order_allowed: "실주문 허용",
  deployment_lock: "배포 안전 잠금",
  max_order_amount_krw: "최대 주문 금액",
  max_daily_loss_pct: "최대 일 손실",
  max_daily_order_count: "일 주문 횟수",
  max_position_pct: "종목 비중",
  max_sector_pct: "섹터 비중",
  status: "상태",
  symbol: "종목코드",
  market: "시장",
  sector: "섹터",
  weights: "가중치",
  weights_json: "가중치",
  params: "전략 파라미터",
  params_json: "전략 파라미터",
  reviewed_at: "검토 시각",
  reviewed_by: "검토자",
  rejection_reason: "거절 사유"
};

const hiddenAuditMetadataFields = new Set(["id", "created_at", "updated_at"]);

export function LogsPage() {
  const adminAccess = useAdminAccess();
  const auditLogs = useQuery({
    queryKey: ["audit_logs", "recent"],
    queryFn: () => fetchAuditLogs(100),
    enabled: adminAccess.isAdmin,
    refetchInterval: adminAccess.isAdmin ? 60_000 : false
  });
  const events = useQuery({
    queryKey: ["engine_events"],
    queryFn: () => fetchEngineEvents(100),
    refetchInterval: 60_000
  });

  return (
    <div className="space-y-4">
      <AuditLogSection
        rows={adminAccess.isAdmin ? auditLogs.data ?? [] : []}
        isLoading={adminAccess.isAdmin && auditLogs.isLoading}
        isError={adminAccess.isAdmin && auditLogs.isError}
        dataAccessLimited={!adminAccess.isAdmin}
      />
      <EngineEventSection
        rows={events.data ?? []}
        isLoading={events.isLoading}
        isError={events.isError}
        dataAccessLimited={adminAccess.isLimited}
      />
    </div>
  );
}

function AuditLogSection({
  rows,
  isLoading,
  isError,
  dataAccessLimited
}: {
  readonly rows: readonly AuditLogRow[];
  readonly isLoading: boolean;
  readonly isError: boolean;
  readonly dataAccessLimited: boolean;
}) {
  return (
    <Panel>
      <SectionTitle title="변경 감사 로그" detail={<Pill tone="info">읽기 전용</Pill>} />
      <p className="mb-3 text-sm text-muted">
        위험 설정과 운영 데이터의 변경 이력입니다. 행위자 UUID와 원본 스냅샷 값은 desktop으로 전송하지 않습니다.
      </p>
      {isLoading ? <LoadingState label="audit_logs를 불러오는 중" /> : null}
      {isError ? <ErrorState message="audit_logs를 읽지 못했습니다." /> : null}
      {!isLoading && !isError && rows.length === 0 ? (
        dataAccessLimited ? (
          <AuthRequiredBlock surface="audit_logs" />
        ) : (
          <EmptyState title="감사 로그 없음" detail="관리자 변경이 기록되면 안전한 요약이 표시됩니다." />
        )
      ) : null}
      {!isLoading && !isError && rows.length > 0 ? (
        <div className="space-y-2" role="list" aria-label="변경 감사 로그 목록">
          {rows.map((row) => (
            <AuditLogCard key={row.id} row={row} />
          ))}
        </div>
      ) : null}
    </Panel>
  );
}

function AuditLogCard({ row }: { readonly row: AuditLogRow }) {
  const fields = changedAuditFields(row);
  return (
    <article className="rounded-md border border-line p-3" role="listitem">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <Pill tone={auditActionTone(row.action)}>{auditActionLabel(row.action)}</Pill>
          <span className="text-sm font-semibold text-ink">{auditTableLabels[row.targetTable] ?? row.targetTable}</span>
          <span className="text-xs text-muted">대상 {compactTargetId(row.targetId)}</span>
        </div>
        <time className="text-xs text-muted" dateTime={row.createdAt ?? undefined}>
          {formatKst(row.createdAt)}
        </time>
      </div>
      <p className="mt-2 text-sm text-muted">
        {fields.length > 0 ? `변경 필드: ${fields.join(", ")}` : "변경 필드 요약 없음"}
      </p>
    </article>
  );
}

function EngineEventSection({
  rows,
  isLoading,
  isError,
  dataAccessLimited
}: {
  readonly rows: readonly EngineEventRow[];
  readonly isLoading: boolean;
  readonly isError: boolean;
  readonly dataAccessLimited: boolean;
}) {
  return (
    <Panel>
      <SectionTitle title="엔진 이벤트" />
      {isLoading ? <LoadingState label="engine_events를 불러오는 중" /> : null}
      {isError ? <ErrorState message="engine_events를 읽지 못했습니다." /> : null}
      {!isLoading && !isError && rows.length === 0 ? (
        dataAccessLimited ? (
          <AuthRequiredBlock surface="engine_events" />
        ) : (
          <EmptyState title="로그 없음" detail="worker event가 저장되면 표시됩니다." />
        )
      ) : null}
      {!isLoading && !isError && rows.length > 0 ? (
        <div className="space-y-2">
          {rows.map((event) => (
            <div key={event.id} className="rounded-md border border-line p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <Pill tone={event.level === "critical" || event.level === "error" ? "danger" : event.level === "warning" ? "warning" : "neutral"}>
                    {event.level}
                  </Pill>
                  <span className="text-sm font-medium text-ink">{event.component}</span>
                </div>
                <span className="text-xs text-muted">{formatKst(event.createdAt)}</span>
              </div>
              <p className="mt-2 text-sm text-ink">{event.message}</p>
              <div className="mt-1"><JsonSummary value={event.details} /></div>
            </div>
          ))}
        </div>
      ) : null}
    </Panel>
  );
}

export function changedAuditFields(row: AuditLogRow): readonly string[] {
  const changed = row.changedFields
    .filter((key) => !hiddenAuditMetadataFields.has(key))
    .map((key) => auditFieldLabels[key] ?? key)
    .sort((left, right) => left.localeCompare(right, "ko-KR"));
  if (changed.length <= 6) {
    return changed;
  }
  return [...changed.slice(0, 6), `외 ${changed.length - 6}개`];
}

function auditActionLabel(action: string): string {
  if (action === "insert") {
    return "생성";
  }
  if (action === "update") {
    return "변경";
  }
  if (action === "delete") {
    return "삭제";
  }
  return "기타";
}

function auditActionTone(action: string): "neutral" | "safe" | "danger" | "info" {
  if (action === "insert") {
    return "safe";
  }
  if (action === "delete") {
    return "danger";
  }
  if (action === "update") {
    return "info";
  }
  return "neutral";
}

function compactTargetId(value: string | null): string {
  if (!value) {
    return "-";
  }
  if (value.length <= 16) {
    return value;
  }
  return `${value.slice(0, 8)}…`;
}
