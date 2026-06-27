import {
  AlertTriangle,
  Activity,
  Bell,
  CircleStop,
  Database,
  LineChart,
  Lock,
  Power,
  ShieldCheck
} from "lucide-react";
import type { ReactNode } from "react";
import { useMemo, useState } from "react";
import { Line, LineChart as ReLineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { chartData, decisions, navItems, orders, PageKey, positions, providerHealth } from "./lib/mockData";

type PillTone = "neutral" | "safe" | "danger" | "warning";

function Pill({ children, tone = "neutral" }: { children: ReactNode; tone?: PillTone }) {
  const classes: Record<PillTone, string> = {
    neutral: "border-line bg-white text-ink",
    safe: "border-emerald-200 bg-emerald-50 text-emerald-800",
    danger: "border-red-200 bg-red-50 text-red-800",
    warning: "border-amber-200 bg-amber-50 text-amber-800"
  };
  return (
    <span className={`inline-flex items-center rounded-md border px-2 py-1 text-xs font-medium ${classes[tone]}`}>
      {children}
    </span>
  );
}

function MetricCard({
  title,
  value,
  detail,
  tone = "neutral"
}: {
  title: string;
  value: string;
  detail: string;
  tone?: PillTone;
}) {
  return (
    <section className="rounded-md border border-line bg-white p-4">
      <div className="flex items-center justify-between gap-3">
        <p className="text-sm font-medium text-muted">{title}</p>
        <Pill tone={tone}>{detail}</Pill>
      </div>
      <p className="mt-3 text-2xl font-semibold text-ink">{value}</p>
    </section>
  );
}

function StatusBar() {
  return (
    <header className="flex flex-wrap items-center gap-2 border-b border-line bg-white px-4 py-3">
      <Pill tone="danger">봇 상태: 정지</Pill>
      <Pill tone="safe">모드: paper</Pill>
      <Pill tone="danger">실주문 허용: false</Pill>
      <Pill tone="safe">Heartbeat: 18초 전</Pill>
      <Pill tone="safe">API: 정상 5 / 주의 1</Pill>
      <Pill tone="neutral">오늘 손익: +0.3%</Pill>
      <Pill tone="neutral">오늘 주문: 1 / 10</Pill>
      <Pill tone="warning">위험 경고: 1</Pill>
      <button className="ml-auto inline-flex h-9 items-center gap-2 rounded-md border border-red-200 bg-red-50 px-3 text-sm font-semibold text-red-800 focus:outline-none focus:ring-2 focus:ring-red-400">
        <CircleStop size={16} aria-hidden="true" />
        Emergency Stop
      </button>
    </header>
  );
}

function Sidebar({ page, setPage }: { page: PageKey; setPage: (page: PageKey) => void }) {
  return (
    <aside className="hidden w-60 shrink-0 border-r border-line bg-slate-950 text-white md:block">
      <div className="px-4 py-5">
        <div className="flex items-center gap-2 text-base font-semibold">
          <ShieldCheck size={20} aria-hidden="true" />
          KR Trading Lab
        </div>
        <p className="mt-1 text-xs text-slate-300">안전 우선 자동매매 cockpit</p>
      </div>
      <nav className="space-y-1 px-2">
        {navItems.map((item) => (
          <button
            key={item.key}
            onClick={() => setPage(item.key)}
            className={`w-full rounded-md px-3 py-2 text-left text-sm focus:outline-none focus:ring-2 focus:ring-white ${
              page === item.key ? "bg-white text-slate-950" : "text-slate-200 hover:bg-slate-800"
            }`}
          >
            {item.label}
          </button>
        ))}
      </nav>
    </aside>
  );
}

function Dashboard() {
  return (
    <div className="space-y-4">
      <div className="grid gap-3 md:grid-cols-4">
        <MetricCard title="봇 상태" value="정지" detail="enabled=false" tone="danger" />
        <MetricCard title="Worker 상태" value="정상" detail="18초 전" tone="safe" />
        <MetricCard title="일 손실 사용률" value="0.0 / 2.0%" detail="safe" tone="safe" />
        <MetricCard title="실주문 경로" value="차단" detail="live=false" tone="danger" />
      </div>
      <div className="grid gap-4 lg:grid-cols-[1.2fr_0.8fr]">
        <section className="rounded-md border border-line bg-white p-4">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-base font-semibold text-ink">최근 의사결정</h2>
            <LineChart size={18} aria-hidden="true" />
          </div>
          <div className="overflow-x-auto">
            <table className="min-w-full text-sm">
              <thead className="text-left text-muted">
                <tr>
                  <th className="py-2">종목</th>
                  <th>액션</th>
                  <th>점수</th>
                  <th>위험</th>
                  <th>시간</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {decisions.map((decision) => (
                  <tr key={`${decision.symbol}-${decision.time}`}>
                    <td className="py-2 font-medium text-ink">{decision.symbol}</td>
                    <td>{decision.action}</td>
                    <td>{decision.score.toFixed(2)}</td>
                    <td>{decision.risk}</td>
                    <td>{decision.time}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
        <section className="rounded-md border border-line bg-white p-4">
          <h2 className="text-base font-semibold text-ink">오늘 손익</h2>
          <div className="mt-4 h-52">
            <ResponsiveContainer width="100%" height="100%">
              <ReLineChart data={chartData}>
                <XAxis dataKey="day" />
                <YAxis width={32} />
                <Tooltip />
                <Line type="monotone" dataKey="pnl" stroke="#047857" strokeWidth={2} dot={false} />
              </ReLineChart>
            </ResponsiveContainer>
          </div>
        </section>
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <ProviderHealth />
        <OrdersPanel />
      </div>
    </div>
  );
}

function ProviderHealth() {
  return (
    <section className="rounded-md border border-line bg-white p-4">
      <div className="mb-3 flex items-center gap-2">
        <Activity size={18} aria-hidden="true" />
        <h2 className="text-base font-semibold text-ink">API Health</h2>
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        {providerHealth.map((item) => (
          <div key={item.provider} className="flex items-center justify-between rounded-md border border-line px-3 py-2">
            <span className="text-sm font-medium">{item.provider}</span>
            <Pill tone={item.status === "healthy" ? "safe" : "warning"}>{item.latencyMs}ms</Pill>
          </div>
        ))}
      </div>
    </section>
  );
}

function OrdersPanel() {
  return (
    <section className="rounded-md border border-line bg-white p-4">
      <h2 className="mb-3 text-base font-semibold text-ink">최근 주문</h2>
      <div className="space-y-2">
        {orders.map((order) => (
          <div key={`${order.symbol}-${order.status}`} className="rounded-md border border-line p-3">
            <div className="flex items-center justify-between">
              <span className="font-medium text-ink">{order.symbol}</span>
              <Pill tone={order.status === "blocked" ? "danger" : "safe"}>{order.status}</Pill>
            </div>
            <p className="mt-1 text-sm text-muted">
              {order.side} · {order.amount} · {order.reason}
            </p>
          </div>
        ))}
      </div>
    </section>
  );
}

function Control() {
  const [confirmText, setConfirmText] = useState("");
  const livePhrase = "실주문 위험을 이해했습니다";
  const canEnableLive = confirmText === livePhrase;
  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_0.85fr]">
      <section className="rounded-md border border-line bg-white p-4">
        <h2 className="text-base font-semibold text-ink">자동매매 제어</h2>
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <button className="inline-flex items-center justify-center gap-2 rounded-md border border-line px-4 py-3 text-sm font-semibold focus:outline-none focus:ring-2 focus:ring-slate-400">
            <Power size={16} aria-hidden="true" />
            Paper 시작
          </button>
          <button className="inline-flex items-center justify-center gap-2 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm font-semibold text-red-800 focus:outline-none focus:ring-2 focus:ring-red-400">
            <CircleStop size={16} aria-hidden="true" />
            전체 정지
          </button>
        </div>
        <div className="mt-5 rounded-md border border-red-200 bg-red-50 p-4">
          <div className="flex items-center gap-2 text-red-900">
            <Lock size={18} aria-hidden="true" />
            <h3 className="font-semibold">실주문 활성화 보호</h3>
          </div>
          <p className="mt-2 text-sm text-red-800">문구를 정확히 입력해야 live_order_allowed 변경이 가능합니다.</p>
          <input
            value={confirmText}
            onChange={(event) => setConfirmText(event.target.value)}
            aria-label="실주문 확인 문구"
            className="mt-3 w-full rounded-md border border-red-200 bg-white px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-red-400"
            placeholder={livePhrase}
          />
          <button
            disabled={!canEnableLive}
            className="mt-3 rounded-md bg-red-700 px-4 py-2 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            실주문 허용 요청
          </button>
        </div>
      </section>
      <section className="rounded-md border border-line bg-white p-4">
        <h2 className="text-base font-semibold text-ink">위험 한도</h2>
        {[
          ["최대 주문 금액", "100,000원"],
          ["최대 일 손실", "2.0%"],
          ["일 주문 횟수", "10회"],
          ["종목 비중", "10%"],
          ["섹터 비중", "30%"],
          ["Loop interval", "30초"]
        ].map(([label, value]) => (
          <div key={label} className="flex items-center justify-between border-b border-line py-3 text-sm">
            <span className="text-muted">{label}</span>
            <span className="font-medium text-ink">{value}</span>
          </div>
        ))}
      </section>
    </div>
  );
}

function SimplePage({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded-md border border-line bg-white p-4">
      <h2 className="text-base font-semibold text-ink">{title}</h2>
      <div className="mt-4">{children}</div>
    </section>
  );
}

function Portfolio() {
  return (
    <SimplePage title="Portfolio">
      <div className="grid gap-3 md:grid-cols-2">
        {positions.map((position) => (
          <div key={position.symbol} className="rounded-md border border-line p-3">
            <div className="flex items-center justify-between">
              <span className="font-medium">{position.name}</span>
              <Pill tone={position.pnl >= 0 ? "safe" : "danger"}>{position.pnl}%</Pill>
            </div>
            <p className="mt-2 text-sm text-muted">
              {position.symbol} · {position.sector} · 노출 {position.exposure}%
            </p>
          </div>
        ))}
      </div>
    </SimplePage>
  );
}

function PlaceholderPage({ page }: { page: PageKey }) {
  const copy = useMemo(
    () => ({
      Watchlist: "종목, 섹터, 목표가, 손절가, 최대 비중을 관리합니다.",
      Orders: "paper, blocked, sent, filled, unknown 상태와 risk result를 확인합니다.",
      Signals: "final score, component score, explanation, policy results를 확인합니다.",
      Fundamentals: "PER, PBR, ROE, 영업이익률, 부채비율, 성장률을 확인합니다.",
      News: "관련 뉴스의 sentiment, event type, risk level, 요약을 확인합니다.",
      "Strategy Lab": "AI 후보 전략은 proposed 상태로만 저장되고, paper 검증 전 live 승격이 차단됩니다.",
      Settings: "Supabase publishable key만 사용하며 broker secret은 저장하지 않습니다.",
      Logs: "heartbeat, engine_events, api_health, audit_logs를 확인합니다.",
      Dashboard: "",
      Control: "",
      Portfolio: ""
    }),
    []
  );
  return (
    <SimplePage title={page}>
      <div className="flex min-h-44 items-center justify-center rounded-md border border-dashed border-line bg-slate-50 p-8 text-center text-sm text-muted">
        {copy[page]}
      </div>
    </SimplePage>
  );
}

function App() {
  const [page, setPage] = useState<PageKey>("Dashboard");
  return (
    <div className="min-h-screen bg-slate-100 text-ink">
      <div className="flex min-h-screen">
        <Sidebar page={page} setPage={setPage} />
        <main className="min-w-0 flex-1">
          <StatusBar />
          <div className="border-b border-line bg-white px-4 py-3 md:hidden">
            <select
              value={page}
              onChange={(event) => setPage(event.target.value as PageKey)}
              className="w-full rounded-md border border-line px-3 py-2 text-sm"
              aria-label="페이지 선택"
            >
              {navItems.map((item) => (
                <option key={item.key} value={item.key}>
                  {item.label}
                </option>
              ))}
            </select>
          </div>
          <div className="mx-auto max-w-7xl p-4">
            <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
              <div>
                <h1 className="text-xl font-semibold text-ink">{page}</h1>
                <p className="text-sm text-muted">KST 기준 · live execution은 worker risk gate만 통과 가능</p>
              </div>
              <div className="flex items-center gap-2">
                <Pill tone="danger">
                  <AlertTriangle size={13} aria-hidden="true" />
                  <span className="ml-1">실주문 기본 차단</span>
                </Pill>
                <Pill tone="safe">
                  <Database size={13} aria-hidden="true" />
                  <span className="ml-1">Supabase Control Plane</span>
                </Pill>
                <Pill tone="warning">
                  <Bell size={13} aria-hidden="true" />
                  <span className="ml-1">1 warning</span>
                </Pill>
              </div>
            </div>
            {page === "Dashboard" && <Dashboard />}
            {page === "Control" && <Control />}
            {page === "Portfolio" && <Portfolio />}
            {!["Dashboard", "Control", "Portfolio"].includes(page) && <PlaceholderPage page={page} />}
          </div>
        </main>
      </div>
    </div>
  );
}

export default App;
