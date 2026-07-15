import type { ClientRealtimeHealth } from "./controlPlaneRealtime";
import type { OperationsSnapshot, RuntimeHealth } from "./operationsContracts";
import { formatKst } from "./formatters";

export type SafetyRailTone = "neutral" | "safe" | "warning" | "danger" | "info";

export type SafetyRailItemKey =
  | "live"
  | "execution"
  | "overall"
  | "worker"
  | "snapshot"
  | "command";

export interface SafetyRailItemModel {
  readonly key: SafetyRailItemKey;
  readonly label: string;
  readonly value: string;
  readonly detail: string;
  readonly tone: SafetyRailTone;
}

export interface SafetyRailModel {
  readonly items: readonly SafetyRailItemModel[];
  readonly criticalMessages: readonly string[];
}

/**
 * Converts the strict control-plane snapshot into user-facing status text.
 * Missing, stale, or disconnected evidence is never promoted to a healthy
 * state. The command cell only describes trading-command eligibility; it does
 * not imply that Worker-independent incident or access work is unavailable.
 */
export function buildSafetyRailModel(
  snapshot: OperationsSnapshot | null,
  isOnline: boolean,
  realtime: ClientRealtimeHealth | null = null,
  now = new Date()
): SafetyRailModel {
  if (snapshot === null) {
    return {
      items: [
        item("live", "LIVE 잠금", "잠금 유지", "환경 확인 불가", "safe"),
        item("execution", "주문 생성", "확인 불가", "운영 상태 대기", "neutral"),
        item("overall", "전체 운영", isOnline ? "확인 불가" : "기기 오프라인", "최신 상태 없음", isOnline ? "warning" : "danger"),
        item("worker", "Worker", "확인 불가", "상태 신호 · 배포 버전 확인 불가", "warning"),
        item("snapshot", "데이터 기준", "확인 불가", "실시간 신호 확인 불가", "warning"),
        item("command", "거래 명령", "차단", isOnline ? "최신 상태 확인 필요" : "기기 오프라인", "warning")
      ],
      criticalMessages: isOnline
        ? []
        : ["기기가 오프라인입니다. 모든 권한 부여와 전송을 차단합니다."]
    };
  }

  const health = snapshot.runtime_health;
  const worker = health.components.find((component) => component.component === "worker") ?? null;
  const realtimeState = resolveRealtimeState(health, realtime, now);
  const snapshotCurrent =
    isWithinAge(snapshot.generated_at, health.freshness_policy.snapshot_max_age_seconds, now) &&
    isWithinAge(health.as_of, health.freshness_policy.snapshot_max_age_seconds, now);
  const workerCurrent =
    worker?.state === "fresh" &&
    health.worker_heartbeat_at !== null &&
    isWithinAge(
      health.worker_heartbeat_at,
      health.freshness_policy.worker_heartbeat_max_age_seconds,
      now
    );
  const commandGate = evaluateTradingCommandGate(snapshot, isOnline, realtime, now);
  const qualification = qualificationSummary(snapshot, now);
  const criticalMessages = criticalAlerts(snapshot, isOnline, now);

  return {
    items: [
      item(
        "live",
        "LIVE 잠금",
        health.live_permitted === false ? "잠금 유지" : "확인 불가",
        environmentLabel(health.environment),
        health.live_permitted === false ? "safe" : "danger"
      ),
      item(
        "execution",
        "주문 생성",
        health.execution_enabled ? "주문 생성 허용" : "주문 생성 중지",
        health.execution_enabled ? "현재 환경에서 생성 가능" : "전체 서비스 중단을 의미하지 않음",
        health.execution_enabled ? "info" : "neutral"
      ),
      item(
        "overall",
        "전체 운영",
        !isOnline
          ? "기기 오프라인"
          : health.overall_state === "fresh" && !snapshotCurrent
            ? "데이터 지연"
            : runtimeStateLabel(health.overall_state),
        isOnline ? `상태 버전 ${health.state_version}` : "서버 상태는 재연결 후 다시 확인",
        !isOnline
          ? "danger"
          : health.overall_state === "fresh" && !snapshotCurrent
            ? "warning"
            : healthTone(health.overall_state)
      ),
      item(
        "worker",
        "Worker",
        worker === null || worker.state !== "fresh"
          ? worker === null
            ? "확인 불가"
            : runtimeStateLabel(worker.state)
          : workerCurrent
            ? formatRelativeAge(health.worker_heartbeat_at, now)
            : `지연 · ${formatRelativeAge(health.worker_heartbeat_at, now)}`,
        `배포 버전 ${shortRelease(health.worker_release_sha)}`,
        worker === null || !workerCurrent ? "warning" : healthTone(worker.state)
      ),
      item(
        "snapshot",
        "데이터 기준",
        formatKst(health.as_of) === "-"
          ? "확인 불가"
          : snapshotCurrent
            ? formatKst(health.as_of)
            : `지연 · ${formatKst(health.as_of)}`,
        realtimeState.detail,
        snapshotCurrent ? realtimeState.tone : "warning"
      ),
      item(
        "command",
        "거래 명령",
        commandGate.allowed ? "가능" : "차단",
        commandGate.allowed ? qualification : `${commandGate.reason} · ${qualification}`,
        commandGate.allowed ? "safe" : "warning"
      )
    ],
    criticalMessages
  };
}

function item(
  key: SafetyRailItemKey,
  label: string,
  value: string,
  detail: string,
  tone: SafetyRailTone
): SafetyRailItemModel {
  return { key, label, value, detail, tone };
}

function evaluateTradingCommandGate(
  snapshot: OperationsSnapshot,
  isOnline: boolean,
  realtime: ClientRealtimeHealth | null,
  now: Date
): { readonly allowed: boolean; readonly reason: string } {
  const health = snapshot.runtime_health;
  const access = snapshot.access;
  if (!isOnline) {
    return { allowed: false, reason: "기기 오프라인" };
  }
  if (!access.signed_in) {
    return { allowed: false, reason: "로그인 필요" };
  }
  if (access.session_state !== "active" || health.overall_state === "session_expired") {
    return { allowed: false, reason: "세션 만료" };
  }
  if (access.assurance_level !== "aal2") {
    return { allowed: false, reason: "2단계 인증 필요" };
  }
  if (access.actor === null) {
    return { allowed: false, reason: "사용자 역할 확인 불가" };
  }
  if (
    !access.permissions.includes("request_command") &&
    !access.actor.roles.includes("operator")
  ) {
    return { allowed: false, reason: "명령 요청 권한 없음" };
  }
  if (health.overall_state !== "fresh") {
    return { allowed: false, reason: runtimeStateLabel(health.overall_state) };
  }
  if (
    !isWithinAge(snapshot.generated_at, health.freshness_policy.snapshot_max_age_seconds, now) ||
    !isWithinAge(health.as_of, health.freshness_policy.snapshot_max_age_seconds, now)
  ) {
    return { allowed: false, reason: "최신 운영 상태 확인 필요" };
  }

  if (health.live_permitted !== false) {
    return { allowed: false, reason: "LIVE 잠금 확인 필요" };
  }
  if (
    health.worker_heartbeat_at === null ||
    !isWithinAge(health.worker_heartbeat_at, health.freshness_policy.worker_heartbeat_max_age_seconds, now)
  ) {
    return { allowed: false, reason: "Worker 상태 신호 확인 필요" };
  }

  const componentNames = new Set(health.components.map((component) => component.component));
  if (!componentNames.has("control_plane") || !componentNames.has("worker")) {
    return { allowed: false, reason: "필수 구성요소 확인 필요" };
  }
  for (const component of health.components) {
    const maxAgeSeconds = component.component === "worker"
      ? health.freshness_policy.worker_heartbeat_max_age_seconds
      : component.component === "realtime"
        ? health.freshness_policy.realtime_max_age_seconds
        : health.freshness_policy.snapshot_max_age_seconds;
    if (
      component.state !== "fresh" ||
      !isWithinAge(component.observed_at, maxAgeSeconds, now)
    ) {
      return {
        allowed: false,
        reason: component.component === "worker"
          ? "Worker 상태 확인 필요"
          : component.component === "realtime"
            ? "실시간 상태 확인 필요"
            : "구성요소 상태 확인 필요"
      };
    }
  }

  const realtimeState = resolveRealtimeState(health, realtime, now);
  if (!realtimeState.fresh) {
    return { allowed: false, reason: "실시간 신호 확인 필요" };
  }
  return { allowed: true, reason: "" };
}

function resolveRealtimeState(
  health: RuntimeHealth,
  realtime: ClientRealtimeHealth | null,
  now: Date
): { readonly fresh: boolean; readonly detail: string; readonly tone: SafetyRailTone } {
  const connected = realtime === null
    ? health.realtime_connected
    : realtime.connected && realtime.connectedAt !== null;
  const lastSignalAt = realtime === null ? health.realtime_last_seen_at : realtime.lastSignalAt;
  const fresh = connected && lastSignalAt !== null &&
    isWithinAge(lastSignalAt, health.freshness_policy.realtime_max_age_seconds, now);

  if (!connected) {
    return { fresh: false, detail: "실시간 연결 끊김", tone: "warning" };
  }
  if (lastSignalAt === null) {
    return { fresh: false, detail: "실시간 연결 · 마지막 신호 확인 불가", tone: "warning" };
  }
  if (!fresh) {
    return { fresh: false, detail: `실시간 신호 지연 · ${formatKst(lastSignalAt)}`, tone: "warning" };
  }
  return { fresh: true, detail: `실시간 신호 ${formatKst(lastSignalAt)}`, tone: "safe" };
}

function qualificationSummary(snapshot: OperationsSnapshot, now: Date): string {
  const qualification = snapshot.qualification;
  if (qualification === null) {
    return "운영 준비 확인 없음 · 회계 기준점 확인 불가";
  }
  const validFrom = Date.parse(qualification.valid_from);
  const validUntil = Date.parse(qualification.valid_until);
  const timeValid = Number.isFinite(validFrom) && Number.isFinite(validUntil) &&
    validFrom <= now.getTime() && validUntil > now.getTime();
  const ready = qualification.status === "qualified" &&
    qualification.environment === snapshot.runtime_health.environment && timeValid;
  return `${ready ? "운영 준비 확인 유효" : "운영 준비 확인 차단"} ${formatKst(qualification.valid_until)} · 회계 기준점 ${qualification.ledger_checkpoint}`;
}

function criticalAlerts(
  snapshot: OperationsSnapshot,
  isOnline: boolean,
  now: Date
): readonly string[] {
  const messages: string[] = [];
  if (!isOnline) {
    messages.push("기기가 오프라인입니다. 모든 권한 부여와 전송을 차단합니다.");
  }
  if (
    snapshot.access.session_state === "expired" ||
    snapshot.runtime_health.overall_state === "session_expired"
  ) {
    messages.push("세션이 만료되었습니다. 다시 로그인해야 합니다.");
  }
  if (
    snapshot.runtime_health.overall_state === "contract_error" ||
    snapshot.runtime_health.components.some((component) => component.state === "contract_error")
  ) {
    messages.push("데이터 계약 오류가 감지되었습니다. 계약을 다시 확인하기 전까지 관련 작업을 차단합니다.");
  }

  const seriousIncidents = snapshot.incidents.filter(
    (incident) => incident.status !== "resolved" && (incident.severity === "sev1" || incident.severity === "sev2")
  );
  if (seriousIncidents.length > 0) {
    messages.push(`중대 사고 ${seriousIncidents.length}건이 해결되지 않았습니다.`);
  }

  const overdueIncidentConfirmations = snapshot.incidents.filter((incident) => {
    const dueAt = incident.ack_due_at === null ? Number.NaN : Date.parse(incident.ack_due_at);
    return incident.status === "open" && Number.isFinite(dueAt) && dueAt <= now.getTime();
  });
  if (overdueIncidentConfirmations.length > 0) {
    messages.push(`사고 확인 기한을 넘긴 항목이 ${overdueIncidentConfirmations.length}건 있습니다.`);
  }
  return messages;
}

function environmentLabel(environment: RuntimeHealth["environment"]): string {
  return environment === "paper" ? "모의거래" : "계약 테스트";
}

function runtimeStateLabel(state: RuntimeHealth["overall_state"]): string {
  const labels: Record<RuntimeHealth["overall_state"], string> = {
    fresh: "정상",
    degraded: "성능 저하",
    stale: "데이터 지연",
    offline: "서비스 오프라인",
    session_expired: "세션 만료",
    contract_error: "데이터 계약 오류"
  };
  return labels[state];
}

function healthTone(state: RuntimeHealth["overall_state"]): SafetyRailTone {
  if (state === "fresh") {
    return "safe";
  }
  if (state === "degraded" || state === "stale") {
    return "warning";
  }
  return "danger";
}

function shortRelease(release: string | null): string {
  return release === null ? "확인 불가" : release.slice(0, 12);
}

function formatRelativeAge(timestamp: string | null, now: Date): string {
  if (timestamp === null) {
    return "상태 신호 확인 불가";
  }
  const timestampMs = Date.parse(timestamp);
  const ageMs = now.getTime() - timestampMs;
  if (!Number.isFinite(ageMs) || ageMs < -30_000) {
    return "상태 신호 시각 확인 불가";
  }
  const seconds = Math.max(0, Math.floor(ageMs / 1_000));
  if (seconds < 60) {
    return `상태 신호 ${seconds}초 전`;
  }
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return `상태 신호 ${minutes}분 전`;
  }
  return `상태 신호 ${Math.floor(minutes / 60)}시간 전`;
}

function isWithinAge(timestamp: string, maxAgeSeconds: number, now: Date): boolean {
  const ageMs = now.getTime() - Date.parse(timestamp);
  return Number.isFinite(ageMs) && ageMs >= -30_000 && ageMs <= maxAgeSeconds * 1_000;
}
