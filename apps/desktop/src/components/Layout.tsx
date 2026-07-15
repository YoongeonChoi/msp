import { useEffect, useRef } from "react";
import { LockKeyhole, ShieldCheck } from "lucide-react";
import { getPageLabel, navItems } from "../lib/navigation";
import type { PageKey } from "../lib/navigation";
import { useOptionalOperationsSnapshot } from "../lib/operationsSnapshotContext";
import { OperationsStatusRail } from "./operations/OperationsStatusRail";
import { formatOperationsRoles } from "../lib/presentation";
import { Pill } from "./ui";

export function AppLayout({
  page,
  setPage,
  preloadPage,
  children
}: {
  readonly page: PageKey;
  readonly setPage: (page: PageKey) => void;
  readonly preloadPage?: (page: PageKey) => void;
  readonly children: React.ReactNode;
}) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const snapshotContext = useOptionalOperationsSnapshot();
  const snapshot = snapshotContext?.snapshot;
  const actor = snapshot?.access.actor ?? null;

  useEffect(() => {
    headingRef.current?.focus({ preventScroll: true });
  }, [page]);

  return (
    <div className="min-h-screen bg-canvas text-ink">
      <a className="skip-link" href="#main-content">본문으로 건너뛰기</a>
      <header className="app-header-glass sticky top-0 z-40" aria-label="앱 헤더">
        <div className="mx-auto max-w-[1440px] px-4 py-3 md:px-6">
          <div className="flex flex-wrap items-center justify-between gap-3 md:flex-nowrap">
            <div className="flex min-w-0 items-center gap-3">
              <span className="grid h-11 w-11 shrink-0 place-items-center rounded-lg bg-ink text-white" aria-hidden="true">
                <ShieldCheck size={22} />
              </span>
              <div className="min-w-0">
                <p className="truncate text-base font-bold">KR Trading Lab</p>
                <p className="truncate text-xs text-mutedStrong">안전 우선 운영 관리</p>
              </div>
            </div>

            <nav className="order-3 grid w-full grid-cols-2 gap-1 rounded-lg bg-canvas p-1 md:order-none md:w-auto" aria-label="주 탐색">
              {navItems.map((item) => {
                const Icon = item.icon;
                const active = item.key === page;
                return (
                  <button
                    key={item.key}
                    type="button"
                    aria-current={active ? "page" : undefined}
                    className={`inline-flex min-h-control items-center justify-center gap-2 rounded-md px-4 text-sm font-semibold transition-[transform,opacity] duration-state ${
                      active ? "bg-surface text-ink shadow-sm" : "text-mutedStrong hover:bg-surface/70"
                    }`}
                    onMouseEnter={() => preloadPage?.(item.key)}
                    onFocus={() => preloadPage?.(item.key)}
                    onClick={() => setPage(item.key)}
                  >
                    <Icon size={17} aria-hidden="true" />
                    {item.label}
                  </button>
                );
              })}
            </nav>

            <div className="flex min-w-0 items-center gap-2 text-right">
              <div className="hidden min-w-0 lg:block">
                <p className="max-w-56 truncate text-sm font-semibold">{actor?.display_name ?? "로그인 확인 중"}</p>
                <p className="max-w-56 truncate text-xs text-mutedStrong">
                  {actor === null || actor === undefined ? "역할 확인 불가" : formatOperationsRoles(actor.roles)}
                </p>
              </div>
              <Pill tone={snapshot?.access.assurance_level === "aal2" ? "safe" : "warning"}>
                <LockKeyhole size={13} aria-hidden="true" />
                {snapshot?.access.assurance_level === "aal2" ? "2단계 인증" : "추가 인증 필요"}
              </Pill>
            </div>
          </div>
        </div>
        <OperationsStatusRail />
      </header>

      <main id="main-content" className="mx-auto max-w-[1440px] px-4 py-6 md:px-6 md:py-8" tabIndex={-1}>
        <div className="mb-6">
          <h1 ref={headingRef} tabIndex={-1} className="text-[28px] font-bold leading-10 outline-none">
            {getPageLabel(page)}
          </h1>
          <p className="mt-1 text-sm text-mutedStrong">
            {page === "control"
              ? "핵심 상태와 다음 행동을 먼저 확인하고, 상세 증거는 필요할 때만 엽니다."
              : "계정, 역할, 2단계 인증과 접근권한을 안전하게 관리합니다."}
          </p>
        </div>
        {children}
      </main>
    </div>
  );
}
