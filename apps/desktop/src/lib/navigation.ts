import {
  Activity,
  Bell,
  ClipboardList,
  FlaskConical,
  Gauge,
  Newspaper,
  PieChart,
  Settings,
  ShieldCheck,
  SlidersHorizontal,
  TableProperties,
  Wallet
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

export type PageKey =
  | "dashboard"
  | "control"
  | "watchlist"
  | "portfolio"
  | "orders"
  | "signals"
  | "fundamentals"
  | "news"
  | "strategy"
  | "settings"
  | "logs";

export interface NavItem {
  readonly key: PageKey;
  readonly label: string;
  readonly icon: LucideIcon;
}

export const navItems: readonly NavItem[] = [
  { key: "dashboard", label: "대시보드", icon: Gauge },
  { key: "control", label: "제어", icon: SlidersHorizontal },
  { key: "watchlist", label: "관심종목", icon: TableProperties },
  { key: "portfolio", label: "포트폴리오", icon: PieChart },
  { key: "orders", label: "주문", icon: ClipboardList },
  { key: "signals", label: "시그널", icon: Activity },
  { key: "fundamentals", label: "펀더멘털", icon: Wallet },
  { key: "news", label: "뉴스", icon: Newspaper },
  { key: "strategy", label: "전략 Lab", icon: FlaskConical },
  { key: "settings", label: "설정", icon: Settings },
  { key: "logs", label: "로그", icon: Bell }
];

export function parsePageKey(value: string): PageKey | null {
  const found = navItems.find((item) => item.key === value);
  return found?.key ?? null;
}

export function getPageLabel(page: PageKey): string {
  return navItems.find((item) => item.key === page)?.label ?? "대시보드";
}

export const brandIcon = ShieldCheck;
