import { Suspense, lazy, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  ChevronRight,
  CircleAlert,
  ClipboardCheck,
  FileSearch,
  KeyRound,
  ListFilter,
  ShieldAlert,
  Timer,
  WifiOff,
  type LucideIcon
} from "lucide-react";
import type {
  Incident,
  OperationCommandReceipt,
  OperationsSnapshot,
  UnknownResolutionContextV2,
  UnknownResolutionSnapshotV2
} from "../../lib/operationsContracts";
import type { DrawerState } from "../../lib/uiState";
import { EmptyState, LoadingState, Panel, Pill, SectionTitle, pageButtonClass } from "../ui";
import type { Tone } from "../ui";
import { commandLabel, isCommandPostconditionVerified } from "./SafetyCommandCenter";
import { humanizeOperationalEvidenceText } from "../../lib/presentation";

const DrawerSurface = lazy(async () => ({
  default: (await import("../DialogSurface")).DrawerSurface
}));

const APPROVAL_EXPIRY_WARNING_MS = 30 * 60_000;

type AttentionLevel = "critical" | "high" | "medium" | "info";

interface AttentionAction {
  readonly label: string;
  readonly drawer: DrawerState;
  readonly disabledReason?: string;
}

export interface AttentionItemModel {
  readonly id: string;
  readonly priority: 1 | 2 | 3 | 4 | 5;
  readonly sortAt: number;
  readonly level: AttentionLevel;
  readonly severityLabel: string;
  readonly title: string;
  readonly cause: string;
  readonly timing: string;
  readonly action?: AttentionAction;
  readonly readOnlyReason?: string;
}

export interface AttentionQueueProps {
  readonly snapshot: OperationsSnapshot;
  readonly unknownSnapshot: UnknownResolutionSnapshotV2 | null;
  readonly isOnline: boolean;
  readonly now: Date;
  readonly onOpen: (drawer: DrawerState) => void;
}

const levelPresentation: Record<
  AttentionLevel,
  { readonly icon: LucideIcon; readonly iconClass: string; readonly tone: Tone }
> = {
  critical: { icon: ShieldAlert, iconClass: "bg-dangerSoft text-danger", tone: "danger" },
  high: { icon: AlertTriangle, iconClass: "bg-warningSoft text-warning", tone: "warning" },
  medium: { icon: Timer, iconClass: "bg-primarySoft text-primary", tone: "info" },
  info: { icon: CircleAlert, iconClass: "bg-canvas text-mutedStrong", tone: "neutral" }
};

/**
 * Builds a deterministic, safety-first queue without inferring healthy state
 * from missing evidence. The numeric priority mirrors the approved operations
 * information architecture and is intentionally independent from color.
 */
export function buildAttentionItems({
  snapshot,
  unknownSnapshot,
  isOnline,
  now
}: Omit<AttentionQueueProps, "onOpen">): readonly AttentionItemModel[] {
  const items: AttentionItemModel[] = [];
  const access = snapshot.access;
  const health = snapshot.runtime_health;

  if (!isOnline) {
    items.push({
      id: "device-offline",
      priority: 1,
      sortAt: 0,
      level: "critical",
      severityLabel: "전송 차단",
      title: "기기가 오프라인입니다",
      cause: "모든 권한 부여와 변경 전송이 차단되며 재연결 후에도 자동 재시도하지 않습니다.",
      timing: "연결 복구 후 최신 상태 재확인",
      readOnlyReason: "다음 행동 · 네트워크 연결을 복구하세요."
    });
  }

  const sessionExpired =
    access.session_state === "expired" || health.overall_state === "session_expired";
  if (sessionExpired) {
    items.push({
      id: "session-expired",
      priority: 1,
      sortAt: 1,
      level: "critical",
      severityLabel: access.signed_in ? "세션 만료" : "기기 연결 필요",
      title: access.signed_in ? "운영 세션이 만료되었습니다" : "이 기기를 운영 계정에 연결해야 합니다",
      cause: "인증 상태와 휘발성 권한 정보를 다시 확인하기 전에는 변경 작업을 전송할 수 없습니다.",
      timing: "즉시 확인",
      readOnlyReason: "다음 행동 · 계정·보안에서 이 기기를 다시 연결하세요."
    });
  } else if (access.assurance_level !== "aal2") {
    items.push({
      id: "aal2-required",
      priority: 5,
      sortAt: 0,
      level: "medium",
      severityLabel: "인증 필요",
      title: "2단계 인증이 필요합니다",
      cause: "운영 변경 전에 현재 세션의 2단계 인증 상태를 확인해야 합니다.",
      timing: "변경 작업 전 완료",
      action: {
        label: "2단계 인증 관리",
        drawer: { kind: "mfa" }
      }
    });
  }

  const contractIncident = snapshot.incidents.find(
    (incident) => incident.kind === "contract_error" && incident.status !== "resolved"
  );
  const hasContractError =
    health.overall_state === "contract_error" ||
    health.components.some((component) => component.state === "contract_error");
  const consumedIncidentIds = new Set<string>();

  if (hasContractError) {
    if (contractIncident !== undefined) {
      items.push(buildIncidentItem(snapshot, contractIncident, isOnline, now, 1));
      consumedIncidentIds.add(contractIncident.incident_id);
    } else {
      items.push({
        id: "contract-error",
        priority: 1,
        sortAt: 2,
        level: "critical",
        severityLabel: "계약 오류",
        title: "데이터 계약을 확인할 수 없습니다",
        cause: "계약 검증이 정상화되기 전까지 관련 변경 작업을 차단합니다.",
        timing: `상태 기준 ${relativeAge(health.as_of, now)}`,
        readOnlyReason: "다음 행동 · 연결 상세와 계약 검증 기록을 확인하세요."
      });
    }
  }

  const workerUnavailable =
    health.components.some(
      (component) => component.component === "worker" && component.state === "offline"
    ) ||
    (health.overall_state === "offline" &&
      !snapshot.incidents.some(
        (incident) => incident.kind === "worker_offline" && incident.status !== "resolved"
      ));
  if (workerUnavailable) {
    items.push({
      id: "worker-offline",
      priority: 2,
      sortAt: 0,
      level: "high",
      severityLabel: "Worker 오프라인",
      title: "거래 명령을 처리할 Worker가 오프라인입니다",
      cause: "거래 명령은 차단되지만 조건을 충족한 사고 확인 작업은 계속할 수 있습니다.",
      timing:
        health.worker_heartbeat_at === null
          ? "상태 신호 확인 불가"
          : `마지막 상태 신호 ${relativeAge(health.worker_heartbeat_at, now)}`,
      readOnlyReason: "다음 행동 · Worker 상태와 열린 사고를 확인하세요."
    });
  }

  for (const incident of snapshot.incidents) {
    if (incident.status === "resolved" || consumedIncidentIds.has(incident.incident_id)) {
      continue;
    }
    items.push(buildIncidentItem(snapshot, incident, isOnline, now));
  }

  const pendingReviewIds = new Set(snapshot.pending_reviews.map((command) => command.command_id));
  for (const command of snapshot.pending_reviews) {
    items.push(buildApprovalItem(snapshot, command, isOnline, now));
  }

  const canViewUnknown = canViewUnknownResolution(snapshot);
  for (const context of unknownSnapshot?.cases ?? []) {
    if (context.postcondition.resolution_complete) {
      continue;
    }
    items.push(buildUnknownItem(context, canViewUnknown, now));
  }

  for (const reconciliation of snapshot.reconciliation_cases) {
    if (reconciliation.status === "resolved" || reconciliation.status === "rejected") {
      continue;
    }
    const canView = access.permissions.includes("view_reconciliation");
    items.push({
      id: `reconciliation-${reconciliation.case_id}`,
      priority: 4,
      sortAt: safeTimestamp(reconciliation.opened_at),
      level: "medium",
      severityLabel: "수동 대사",
      title: "주문 대사 확인이 필요합니다",
      cause: reconciliationReasonLabel(reconciliation.reason_code),
      timing: `열림 ${relativeAge(reconciliation.opened_at, now)}`,
      action: canView
        ? {
            label: "대사 상세",
            drawer: { kind: "reconciliation", entityId: reconciliation.case_id }
          }
        : undefined,
      readOnlyReason: canView
        ? undefined
        : "현재 역할에는 대사 기록을 열람할 권한이 없습니다."
    });
  }

  const qualificationItem = buildQualificationItem(snapshot, isOnline, now);
  if (qualificationItem !== null) {
    items.push(qualificationItem);
  }

  for (const command of snapshot.commands) {
    if (command.state === "requested" && pendingReviewIds.has(command.command_id)) {
      continue;
    }
    const commandItem = buildCommandItem(snapshot, command, now);
    if (commandItem !== null) {
      items.push(commandItem);
    }
  }

  return items.sort((left, right) => {
    const priorityDifference = left.priority - right.priority;
    if (priorityDifference !== 0) {
      return priorityDifference;
    }
    const timeDifference = left.sortAt - right.sortAt;
    if (timeDifference !== 0) {
      return timeDifference;
    }
    return left.id.localeCompare(right.id);
  });
}

export function AttentionQueue(props: AttentionQueueProps) {
  const { snapshot, unknownSnapshot, isOnline, now, onOpen } = props;
  const [allOpen, setAllOpen] = useState(false);
  const items = useMemo(
    () => buildAttentionItems({ snapshot, unknownSnapshot, isOnline, now }),
    [snapshot, unknownSnapshot, isOnline, now]
  );
  const visibleItems = items.slice(0, 5);

  const openItem = (item: AttentionItemModel) => {
    if (item.action === undefined || item.action.disabledReason !== undefined) {
      return;
    }
    setAllOpen(false);
    onOpen({ kind: item.action.drawer.kind, entityId: item.action.drawer.entityId });
  };

  return (
    <Panel className="min-w-0" >
      <SectionTitle
        title="지금 확인할 항목"
        detail={<Pill tone={items.length > 0 ? "warning" : "safe"}>{items.length}건</Pill>}
      />
      <p className="mb-2 text-sm text-mutedStrong">
        현재 상태에서 사람이 확인해야 하는 항목을 위험도와 마감 순서로 정렬했습니다.
      </p>
      {visibleItems.length === 0 ? (
        <EmptyState
          title="지금 확인할 항목이 없습니다"
          detail="새로운 사고, 승인, 대사 또는 적용 확인이 생기면 이곳에 표시됩니다."
        />
      ) : (
        <AttentionList items={visibleItems} onAction={openItem} />
      )}
      {items.length > 0 ? (
        <div className="mt-4 flex justify-end border-t border-lineSubtle pt-4">
          <button
            type="button"
            className={pageButtonClass("neutral")}
            onClick={() => setAllOpen(true)}
          >
            <ListFilter size={17} aria-hidden="true" />
            전체 보기
            <span className="sr-only">, 총 {items.length}건</span>
          </button>
        </div>
      ) : null}

      {allOpen ? (
        <Suspense fallback={<LoadingState label="전체 확인 항목을 불러오는 중" />}>
          <DrawerSurface
            open
            readOnly
            title="지금 확인할 항목 전체"
            description="심각도, 원인, 경과 시간과 다음 행동을 한 단계에서 확인합니다."
            onRequestClose={() => setAllOpen(false)}
          >
            <AttentionList items={items} onAction={openItem} compact />
          </DrawerSurface>
        </Suspense>
      ) : null}
    </Panel>
  );
}

function AttentionList({
  items,
  onAction,
  compact = false
}: {
  readonly items: readonly AttentionItemModel[];
  readonly onAction: (item: AttentionItemModel) => void;
  readonly compact?: boolean;
}) {
  return (
    <ul className="divide-y divide-lineSubtle" aria-label="운영 확인 대기열">
      {items.map((item) => (
        <AttentionRow key={item.id} item={item} compact={compact} onAction={() => onAction(item)} />
      ))}
    </ul>
  );
}

function AttentionRow({
  item,
  compact,
  onAction
}: {
  readonly item: AttentionItemModel;
  readonly compact: boolean;
  readonly onAction: () => void;
}) {
  const presentation = levelPresentation[item.level];
  const Icon = attentionIcon(item, presentation.icon);
  const reasonId = `attention-reason-${item.id.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
  const disabledReason = item.action?.disabledReason;

  return (
    <li className={compact ? "py-4 first:pt-0" : "py-4 first:pt-2"}>
      <article className="grid min-w-0 grid-cols-[44px_minmax(0,1fr)] gap-3 lg:grid-cols-[44px_minmax(0,1fr)_auto] lg:items-center">
        <span
          className={`flex size-11 shrink-0 items-center justify-center rounded-md ${presentation.iconClass}`}
          aria-hidden="true"
        >
          <Icon size={20} />
        </span>
        <div className="min-w-0">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <Pill tone={presentation.tone}>{item.severityLabel}</Pill>
            <h3 className="min-w-0 font-semibold text-ink">{item.title}</h3>
          </div>
          <p className="mt-1 text-sm leading-5 text-mutedStrong">
            {item.cause}
          </p>
          <p className="mt-1 text-xs font-medium text-muted">{item.timing}</p>
        </div>
        <div className="col-start-2 min-w-0 lg:col-start-auto lg:justify-self-end">
          {item.action !== undefined ? (
            <>
              {disabledReason !== undefined ? (
                <p id={reasonId} className="mb-2 max-w-72 text-xs text-warning lg:text-right">
                  {disabledReason}
                </p>
              ) : null}
              <button
                type="button"
                className={`${pageButtonClass("neutral")} w-full justify-center lg:w-auto lg:min-w-32 lg:whitespace-nowrap`}
                disabled={disabledReason !== undefined}
                aria-label={`${item.title} · ${item.action.label}`}
                aria-describedby={disabledReason !== undefined ? reasonId : undefined}
                onClick={onAction}
              >
                {item.action.label}
                <ChevronRight size={16} aria-hidden="true" />
              </button>
            </>
          ) : (
            <p className="max-w-80 text-sm text-mutedStrong lg:text-right">
              {item.readOnlyReason ?? "현재 역할에서는 읽기 전용으로 표시됩니다."}
            </p>
          )}
        </div>
      </article>
    </li>
  );
}

function buildIncidentItem(
  snapshot: OperationsSnapshot,
  incident: Incident,
  isOnline: boolean,
  now: Date,
  priorityOverride?: 1
): AttentionItemModel {
  const dueAt = incident.ack_due_at === null ? Number.NaN : Date.parse(incident.ack_due_at);
  const overdue = incident.status === "open" && Number.isFinite(dueAt) && dueAt <= now.getTime();
  const serious = incident.severity === "sev1" || incident.severity === "sev2";
  const priority = priorityOverride ?? (serious || overdue ? 2 : 5);
  const requiredPermission = incident.status === "open" ? "acknowledge_incident" : "resolve_incident";
  const canAct = snapshot.access.permissions.includes(requiredPermission);
  const temporaryReason = incident.status === "mitigating"
    ? undefined
    : incidentMutationBlockedReason(snapshot, isOnline, now);
  const label = incident.status === "open"
    ? "사고 확인"
    : incident.status === "acknowledged"
      ? "해결 검토"
      : "완화 상태 확인";

  return {
    id: `incident-${incident.incident_id}`,
    priority,
    sortAt: Number.isFinite(dueAt) ? dueAt : safeTimestamp(incident.detected_at),
    level: priorityOverride !== undefined || incident.severity === "sev1"
      ? "critical"
      : serious || overdue
        ? "high"
        : "medium",
    severityLabel: priorityOverride !== undefined
      ? "계약 오류"
      : overdue
        ? "확인 기한 초과"
        : incident.severity.toUpperCase(),
    title: humanizeOperationalEvidenceText(incident.title),
    cause: humanizeOperationalEvidenceText(incident.summary),
    timing: incident.ack_due_at === null
      ? `감지 ${relativeAge(incident.detected_at, now)}`
      : `사고 확인 ${dueLabel(incident.ack_due_at, now)}`,
    action: canAct
      ? {
          label,
          drawer: { kind: "incident", entityId: incident.incident_id },
          disabledReason: temporaryReason
        }
      : incident.status === "mitigating"
        ? {
            label,
            drawer: { kind: "incident", entityId: incident.incident_id }
          }
        : undefined,
    readOnlyReason: canAct
      ? undefined
      : incident.status === "open"
        ? "현재 역할에는 사고 확인 권한이 없어 읽기 전용으로 표시됩니다."
        : "현재 역할에는 사고 해결 권한이 없어 읽기 전용으로 표시됩니다."
  };
}

function buildApprovalItem(
  snapshot: OperationsSnapshot,
  command: OperationCommandReceipt,
  isOnline: boolean,
  now: Date
): AttentionItemModel {
  const expiresAt = Date.parse(command.expires_at);
  const remaining = expiresAt - now.getTime();
  const urgent = !Number.isFinite(expiresAt) || remaining <= APPROVAL_EXPIRY_WARNING_MS;
  const permanentReason = approvalPermissionReason(snapshot, command);
  const temporaryReason = remaining <= 0
    ? "승인 기한이 지나 최신 상태에서 새 요청을 확인해야 합니다."
    : commandMutationBlockedReason(snapshot, isOnline, now);

  return {
    id: `approval-${command.command_id}`,
    priority: urgent ? 3 : 5,
    sortAt: Number.isFinite(expiresAt) ? expiresAt : Number.MAX_SAFE_INTEGER,
    level: remaining <= 0 ? "critical" : urgent ? "high" : "medium",
    severityLabel: remaining <= 0 ? "승인 기한 초과" : urgent ? "승인 임박" : "승인 대기",
    title: `${commandLabel(command.command_type)} 독립 검토`,
    cause: `${command.requested_by.display_name} 요청 · 요청자와 검토자를 분리해야 합니다.`,
    timing: dueLabel(command.expires_at, now),
    action: permanentReason === null
      ? {
          label: "승인 검토",
          drawer: { kind: "approval", entityId: command.command_id },
          disabledReason: temporaryReason
        }
      : undefined,
    readOnlyReason: permanentReason ?? undefined
  };
}

function buildUnknownItem(
  context: UnknownResolutionContextV2,
  canView: boolean,
  now: Date
): AttentionItemModel {
  const state = context.request?.state ?? null;
  const awaitingAccounting = context.application_receipt !== null &&
    !context.postcondition.resolution_complete;
  const cause = awaitingAccounting
    ? "적용 기록은 있지만 최신 회계 집계와 완료 조건이 아직 확인되지 않았습니다."
    : state === "requested"
      ? "불변 증거와 최신 버전을 다른 위험 검토자가 확인해야 합니다."
      : state === "approved" || state === "claimed"
        ? "승인 이후 적용 결과와 최신 회계 상태를 확인해야 합니다."
        : "미확정 체결 관찰의 원시 증거와 최신 버전을 확인해야 합니다.";
  const label = awaitingAccounting
    ? "회계 반영 확인"
    : state === "requested"
      ? "독립 검토"
      : "대사 상세";

  return {
    id: `unknown-${context.break_id}`,
    priority: 4,
    sortAt: safeTimestamp(context.detected_at),
    level: awaitingAccounting ? "high" : "medium",
    severityLabel: awaitingAccounting ? "완료 확인 대기" : "미확정 대사",
    title: `${context.symbol} ${context.side === "buy" ? "매수" : "매도"} 상태 확인`,
    cause,
    timing: `감지 ${relativeAge(context.detected_at, now)} · 최신 버전 ${context.break_revision}`,
    action: canView
      ? {
          label,
          drawer: { kind: "reconciliation", entityId: context.break_id }
        }
      : undefined,
    readOnlyReason: canView
      ? undefined
      : "현재 역할에는 미확정 대사 증거를 열람할 권한이 없습니다."
  };
}

function buildQualificationItem(
  snapshot: OperationsSnapshot,
  isOnline: boolean,
  now: Date
): AttentionItemModel | null {
  const qualification = snapshot.qualification;
  const expiresAt = qualification === null ? Number.NaN : Date.parse(qualification.valid_until);
  const ready = qualification !== null &&
    qualification.status === "qualified" &&
    qualification.environment === snapshot.runtime_health.environment &&
    qualification.g1.status === "pass" &&
    qualification.g2.status === "pass" &&
    Date.parse(qualification.valid_from) <= now.getTime() &&
    expiresAt > now.getTime();
  const expiringSoon = ready && expiresAt - now.getTime() <= APPROVAL_EXPIRY_WARNING_MS;
  if (ready && !expiringSoon) {
    return null;
  }

  const hasRequestPermission = snapshot.access.permissions.includes("request_command");
  return {
    id: "qualification",
    priority: 5,
    sortAt: Number.isFinite(expiresAt) ? expiresAt : Number.MAX_SAFE_INTEGER,
    level: ready ? "medium" : "high",
    severityLabel: ready ? "준비 만료 임박" : "운영 준비 차단",
    title: ready ? "운영 준비 확인이 곧 만료됩니다" : "운영 준비 확인이 필요합니다",
    cause: qualification === null
      ? "현재 환경에 유효한 운영 준비 확인 기록이 없습니다."
      : qualification.environment !== snapshot.runtime_health.environment
        ? "현재 환경과 운영 준비 확인 환경이 일치하지 않습니다."
        : "운영 준비 확인 조건이 충족되지 않았거나 유효 시간이 지났습니다.",
    timing: qualification === null ? "유효 시각 확인 불가" : dueLabel(qualification.valid_until, now),
    action: hasRequestPermission
      ? {
          label: "준비 조건 확인",
          drawer: { kind: "command" },
          disabledReason: commandMutationBlockedReason(snapshot, isOnline, now)
        }
      : undefined,
    readOnlyReason: hasRequestPermission
      ? undefined
      : "현재 역할에는 운영 명령 요청 권한이 없어 읽기 전용으로 표시됩니다."
  };
}

function buildCommandItem(
  snapshot: OperationsSnapshot,
  command: OperationCommandReceipt,
  now: Date
): AttentionItemModel | null {
  const verified = isCommandPostconditionVerified(command, snapshot);
  if (verified || ["rejected", "expired", "canceled"].includes(command.state)) {
    return null;
  }
  if (command.state === "requested") {
    return commandAttentionItem(command, now, "독립 검토 대기", "요청 접수 후 독립 검토가 아직 확인되지 않았습니다.");
  }
  if (command.state === "approved") {
    return commandAttentionItem(command, now, "Worker 적용 대기", "독립 검토는 끝났지만 Worker 작업 인수가 아직 확인되지 않았습니다.");
  }
  if (command.state === "claimed") {
    return commandAttentionItem(command, now, "Worker 처리 중", "Worker 작업 인수 이후 적용 결과가 아직 확인되지 않았습니다.");
  }
  if (command.state === "failed") {
    return {
      ...commandAttentionItem(
        command,
        now,
        "적용 실패",
        command.worker_ack?.failure_code === null || command.worker_ack?.failure_code === undefined
          ? "Worker 적용이 실패했습니다. 실패 기록을 확인해야 합니다."
          : `Worker 적용 실패 · ${command.worker_ack.failure_code}`
      ),
      priority: 2,
      level: "critical"
    };
  }
  return commandAttentionItem(
    command,
    now,
    "최신 상태 확인 대기",
    "Worker 적용 보고만으로 완료하지 않고 최신 실행 상태의 결과를 확인합니다."
  );
}

function commandAttentionItem(
  command: OperationCommandReceipt,
  now: Date,
  severityLabel: string,
  cause: string
): AttentionItemModel {
  const expiresAt = Date.parse(command.expires_at);
  const ackOverdue =
    (command.state === "approved" || command.state === "claimed") &&
    Number.isFinite(expiresAt) &&
    expiresAt <= now.getTime();
  return {
    id: `command-${command.command_id}`,
    priority: ackOverdue ? 2 : 5,
    sortAt: Number.isFinite(expiresAt) ? expiresAt : safeTimestamp(command.requested_at),
    level: ackOverdue ? "high" : "info",
    severityLabel: ackOverdue ? "적용 확인 기한 초과" : severityLabel,
    title: commandLabel(command.command_type),
    cause,
    timing: command.state === "applied"
      ? `적용 보고 ${relativeAge(command.worker_ack?.applied_at ?? null, now)}`
      : dueLabel(command.expires_at, now),
    action: {
      label: "명령 상세",
      drawer: { kind: "command", entityId: command.command_id }
    }
  };
}

function approvalPermissionReason(
  snapshot: OperationsSnapshot,
  command: OperationCommandReceipt
): string | null {
  const access = snapshot.access;
  const actor = access.actor;
  if (!access.permissions.includes("review_command")) {
    return "현재 역할에는 독립 검토 권한이 없어 읽기 전용으로 표시됩니다.";
  }
  if (actor === null) {
    return null;
  }
  if (actor.actor_id === command.requested_by.actor_id) {
    return "본인이 요청한 작업은 검토할 수 없어 읽기 전용으로 표시됩니다.";
  }
  if (actor.roles.includes("platform_admin")) {
    return "플랫폼 관리자는 거래 운영 요청을 검토할 수 없습니다.";
  }
  if (command.command_type === "emergency_stop") {
    return "비상 정지는 독립 검토 대상이 아닙니다. 최신 명령 상태를 확인하세요.";
  }
  const requiredRole = command.command_type === "activate_paper_strategy"
    ? "strategy_reviewer"
    : "risk_approver";
  if (!actor.roles.includes(requiredRole)) {
    return "이 요청에 필요한 독립 검토 역할이 없어 읽기 전용으로 표시됩니다.";
  }
  return null;
}

function incidentMutationBlockedReason(
  snapshot: OperationsSnapshot,
  isOnline: boolean,
  now: Date
): string | undefined {
  const common = commonMutationBlockedReason(snapshot, isOnline, now);
  if (common !== undefined) {
    return common;
  }
  const required = ["control_plane", "realtime"] as const;
  for (const name of required) {
    const component = snapshot.runtime_health.components.find((item) => item.component === name);
    const maxAge = name === "realtime"
      ? snapshot.runtime_health.freshness_policy.realtime_max_age_seconds
      : snapshot.runtime_health.freshness_policy.snapshot_max_age_seconds;
    if (component === undefined || component.state !== "fresh" || !isWithinAge(component.observed_at, maxAge, now)) {
      return name === "realtime" ? "실시간 상태를 다시 확인해야 합니다." : "제어면 상태를 다시 확인해야 합니다.";
    }
  }
  if (
    !snapshot.runtime_health.realtime_connected ||
    snapshot.runtime_health.realtime_last_seen_at === null ||
    !isWithinAge(
      snapshot.runtime_health.realtime_last_seen_at,
      snapshot.runtime_health.freshness_policy.realtime_max_age_seconds,
      now
    )
  ) {
    return "실시간 신호를 다시 확인해야 합니다.";
  }
  return undefined;
}

function commandMutationBlockedReason(
  snapshot: OperationsSnapshot,
  isOnline: boolean,
  now: Date
): string | undefined {
  const incidentReason = incidentMutationBlockedReason(snapshot, isOnline, now);
  if (incidentReason !== undefined) {
    return incidentReason;
  }
  const health = snapshot.runtime_health;
  if (health.overall_state !== "fresh") {
    return "전체 운영 상태가 최신·정상으로 확인되지 않았습니다.";
  }
  if (health.live_permitted !== false) {
    return "현재 환경의 안전 잠금 상태를 확인할 수 없습니다.";
  }
  if (
    health.worker_heartbeat_at === null ||
    !isWithinAge(
      health.worker_heartbeat_at,
      health.freshness_policy.worker_heartbeat_max_age_seconds,
      now
    )
  ) {
    return "Worker 상태 신호를 다시 확인해야 합니다.";
  }
  const worker = health.components.find((component) => component.component === "worker");
  if (
    worker === undefined ||
    worker.state !== "fresh" ||
    !isWithinAge(worker.observed_at, health.freshness_policy.worker_heartbeat_max_age_seconds, now)
  ) {
    return "Worker 상태를 다시 확인해야 합니다.";
  }
  return undefined;
}

function commonMutationBlockedReason(
  snapshot: OperationsSnapshot,
  isOnline: boolean,
  now: Date
): string | undefined {
  if (!isOnline) {
    return "기기가 오프라인이라 권한 부여와 전송을 차단합니다.";
  }
  if (!snapshot.access.signed_in) {
    return "이 기기의 운영 계정 연결이 필요합니다.";
  }
  if (snapshot.access.session_state !== "active") {
    return "운영 세션이 만료되어 이 기기를 다시 연결해야 합니다.";
  }
  if (snapshot.access.assurance_level !== "aal2") {
    return "2단계 인증이 필요합니다.";
  }
  if (snapshot.access.actor === null) {
    return "현재 사용자 역할을 확인할 수 없습니다.";
  }
  if (
    !isWithinAge(
      snapshot.generated_at,
      snapshot.runtime_health.freshness_policy.snapshot_max_age_seconds,
      now
    ) ||
    !isWithinAge(
      snapshot.runtime_health.as_of,
      snapshot.runtime_health.freshness_policy.snapshot_max_age_seconds,
      now
    )
  ) {
    return "최신 운영 상태를 다시 확인해야 합니다.";
  }
  return undefined;
}

function canViewUnknownResolution(snapshot: OperationsSnapshot): boolean {
  const roles = new Set(snapshot.access.actor?.roles ?? []);
  return roles.has("operator") || roles.has("risk_approver") || roles.has("auditor");
}

function attentionIcon(item: AttentionItemModel, fallback: LucideIcon): LucideIcon {
  if (item.id === "device-offline" || item.id === "worker-offline") {
    return WifiOff;
  }
  if (item.id === "session-expired" || item.id === "aal2-required") {
    return KeyRound;
  }
  if (item.id.startsWith("approval-")) {
    return ClipboardCheck;
  }
  if (item.id.startsWith("unknown-") || item.id.startsWith("reconciliation-")) {
    return FileSearch;
  }
  if (item.id.startsWith("command-")) {
    return Activity;
  }
  return fallback;
}

function reconciliationReasonLabel(reason: string): string {
  const labels: Record<string, string> = {
    ack_timeout: "Worker 적용 확인 시간이 초과되었습니다.",
    ambiguous_order_state: "주문 상태를 한 가지 결과로 확정할 수 없습니다.",
    fill_mismatch: "체결 수량 또는 금액이 대사 결과와 일치하지 않습니다.",
    operator_escalation: "운영자가 수동 확인 대상으로 전달했습니다."
  };
  return labels[reason] ?? "수동 대사 증거를 확인해야 합니다.";
}

function dueLabel(timestamp: string, now: Date): string {
  const timestampMs = Date.parse(timestamp);
  if (!Number.isFinite(timestampMs)) {
    return "마감 시각 확인 불가";
  }
  const difference = timestampMs - now.getTime();
  if (difference <= 0) {
    return `${durationLabel(Math.abs(difference))} 초과`;
  }
  return `${durationLabel(difference)} 남음`;
}

function relativeAge(timestamp: string | null, now: Date): string {
  if (timestamp === null) {
    return "확인 불가";
  }
  const timestampMs = Date.parse(timestamp);
  if (!Number.isFinite(timestampMs)) {
    return "확인 불가";
  }
  const difference = now.getTime() - timestampMs;
  if (difference < -30_000) {
    return "시각 확인 필요";
  }
  return `${durationLabel(Math.max(0, difference))} 전`;
}

function durationLabel(durationMs: number): string {
  const minutes = Math.floor(durationMs / 60_000);
  if (minutes < 1) {
    return "1분 미만";
  }
  if (minutes < 60) {
    return `${minutes}분`;
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return `${hours}시간`;
  }
  return `${Math.floor(hours / 24)}일`;
}

function safeTimestamp(timestamp: string): number {
  const parsed = Date.parse(timestamp);
  return Number.isFinite(parsed) ? parsed : Number.MAX_SAFE_INTEGER;
}

function isWithinAge(timestamp: string, maxAgeSeconds: number, now: Date): boolean {
  const parsed = Date.parse(timestamp);
  const age = now.getTime() - parsed;
  return Number.isFinite(parsed) && age >= -30_000 && age <= maxAgeSeconds * 1_000;
}
