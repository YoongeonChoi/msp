import { forwardRef } from "react";
import { Menu, RefreshCcw } from "lucide-react";

export const UtilityHeader = forwardRef<
  HTMLHeadingElement,
  {
    readonly title: string;
    readonly description: string;
    readonly connectionLabel: string;
    readonly snapshotLabel: string;
    readonly connected: boolean;
    readonly refreshing: boolean;
    readonly menuOpen: boolean;
    readonly onOpenMenu: () => void;
    readonly onRefresh: () => void;
  }
>(function UtilityHeader(
  {
    title,
    description,
    connectionLabel,
    snapshotLabel,
    connected,
    refreshing,
    menuOpen,
    onOpenMenu,
    onRefresh
  },
  ref
) {
  return (
    <header className="utility-header" aria-label="앱 헤더">
      <button
        type="button"
        className="utility-header__menu"
        aria-label="메뉴 열기"
        aria-controls="mobile-navigation"
        aria-expanded={menuOpen}
        onClick={onOpenMenu}
      >
        <Menu aria-hidden="true" />
      </button>

      <div className="utility-header__title">
        <h1 ref={ref} tabIndex={-1}>{title}</h1>
        <p>{description}</p>
      </div>

      <div className="utility-header__status">
        <div>
          <span>{connectionLabel}</span>
          <small>{snapshotLabel}</small>
        </div>
        <button
          type="button"
          aria-label="최신 운영 상태 새로고침"
          title="최신 운영 상태 새로고침"
          disabled={!connected || refreshing}
          onClick={onRefresh}
        >
          <RefreshCcw className={refreshing ? "is-spinning" : undefined} aria-hidden="true" />
        </button>
      </div>
    </header>
  );
});
