import { useEffect, useRef } from "react";
import { LockKeyhole, ShieldCheck } from "lucide-react";
import { getPageLabel, navItems } from "../lib/navigation";
import type { PageKey } from "../lib/navigation";
import { useOptionalOperationsSnapshot } from "../lib/operationsSnapshotContext";
import { operationsErrorTitle } from "../lib/operationsData";
import { OperationsStatusRail } from "./operations/OperationsStatusRail";
import { formatOperationsRoles } from "../lib/presentation";
import { Pill } from "./ui";

export type DeviceConnectionState =
  | "checking"
  | "connected"
  | "disconnected"
  | "setup-required"
  | "error";

const connectionStateCopy: Record<
  Exclude<DeviceConnectionState, "connected">,
  { readonly account: string; readonly detail: string; readonly assurance: string }
> = {
  checking: {
    account: "기기 연결 확인 중",
    detail: "저장된 세션 확인 중",
    assurance: "세션 확인 중"
  },
  disconnected: {
    account: "기기 연결 필요",
    detail: "운영 세션 없음",
    assurance: "기기 연결 필요"
  },
  "setup-required": {
    account: "연결 설정 필요",
    detail: "환경 설정 확인 필요",
    assurance: "연결 설정 필요"
  },
  error: {
    account: "기기 연결 확인 불가",
    detail: "계정 상태 다시 확인",
    assurance: "인증 상태 확인 불가"
  }
};

export function AppLayout({
  page,
  setPage,
  preloadPage,
  connectionState,
  children
}: {
  readonly page: PageKey;
  readonly setPage: (page: PageKey) => void;
  readonly preloadPage?: (page: PageKey) => void;
  readonly connectionState: DeviceConnectionState;
  readonly children: React.ReactNode;
}) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const snapshotContext = useOptionalOperationsSnapshot();
  const snapshot = snapshotContext?.snapshot;
  const actor = snapshot?.access.actor ?? null;
  const disconnectedCopy = connectionState === "connected"
    ? null
    : connectionStateCopy[connectionState];
  const accountLabel = actor?.display_name ?? (
    disconnectedCopy?.account ?? (
      snapshot === undefined
        ? "이 기기 연결됨"
        : snapshot.access.signed_in
          ? "계정 정보 확인 불가"
          : "기기 연결 필요"
    )
  );
  const accountDetail = actor !== null
    ? formatOperationsRoles(actor.roles)
    : disconnectedCopy?.detail ?? (
      snapshotContext?.isLoading === true
        ? "운영 상태 확인 중"
        : snapshot === undefined
          ? snapshotContext?.error
            ? operationsErrorTitle(snapshotContext.error)
            : "운영 상태 대기"
          : snapshot.access.signed_in
            ? "역할 확인 불가"
            : "운영 세션 없음"
    );
  const assuranceLabel = snapshot?.access.assurance_level === "aal2"
    ? "2단계 인증"
    : disconnectedCopy?.assurance ?? (
      snapshotContext?.isLoading === true
        ? "운영 권한 확인 중"
        : snapshot === undefined
          ? "운영 권한 확인 대기"
          : snapshot.access.session_state === "active"
            ? "2단계 인증 필요"
            : "기기 재연결 필요"
    );

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
                <p className="max-w-56 truncate text-sm font-semibold">{accountLabel}</p>
                <p className="max-w-56 truncate text-xs text-mutedStrong">{accountDetail}</p>
              </div>
              <Pill tone={snapshot?.access.assurance_level === "aal2" ? "safe" : "warning"}>
                <LockKeyhole size={13} aria-hidden="true" />
                {assuranceLabel}
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
