import {
  Settings,
  ShieldCheck,
  SlidersHorizontal
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

export type PageKey = "control" | "settings";

export interface NavItem {
  readonly key: PageKey;
  readonly label: string;
  readonly icon: LucideIcon;
}

export const navItems: readonly NavItem[] = [
  { key: "control", label: "운영 제어", icon: SlidersHorizontal },
  { key: "settings", label: "접근 및 로그인", icon: Settings }
];

export function parsePageKey(value: string): PageKey | null {
  const found = navItems.find((item) => item.key === value);
  return found?.key ?? null;
}

export function getPageLabel(page: PageKey): string {
  return navItems.find((item) => item.key === page)?.label ?? "운영 제어";
}

export const brandIcon = ShieldCheck;
