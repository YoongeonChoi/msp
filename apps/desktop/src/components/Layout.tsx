import { AlertTriangle, Database } from "lucide-react";
import { brandIcon, getPageLabel, navItems, parsePageKey } from "../lib/navigation";
import type { PageKey } from "../lib/navigation";
import { OperationsStatusRail } from "./operations/OperationsStatusRail";
import { Pill } from "./ui";

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
          <OperationsStatusRail />
          <MobileNav page={page} setPage={setPage} />
          <div className="mx-auto max-w-7xl p-4">
            <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
              <div>
                <h1 className="text-xl font-semibold text-ink">{getPageLabel(page)}</h1>
                <p className="text-sm text-muted">KST 기준 · Desktop은 엄격한 Supabase RPC/read model control plane입니다.</p>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Pill tone="danger">
                  <AlertTriangle size={13} aria-hidden="true" />
                  LIVE 영구 금지
                </Pill>
                <Pill tone="info">
                  <Database size={13} aria-hidden="true" />
                  Supabase control plane
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
        <p className="mt-1 text-xs text-slate-300">PAPER · CONTRACT TEST operations cockpit</p>
      </div>
      <nav className="space-y-1 px-2" aria-label="주 탐색">
        {navItems.map((item) => {
          const Icon = item.icon;
          return (
            <button
              key={item.key}
              type="button"
              onClick={() => setPage(item.key)}
              aria-current={page === item.key ? "page" : undefined}
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
