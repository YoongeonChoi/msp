import { useCallback, useEffect, useRef, useState } from "react";
import { X } from "lucide-react";

import { formatKst } from "../lib/formatters";
import { getPageDescription, getPageLabel, type PageKey } from "../lib/navigation";
import { operationsErrorTitle } from "../lib/operationsData";
import { useOptionalOperationsSnapshot } from "../lib/operationsSnapshotContext";
import { formatOperationsRoles } from "../lib/presentation";
import { OperationsStatusRail } from "./operations/OperationsStatusRail";
import { AppSidebar } from "./shell/AppSidebar";
import { UtilityHeader } from "./shell/UtilityHeader";

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
  const previousPageRef = useRef(page);
  const mobileNavigationRef = useRef<HTMLDivElement>(null);
  const [mobileNavigationOpen, setMobileNavigationOpen] = useState(false);
  const snapshotContext = useOptionalOperationsSnapshot();
  const snapshot = snapshotContext?.snapshot;
  const actor = snapshot?.access.actor ?? null;
  const connected = connectionState === "connected";
  const disconnectedCopy = connected ? null : connectionStateCopy[connectionState];
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
    ? "2단계 인증 확인"
    : disconnectedCopy?.assurance ?? (
      snapshotContext?.isLoading === true
        ? "운영 권한 확인 중"
        : snapshot === undefined
          ? "운영 권한 확인 대기"
          : snapshot.access.session_state === "active"
            ? "2단계 인증 필요"
            : "기기 재연결 필요"
    );
  const environmentLabel = snapshot?.runtime_health.environment === "paper"
    ? "PAPER"
    : snapshot?.runtime_health.environment === "contract_test"
      ? "CONTRACT TEST"
      : "환경 확인 필요";
  const snapshotLabel = snapshot
    ? `상태 r${snapshot.runtime_health.state_version} · ${formatKst(snapshot.runtime_health.as_of)}`
    : snapshotContext?.isLoading
      ? "최신 운영 상태 확인 중"
      : "운영 상태 없음";

  useEffect(() => {
    if (previousPageRef.current !== page) {
      headingRef.current?.focus({ preventScroll: true });
      previousPageRef.current = page;
    }
  }, [page]);

  useEffect(() => {
    if (!mobileNavigationOpen) {
      return;
    }
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    mobileNavigationRef.current
      ?.querySelector<HTMLButtonElement>('button[aria-current="page"]')
      ?.focus({ preventScroll: true });
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setMobileNavigationOpen(false);
        return;
      }
      if (event.key !== "Tab" || mobileNavigationRef.current === null) {
        return;
      }
      const focusable = [
        ...mobileNavigationRef.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
        )
      ];
      if (focusable.length === 0) {
        event.preventDefault();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
      document.querySelector<HTMLButtonElement>('.utility-header__menu')?.focus({ preventScroll: true });
    };
  }, [mobileNavigationOpen]);

  const navigate = useCallback((nextPage: PageKey) => {
    setMobileNavigationOpen(false);
    setPage(nextPage);
  }, [setPage]);

  const refreshSnapshot = useCallback(() => {
    if (!connected || snapshotContext === null) {
      return;
    }
    void snapshotContext.refetchSnapshot();
  }, [connected, snapshotContext]);

  const sidebarProps = {
    page,
    accountLabel,
    accountDetail,
    assuranceLabel,
    environmentLabel,
    connected,
    refreshing: snapshotContext?.isFetching ?? false,
    onNavigate: navigate,
    onPreload: preloadPage,
    onRefresh: refreshSnapshot
  } as const;

  return (
    <div className="cockpit-shell">
      <a className="skip-link" href="#main-content">본문으로 건너뛰기</a>
      <AppSidebar {...sidebarProps} />

      {mobileNavigationOpen ? (
        <div className="mobile-navigation-layer" role="presentation">
          <button
            type="button"
            className="mobile-navigation-layer__backdrop"
            aria-label="메뉴 닫기"
            onClick={() => setMobileNavigationOpen(false)}
          />
          <div
            id="mobile-navigation"
            ref={mobileNavigationRef}
            className="mobile-navigation-layer__panel"
            role="dialog"
            aria-modal="true"
            aria-label="대시보드 메뉴"
          >
            <button
              type="button"
              className="mobile-navigation-layer__close"
              aria-label="메뉴 닫기"
              onClick={() => setMobileNavigationOpen(false)}
            >
              <X aria-hidden="true" />
            </button>
            <AppSidebar {...sidebarProps} mobile />
          </div>
        </div>
      ) : null}

      <div className="cockpit-workspace">
        <UtilityHeader
          ref={headingRef}
          title={getPageLabel(page)}
          description={getPageDescription(page)}
          connectionLabel={accountLabel}
          snapshotLabel={snapshotLabel}
          connected={connected}
          refreshing={snapshotContext?.isFetching ?? false}
          menuOpen={mobileNavigationOpen}
          onOpenMenu={() => setMobileNavigationOpen(true)}
          onRefresh={refreshSnapshot}
        />
        <OperationsStatusRail />

        <main id="main-content" className="cockpit-content" tabIndex={-1}>
          {children}
        </main>
      </div>
    </div>
  );
}
