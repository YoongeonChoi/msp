import { AlertTriangle, CircleStop, Database } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchApiHealth, fetchBotSettings, fetchLatestHeartbeat, fetchTodayDecisions, fetchTodayOrders, updateBotSettings } from "../lib/supabaseData";
import { formatAge, isOlderThan } from "../lib/formatters";
import { brandIcon, getPageLabel, navItems, parsePageKey } from "../lib/navigation";
import type { PageKey } from "../lib/navigation";
import { pageButtonClass, Pill } from "./ui";

const BrandIcon = brandIcon;

export function AppLayout({
  page,
  setPage,
  children
}: {
  readonly page: PageKey;
  readonly setPage: (page: PageKey) => void;
  readonly children: React.ReactNode;
}) {
  return (
    <div className="min-h-screen bg-slate-100 text-ink">
      <div className="flex min-h-screen">
        <Sidebar page={page} setPage={setPage} />
        <main className="min-w-0 flex-1">
          <StatusBar />
          <MobileNav page={page} setPage={setPage} />
          <div className="mx-auto max-w-7xl p-4">
            <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
              <div>
                <h1 className="text-xl font-semibold text-ink">{getPageLabel(page)}</h1>
                <p className="text-sm text-muted">KST 기준 · Desktop은 Supabase RLS를 통한 control plane입니다.</p>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Pill tone="danger">
                  <AlertTriangle size={13} aria-hidden="true" />
                  실주문 기본 차단
                </Pill>
                <Pill tone="info">
                  <Database size={13} aria-hidden="true" />
                  Supabase
                </Pill>
              </div>
            </div>
            {children}
          </div>
        </main>
      </div>
    </div>
  );
}

function Sidebar({ page, setPage }: { readonly page: PageKey; readonly setPage: (page: PageKey) => void }) {
  return (
    <aside className="hidden w-64 shrink-0 border-r border-line bg-slate-950 text-white md:block">
      <div className="px-4 py-5">
        <div className="flex items-center gap-2 text-base font-semibold">
          <BrandIcon size={20} aria-hidden="true" />
          KR Trading Lab
        </div>
        <p className="mt-1 text-xs text-slate-300">Paper Trading monitoring cockpit</p>
      </div>
      <nav className="space-y-1 px-2">
        {navItems.map((item) => {
          const Icon = item.icon;
          return (
            <button
              key={item.key}
              onClick={() => setPage(item.key)}
              className={`flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm focus:outline-none focus:ring-2 focus:ring-white ${
                page === item.key ? "bg-white text-slate-950" : "text-slate-200 hover:bg-slate-800"
              }`}
            >
              <Icon size={16} aria-hidden="true" />
              {item.label}
            </button>
          );
        })}
      </nav>
    </aside>
  );
}

function MobileNav({ page, setPage }: { readonly page: PageKey; readonly setPage: (page: PageKey) => void }) {
  return (
    <div className="border-b border-line bg-white px-4 py-3 md:hidden">
      <select
        value={page}
        onChange={(event) => {
          const nextPage = parsePageKey(event.currentTarget.value);
          if (nextPage) {
            setPage(nextPage);
          }
        }}
        className="w-full rounded-md border border-line px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-slate-400"
        aria-label="페이지 선택"
      >
        {navItems.map((item) => (
          <option key={item.key} value={item.key}>
            {item.label}
          </option>
        ))}
      </select>
    </div>
  );
}

function StatusBar() {
  const queryClient = useQueryClient();
  const settings = useQuery({ queryKey: ["bot_settings"], queryFn: fetchBotSettings, refetchInterval: 30_000 });
  const heartbeat = useQuery({ queryKey: ["worker_heartbeats", "latest"], queryFn: fetchLatestHeartbeat, refetchInterval: 30_000 });
  const apiHealth = useQuery({ queryKey: ["api_health"], queryFn: fetchApiHealth, refetchInterval: 60_000 });
  const todayDecisions = useQuery({ queryKey: ["decision_snapshots", "today"], queryFn: fetchTodayDecisions, refetchInterval: 60_000 });
  const todayOrders = useQuery({ queryKey: ["orders", "today"], queryFn: fetchTodayOrders, refetchInterval: 60_000 });

  const emergencyStop = useMutation({
    mutationFn: () => updateBotSettings({ enabled: false, liveOrderAllowed: false }),
    onSuccess: () => queryClient.invalidateQueries()
  });

  const currentSettings = settings.data;
  const latestHeartbeat = heartbeat.data;
  const heartbeatStale = isOlderThan(latestHeartbeat?.createdAt, 120);
  const healthyCount = apiHealth.data?.filter((item) => item.healthy).length ?? 0;
  const unhealthyCount = apiHealth.data ? apiHealth.data.length - healthyCount : 0;
  const paperOrders =
    todayOrders.data?.filter((order) => ["paper", "proposed", "blocked"].includes(order.status)).length ?? 0;

  return (
    <header className="border-b border-line bg-white">
      <div className="flex flex-wrap items-center gap-2 px-4 py-3">
        <Pill tone={currentSettings?.enabled ? "safe" : "danger"}>
          봇 상태: {currentSettings?.enabled ? "실행" : "정지"}
        </Pill>
        <Pill tone={currentSettings?.mode === "live" ? "danger" : "safe"}>모드: {currentSettings?.mode ?? "-"}</Pill>
        <Pill tone={currentSettings?.liveOrderAllowed ? "danger" : "safe"}>
          실주문 허용: {currentSettings?.liveOrderAllowed ? "예" : "아니오"}
        </Pill>
        <Pill tone={heartbeatStale ? "warning" : "safe"}>Heartbeat: {formatAge(latestHeartbeat?.createdAt)}</Pill>
        <Pill tone={unhealthyCount > 0 ? "warning" : "safe"}>
          API: 정상 {healthyCount} / 이상 {unhealthyCount}
        </Pill>
        <Pill tone="neutral">오늘 decision: {todayDecisions.data?.length ?? 0}</Pill>
        <Pill tone="neutral">오늘 paper/proposed/blocked: {paperOrders}</Pill>
        <button
          className={`${pageButtonClass("danger")} ml-auto`}
          onClick={() => {
            if (window.confirm("Emergency stop을 실행할까요? enabled=false, live_order_allowed=false로 변경됩니다.")) {
              emergencyStop.mutate();
            }
          }}
          disabled={emergencyStop.isPending}
        >
          <CircleStop size={16} aria-hidden="true" />
          Emergency Stop
        </button>
      </div>
      {currentSettings?.liveOrderAllowed ? (
        <div className="border-t border-red-200 bg-red-50 px-4 py-2 text-sm font-semibold text-red-800">
          위험 배너: live_order_allowed=true 상태입니다. Paper Trading 검증 중에는 즉시 Emergency Stop을 권장합니다.
        </div>
      ) : null}
      {settings.error ? (
        <div className="border-t border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-800">
          Supabase 설정 또는 RLS 권한 때문에 bot_settings를 읽지 못했습니다.
        </div>
      ) : null}
    </header>
  );
}
