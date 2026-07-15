import type { OperationsRole } from "./operationsContracts";

export const operationsRoleLabels: Record<OperationsRole, string> = {
  platform_admin: "플랫폼 관리자",
  operator: "운영 담당자",
  risk_approver: "위험 검토자",
  strategy_reviewer: "전략 검토자",
  auditor: "감사자",
  release_manager: "배포 관리자",
  viewer: "조회 사용자"
};

export function formatOperationsRoles(roles: readonly OperationsRole[]): string {
  return roles.map((role) => operationsRoleLabels[role]).join(" · ") || "역할 확인 불가";
}

const commandStates: Record<string, string> = {
  requested: "독립 검토 대기",
  approved: "Worker 적용 대기",
  claimed: "Worker 처리 중",
  applied: "적용 보고 · 최신 상태 확인 중",
  failed: "적용 실패",
  rejected: "거절됨",
  expired: "만료됨",
  canceled: "취소됨"
};

export function operationStateLabel(state: string): string {
  return commandStates[state] ?? "확인 불가";
}

const workerStates: Record<string, string> = {
  claimed: "작업 인수",
  applied: "적용 보고",
  failed: "적용 실패"
};

export function workerStateLabel(state: string | null | undefined): string {
  return state === null || state === undefined ? "없음" : workerStates[state] ?? "확인 불가";
}

export function humanizeOperationalEvidenceText(value: string): string {
  return value
    .replace(/\bWorker ACK\b/gi, "Worker 적용 확인")
    .replace(/\bACK\b/g, "적용 확인")
    .replace(/\bclaim\b/gi, "작업 인수")
    .replace(/\bpostcondition\b/gi, "최신 결과 확인")
    .replace(/\bRealtime\b/gi, "실시간 신호")
    .replace(/\bAAL2\b/gi, "2단계 인증")
    .replace(/\bG1\s*\/\s*G2\b/gi, "운영 준비 확인")
    .replace(/\bCAS\b/g, "최신 버전 확인")
    .replace(/\bRPC\b/g, "연결 요청");
}
