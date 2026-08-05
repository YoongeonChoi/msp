import {
  Activity,
  CheckCheck,
  KeyRound,
  LayoutDashboard,
  Scale,
  ScrollText,
  Settings,
  ShieldCheck,
  TriangleAlert,
  WalletCards
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

export type PageKey =
  | "control"
  | "approvals"
  | "portfolio"
  | "reconciliation"
  | "incidents"
  | "records"
  | "runtime"
  | "access"
  | "settings";

export type NavigationGroup = "operations" | "governance";

export interface NavItem {
  readonly key: PageKey;
  readonly label: string;
  readonly description: string;
  readonly icon: LucideIcon;
  readonly group: NavigationGroup;
  readonly requiresConnection: boolean;
}

export const navItems: readonly NavItem[] = [
  {
    key: "control",
    label: "운영 제어",
    description: "현재 운용 자세, 다음 행동과 진행 명령을 한 화면에서 확인합니다.",
    icon: LayoutDashboard,
    group: "operations",
    requiresConnection: true
  },
  {
    key: "approvals",
    label: "승인 검토",
    description: "요청자와 검토자를 분리해 대기 중인 운영 요청을 검토합니다.",
    icon: CheckCheck,
    group: "operations",
    requiresConnection: true
  },
  {
    key: "portfolio",
    label: "주문·보유",
    description: "모의·계약 테스트 주문과 검증된 보유 수량을 읽기 전용으로 확인합니다.",
    icon: WalletCards,
    group: "operations",
    requiresConnection: true
  },
  {
    key: "reconciliation",
    label: "수동 대사",
    description: "상태가 불명확한 주문의 증거, 검토와 회계 반영 단계를 확인합니다.",
    icon: Scale,
    group: "operations",
    requiresConnection: true
  },
  {
    key: "incidents",
    label: "사고 대응",
    description: "운영 사고의 확인 기한, 담당자와 해결 상태를 관리합니다.",
    icon: TriangleAlert,
    group: "operations",
    requiresConnection: true
  },
  {
    key: "records",
    label: "명령 기록",
    description: "명령, 검토와 감사 이벤트를 시간 순서로 추적합니다.",
    icon: ScrollText,
    group: "governance",
    requiresConnection: true
  },
  {
    key: "runtime",
    label: "런타임 상태",
    description: "Worker, 실시간 신호와 데이터 신선도 근거를 점검합니다.",
    icon: Activity,
    group: "governance",
    requiresConnection: true
  },
  {
    key: "access",
    label: "접근 권한",
    description: "역할 변경 요청과 독립 검토 기록을 관리합니다.",
    icon: KeyRound,
    group: "governance",
    requiresConnection: true
  },
  {
    key: "settings",
    label: "계정·보안",
    description: "기기 연결, 역할과 2단계 인증을 안전하게 관리합니다.",
    icon: Settings,
    group: "governance",
    requiresConnection: false
  }
];

export function parsePageKey(value: string): PageKey | null {
  const found = navItems.find((item) => item.key === value);
  return found?.key ?? null;
}

export function getPageLabel(page: PageKey): string {
  return navItems.find((item) => item.key === page)?.label ?? "운영 제어";
}

export function getPageDescription(page: PageKey): string {
  return navItems.find((item) => item.key === page)?.description ?? navItems[0].description;
}

export function pageRequiresConnection(page: PageKey): boolean {
  return navItems.find((item) => item.key === page)?.requiresConnection ?? true;
}

export const brandIcon = ShieldCheck;
