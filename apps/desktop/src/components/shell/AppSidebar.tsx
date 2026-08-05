import clsx from "clsx";
import { LockKeyhole, RefreshCcw, ShieldCheck } from "lucide-react";

import { navItems, type NavigationGroup, type PageKey } from "../../lib/navigation";

const groupLabels: Record<NavigationGroup, string> = {
  operations: "운영",
  governance: "관리"
};

export function AppSidebar({
  page,
  accountLabel,
  accountDetail,
  assuranceLabel,
  environmentLabel,
  connected,
  refreshing,
  mobile = false,
  onNavigate,
  onPreload,
  onRefresh
}: {
  readonly page: PageKey;
  readonly accountLabel: string;
  readonly accountDetail: string;
  readonly assuranceLabel: string;
  readonly environmentLabel: string;
  readonly connected: boolean;
  readonly refreshing: boolean;
  readonly mobile?: boolean;
  readonly onNavigate: (page: PageKey) => void;
  readonly onPreload?: (page: PageKey) => void;
  readonly onRefresh: () => void;
}) {
  return (
    <aside className={clsx("app-sidebar", mobile && "app-sidebar--mobile")} aria-label="대시보드 내비게이션">
      <div className="app-sidebar__brand" aria-label="KR Trading Control">
        <div className="app-sidebar__logo" aria-hidden="true">
          <ShieldCheck />
        </div>
        <div>
          <strong>KR Trading Lab</strong>
          <p>TRADING CONTROL</p>
        </div>
      </div>

      <nav className="app-sidebar__navigation" aria-label="주 탐색">
        {(["operations", "governance"] as const).map((group) => (
          <div className="app-sidebar__group" key={group}>
            <p className="app-sidebar__group-label">{groupLabels[group]}</p>
            {navItems.filter((item) => item.group === group).map((item) => {
              const Icon = item.icon;
              const active = item.key === page;
              const unavailable = item.requiresConnection && !connected;
              return (
                <button
                  key={item.key}
                  type="button"
                  aria-current={active ? "page" : undefined}
                  className={clsx("app-sidebar__nav-item", active && "is-active")}
                  title={unavailable ? `${item.description} 이 기기를 먼저 연결하세요.` : item.description}
                  disabled={unavailable}
                  onMouseEnter={() => onPreload?.(item.key)}
                  onFocus={() => onPreload?.(item.key)}
                  onClick={() => onNavigate(item.key)}
                >
                  <Icon aria-hidden="true" strokeWidth={1.65} />
                  <span>{item.label}</span>
                </button>
              );
            })}
          </div>
        ))}
      </nav>

      <div className="app-sidebar__footer">
        <div className="app-sidebar__safety" aria-label="안전 환경">
          <span>{environmentLabel}</span>
          <strong>
            <LockKeyhole aria-hidden="true" />
            LIVE 영구 금지
          </strong>
        </div>
        {connected ? (
          <button
            type="button"
            className="app-sidebar__refresh"
            disabled={refreshing}
            onClick={onRefresh}
          >
            <RefreshCcw className={refreshing ? "is-spinning" : undefined} aria-hidden="true" />
            {refreshing ? "새로고침 중" : "최신 상태 새로고침"}
          </button>
        ) : null}
        <div className="app-sidebar__account">
          <span>{accountLabel}</span>
          <small>{accountDetail}</small>
          <small>{assuranceLabel}</small>
        </div>
      </div>
    </aside>
  );
}
