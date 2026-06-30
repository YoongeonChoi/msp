import { Activity, AlertTriangle, Info } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import {
  fetchApiHealth,
  fetchBotSettings,
  fetchEngineEvents,
  fetchLatestHeartbeat,
  fetchTodayDecisions,
  fetchTodayOrders
} from "../lib/supabaseData";
import { formatAge, formatKst, isOlderThan } from "../lib/formatters";
import { useAdminAccess } from "../lib/useAdminAccess";
import { AuthRequiredBlock } from "../components/AuthRequiredState";
import { EmptyState, ErrorState, JsonSummary, Metric, Panel, Pill, SectionTitle } from "../components/ui";

const monitoredProviders = [
  { label: "NAVER", keys: ["naver"] },
  { label: "OPENDART", keys: ["opendart"] },
  { label: "MARKET DATA", keys: ["krx", "toss_market_data"] },
  { label: "OPENAI", keys: ["openai"] },
  { label: "TOSS", keys: ["toss"] }
];

export function DashboardPage() {
  const adminAccess = useAdminAccess();
  const settings = useQuery({ queryKey: ["bot_settings"], queryFn: fetchBotSettings, refetchInterval: 30_000 });
  const heartbeat = useQuery({ queryKey: ["worker_heartbeats", "latest"], queryFn: fetchLatestHeartbeat, refetchInterval: 30_000 });
  const apiHealth = useQuery({ queryKey: ["api_health"], queryFn: fetchApiHealth, refetchInterval: 60_000 });
  const decisions = useQuery({ queryKey: ["decision_snapshots", "today"], queryFn: fetchTodayDecisions, refetchInterval: 60_000 });
  const orders = useQuery({ queryKey: ["orders", "today"], queryFn: fetchTodayOrders, refetchInterval: 60_000 });
  const events = useQuery({ queryKey: ["engine_events"], queryFn: () => fetchEngineEvents(20), refetchInterval: 60_000 });

  if (settings.error || heartbeat.error || apiHealth.error) {
    return <ErrorState message="핵심 상태 테이블을 읽지 못했습니다." />;
  }

  const latestHeartbeat = heartbeat.data;
  const dataAccessLimited = adminAccess.isLimited;
  const heartbeatStale = !dataAccessLimited && isOlderThan(latestHeartbeat?.createdAt, 120);
  const decisionCounts = countBy(decisions.data ?? [], (item) => item.action);
  const orderCounts = countBy(orders.data ?? [], (item) => item.status);
  const warningEvents =
    events.data?.filter((event) => ["warning", "error", "critical"].includes(event.level)).slice(0, 8) ?? [];

  return (
    <div className="space-y-4">
      {adminAccess.warning ? (
        <Panel className="border-amber-200 bg-amber-50">
          <div className="flex items-center gap-2 text-sm font-semibold text-amber-900">
            <AlertTriangle size={16} aria-hidden="true" />
            {adminAccess.warning} Settings에서 admin 계정으로 로그인하면 데이터/provider 상태가 표시됩니다.
          </div>
        </Panel>
      ) : null}

      <div className="grid gap-3 md:grid-cols-4">
        <Metric
          title="거래 봇"
          value={formatBotStatus(settings.data, dataAccessLimited)}
          detail={settings.data ? (settings.data.enabled ? "enabled=true" : "enabled=false") : dataAccessLimited ? "admin 필요" : "settings 없음"}
          tone={settings.data ? (settings.data.enabled ? "safe" : "danger") : "warning"}
        />
        <Metric
          title="데이터 수집"
          value={formatWorkerStatus(latestHeartbeat?.createdAt, dataAccessLimited)}
          detail={dataAccessLimited ? "admin 필요" : formatAge(latestHeartbeat?.createdAt)}
          tone={dataAccessLimited || heartbeatStale ? "warning" : "safe"}
        />
        <Metric
          title="오늘 주문"
          value={String((orders.data ?? []).filter((order) => ["paper", "proposed", "blocked"].includes(order.status)).length)}
          detail="오늘"
          tone="neutral"
        />
        <Metric
          title="실주문 경로"
          value={settings.data ? (settings.data.liveOrderAllowed ? "위험" : "차단") : dataAccessLimited ? "권한 필요" : "설정 없음"}
          detail={settings.data?.mode ?? (dataAccessLimited ? "admin 필요" : "paper")}
          tone={settings.data ? (settings.data.liveOrderAllowed ? "danger" : "safe") : "warning"}
        />
      </div>

      {heartbeatStale ? (
        <Panel className="border-amber-200 bg-amber-50">
          <div className="flex items-center gap-2 text-sm font-semibold text-amber-900">
            <AlertTriangle size={16} aria-hidden="true" />
            데이터 수집 heartbeat가 2분 이상 갱신되지 않았습니다. 거래 봇 정지와는 별개로 확인이 필요합니다.
          </div>
        </Panel>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-[1fr_1fr]">
        <Panel>
          <SectionTitle title="Provider Health" detail={<Activity size={18} aria-hidden="true" />} />
          <div className="grid gap-2 sm:grid-cols-2">
            {monitoredProviders.map((provider) => {
              const item = apiHealth.data?.find((health) => provider.keys.includes(health.provider.toLowerCase()));
              return (
                <div key={provider.label} className="rounded-md border border-line p-3">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium uppercase text-ink">{provider.label}</span>
                    <Pill tone={item?.healthy ? "safe" : "warning"}>
                      {item ? (item.healthy ? "정상" : item.status) : dataAccessLimited ? "권한 필요" : "미확인"}
                    </Pill>
                  </div>
                  <p className="mt-1 text-xs text-muted">
                    {item?.message ??
                      item?.errorCode ??
                      (dataAccessLimited ? "Supabase admin 세션이 필요합니다. 거래 봇 정지와는 별개입니다." : `최근 확인: ${formatKst(item?.checkedAt)}`)}
                  </p>
                </div>
              );
            })}
          </div>
        </Panel>

        <Panel>
          <SectionTitle title="Paper Trading 상태" detail={<Info size={18} aria-hidden="true" />} />
          <div className="space-y-2 text-sm text-muted">
            <p>Paper mode에서는 decision_snapshots와 paper/proposed/blocked orders만 모니터링합니다.</p>
            <p>enabled=false는 주문 생성만 멈추며, 데이터 조회와 provider 상태 표시는 Supabase 권한과 데이터 수집 heartbeat로 판단합니다.</p>
            <p>Desktop은 broker 호출을 하지 않으며, 모든 변경은 Supabase RLS 정책을 통과해야 합니다.</p>
            <p>live_order_allowed=true가 보이면 전역 위험 배너와 Emergency Stop을 우선 확인하세요.</p>
          </div>
        </Panel>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Panel>
          <SectionTitle title="오늘 decision" />
          <CountRows
            counts={decisionCounts}
            emptyLabel="오늘 decision이 없습니다."
            dataAccessLimited={dataAccessLimited}
            surface="decision_snapshots"
          />
        </Panel>
        <Panel>
          <SectionTitle title="오늘 주문 상태" />
          <CountRows
            counts={orderCounts}
            emptyLabel="오늘 주문이 없습니다."
            dataAccessLimited={dataAccessLimited}
            surface="orders"
          />
        </Panel>
        <Panel>
          <SectionTitle title="최근 경고 이벤트" />
          {warningEvents.length === 0 ? (
            dataAccessLimited ? (
              <AuthRequiredBlock surface="engine_events" />
            ) : (
              <EmptyState title="위험 이벤트 없음" detail="warning/error/critical engine_event가 없습니다." />
            )
          ) : (
            <div className="space-y-2">
              {warningEvents.map((event) => (
                <div key={event.id} className="rounded-md border border-line p-3">
                  <div className="flex items-center justify-between gap-2">
                    <Pill tone={event.level === "critical" || event.level === "error" ? "danger" : "warning"}>
                      {event.level}
                    </Pill>
                    <span className="text-xs text-muted">{formatKst(event.createdAt)}</span>
                  </div>
                  <p className="mt-2 text-sm font-medium text-ink">{event.message}</p>
                  <JsonSummary value={event.details} />
                </div>
              ))}
            </div>
          )}
        </Panel>
      </div>
    </div>
  );
}

function formatBotStatus(
  settings: { readonly enabled: boolean } | null | undefined,
  dataAccessLimited: boolean
): string {
  if (!settings) {
    return dataAccessLimited ? "권한 필요" : "설정 없음";
  }
  return settings.enabled ? "실행" : "정지";
}

function formatWorkerStatus(createdAt: string | null | undefined, dataAccessLimited: boolean): string {
  if (dataAccessLimited) {
    return "권한 필요";
  }
  return isOlderThan(createdAt, 120) ? "확인 필요" : "온라인";
}

function CountRows({
  counts,
  emptyLabel,
  dataAccessLimited,
  surface
}: {
  readonly counts: ReadonlyMap<string, number>;
  readonly emptyLabel: string;
  readonly dataAccessLimited: boolean;
  readonly surface: string;
}) {
  if (counts.size === 0) {
    return dataAccessLimited ? (
      <AuthRequiredBlock surface={surface} />
    ) : (
      <EmptyState title={emptyLabel} detail="데이터 수집기가 아직 수집 전이거나 Supabase 연결 확인이 필요합니다." />
    );
  }
  return (
    <div className="space-y-2">
      {Array.from(counts.entries()).map(([key, value]) => (
        <div key={key} className="flex items-center justify-between rounded-md border border-line px-3 py-2 text-sm">
          <span className="font-medium text-ink">{key}</span>
          <Pill tone="neutral">{value}</Pill>
        </div>
      ))}
    </div>
  );
}

function countBy<T>(items: readonly T[], getKey: (item: T) => string): ReadonlyMap<string, number> {
  const counts = new Map<string, number>();
  for (const item of items) {
    const key = getKey(item) || "unknown";
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return counts;
}
