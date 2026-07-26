import { useRef, useState } from "react";
import { CheckCircle2, Siren } from "lucide-react";
import type { Incident, OperationsSnapshot } from "../../lib/operationsContracts";
import { formatKst } from "../../lib/formatters";
import { incidentConfirmationGuard } from "../../lib/operationGuards";
import { humanizeOperationalEvidenceText } from "../../lib/presentation";
import type { ConfirmAction } from "../../lib/uiState";
import { ConfirmDialog } from "../DialogSurface";
import { EmptyState, Panel, Pill, SectionTitle, pageButtonClass } from "../ui";
import type { Tone } from "../ui";

type IncidentDecision = "acknowledge" | "resolve";

export function IncidentCenter({
  snapshot,
  mutationsAllowed,
  pending,
  onAction
}: {
  readonly snapshot: OperationsSnapshot;
  readonly mutationsAllowed: boolean;
  readonly pending: boolean;
  readonly onAction: (incident: Incident, action: "acknowledge" | "resolve", openGuard: string) => void;
}) {
  const [confirmation, setConfirmation] = useState<ConfirmAction<IncidentDecision> | null>(null);
  const confirmationGuardRef = useRef<string | null>(null);
  const [confirmationError, setConfirmationError] = useState<string | null>(null);
  const canAcknowledge = snapshot.access.permissions.includes("acknowledge_incident");
  const canResolve = snapshot.access.permissions.includes("resolve_incident");
  const actorRoles = snapshot.access.actor?.roles ?? [];
  const actorId = snapshot.access.actor?.actor_id ?? null;
  const openIncidents = snapshot.incidents.filter((incident) => incident.status !== "resolved");

  const closeConfirmation = () => {
    confirmationGuardRef.current = null;
    setConfirmation(null);
    setConfirmationError(null);
  };

  const openConfirmation = (incident: Incident, decision: IncidentDecision) => {
    confirmationGuardRef.current = incidentConfirmationGuard(snapshot, incident, decision);
    setConfirmationError(null);
    setConfirmation({
      kind: decision,
      entityId: incident.incident_id,
      label: humanizeOperationalEvidenceText(incident.title),
      tone: "primary"
    });
  };

  const submitResolution = () => {
    if (confirmation === null) {
      return;
    }

    const currentIncident = snapshot.incidents.find(
      (incident) => incident.incident_id === confirmation.entityId
    );
    const expectedStatus = confirmation.kind === "acknowledge" ? "open" : "acknowledged";
    if (currentIncident === undefined || currentIncident.status !== expectedStatus) {
      setConfirmationError("상태가 변경됨 — 사고 목록을 새로 확인한 뒤 다시 검토해 주세요.");
      return;
    }
    const blockedReason = incidentActionBlockedReason({
      incident: currentIncident,
      action: confirmation.kind,
      mutationsAllowed,
      pending,
      canAcknowledge,
      canResolve,
      actorRoles,
      actorId
    });
    if (blockedReason !== null) {
      setConfirmationError(`상태가 변경됨 — ${blockedReason}`);
      return;
    }

    const openGuard = confirmationGuardRef.current;
    if (openGuard === null) {
      setConfirmationError("상태가 변경됨 — 확인 정보를 다시 열어 검토해 주세요.");
      return;
    }

    onAction(currentIncident, confirmation.kind, openGuard);
    closeConfirmation();
  };

  return (
    <Panel>
      <SectionTitle
        title="사고 센터"
        detail={<Pill tone={openIncidents.length > 0 ? "danger" : "safe"}>{openIncidents.length}건 미해결</Pill>}
      />
      {snapshot.incidents.length === 0 ? (
        <EmptyState title="기록된 사고가 없습니다" detail="stale, offline, 명령 timeout과 계약 오류가 이곳에 집계됩니다." />
      ) : (
        <div className="space-y-3">
          {snapshot.incidents.map((incident) => {
            const acknowledgeBlockedReason = incidentActionBlockedReason({
              incident,
              action: "acknowledge",
              mutationsAllowed,
              pending,
              canAcknowledge,
              canResolve,
              actorRoles,
              actorId
            });
            const resolveBlockedReason = incidentActionBlockedReason({
              incident,
              action: "resolve",
              mutationsAllowed,
              pending,
              canAcknowledge,
              canResolve,
              actorRoles,
              actorId
            });
            const acknowledgeDisabled = acknowledgeBlockedReason !== null;
            const resolveDisabled = resolveBlockedReason !== null;
            const acknowledgeReasonId = `incident-acknowledge-blocked-${incident.incident_id}`;
            const resolveReasonId = `incident-resolve-blocked-${incident.incident_id}`;
            return (
              <article key={incident.incident_id} className="rounded-lg border border-lineSubtle p-4">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <p className="flex items-center gap-2 font-semibold text-ink">
                      <Siren size={17} aria-hidden="true" />
                      {humanizeOperationalEvidenceText(incident.title)}
                    </p>
                    <p className="mt-1 text-sm text-muted">{humanizeOperationalEvidenceText(incident.summary)}</p>
                  </div>
                  <div className="flex gap-2">
                    <Pill tone={severityTone(incident.severity)}>{incident.severity.toUpperCase()}</Pill>
                    <Pill tone={incident.status === "resolved" ? "safe" : "warning"}>{incidentStatusLabel(incident.status)}</Pill>
                  </div>
                </div>
                <p className="mt-2 text-xs text-muted">
                  감지 {formatKst(incident.detected_at)} · 담당 {incident.owner?.display_name ?? "미배정"} · 증거 {incident.evidence_refs.length}개
                </p>
                <p className="mt-1 text-xs text-muted">
                  사고 확인 기한 {formatKst(incident.ack_due_at)} · 상향 보고 {escalationStatusLabel(incident.escalation_status)}
                </p>
                {incident.status !== "resolved" ? (
                  <div className="mt-3 flex flex-wrap gap-2">
                    {acknowledgeBlockedReason !== null ? (
                      <p id={acknowledgeReasonId} className="w-full text-xs text-warning">
                        사고 확인 차단 · {acknowledgeBlockedReason}
                      </p>
                    ) : null}
                    {resolveBlockedReason !== null ? (
                      <p id={resolveReasonId} className="w-full text-xs text-warning">
                        해결 확인 차단 · {resolveBlockedReason}
                      </p>
                    ) : null}
                    <button
                      type="button"
                      className={`${pageButtonClass("warning")} min-h-11`}
                      disabled={acknowledgeDisabled}
                      aria-describedby={acknowledgeDisabled ? acknowledgeReasonId : undefined}
                      onClick={() => openConfirmation(incident, "acknowledge")}
                    >
                      확인 접수
                    </button>
                    <button
                      type="button"
                      className={`${pageButtonClass("safe")} min-h-11`}
                      disabled={resolveDisabled}
                      aria-describedby={resolveDisabled ? resolveReasonId : undefined}
                      onClick={() => openConfirmation(incident, "resolve")}
                    >
                      <CheckCircle2 size={16} aria-hidden="true" />
                      해결 확인
                    </button>
                  </div>
                ) : null}
              </article>
            );
          })}
        </div>
      )}
      <ConfirmDialog
        open={confirmation !== null}
        title={confirmation?.kind === "acknowledge" ? "사고 확인 접수" : "사고 해결 확인"}
        description={
          confirmation === null
            ? undefined
            : confirmation.kind === "acknowledge"
              ? `${confirmation.label}의 최신 상태와 사고 확인 기한을 다시 확인한 뒤 접수합니다.`
              : `${confirmation.label}의 완화 증거와 최신 사고 상태를 다시 확인한 뒤 해결 상태로 전환합니다.`
        }
        confirmLabel={confirmation?.kind === "acknowledge" ? "사고 확인 접수" : "해결 상태로 전환"}
        pendingLabel="사고 상태 확인 중"
        tone={confirmation?.tone ?? "primary"}
        pending={pending}
        error={confirmationError}
        onConfirm={submitResolution}
        onCancel={closeConfirmation}
      />
    </Panel>
  );
}

function incidentActionBlockedReason({
  incident,
  action,
  mutationsAllowed,
  pending,
  canAcknowledge,
  canResolve,
  actorRoles,
  actorId
}: {
  readonly incident: Incident;
  readonly action: "acknowledge" | "resolve";
  readonly mutationsAllowed: boolean;
  readonly pending: boolean;
  readonly canAcknowledge: boolean;
  readonly canResolve: boolean;
  readonly actorRoles: readonly string[];
  readonly actorId: string | null;
}): string | null {
  if (pending) {
    return "다른 운영 요청을 처리하고 있습니다.";
  }
  if (!mutationsAllowed) {
    return "현재 연결·인증 또는 데이터 최신성 조건에서 사고 작업을 전송할 수 없습니다.";
  }
  if (action === "acknowledge") {
    if (!canAcknowledge) {
      return "현재 역할에는 사고 확인 권한이 없습니다.";
    }
    if (!actorRoles.includes("operator")) {
      return "사고 확인은 운영 담당자 역할만 수행할 수 있습니다.";
    }
    return incident.status === "open" ? null : "열린 사고만 확인할 수 있습니다.";
  }
  if (!canResolve) {
    return "현재 역할에는 사고 해결 권한이 없습니다.";
  }
  if (!actorRoles.includes("risk_approver")) {
    return "사고 해결은 위험 검토자 역할만 수행할 수 있습니다.";
  }
  if (incident.owner === null || actorId === null) {
    return "사고 확인 담당자와 해결 검토자의 분리를 확인할 수 없습니다.";
  }
  if (incident.owner.actor_id === actorId) {
    return "사고를 확인한 사용자는 같은 사고의 해결 검토를 할 수 없습니다.";
  }
  return incident.status === "acknowledged" ? null : "사고 확인이 접수된 상태에서만 해결할 수 있습니다.";
}

function severityTone(severity: Incident["severity"]): Tone {
  if (severity === "sev1" || severity === "sev2") {
    return "danger";
  }
  if (severity === "sev3") {
    return "warning";
  }
  return "info";
}

function incidentStatusLabel(status: Incident["status"]): string {
  const labels: Record<Incident["status"], string> = {
    open: "열림",
    acknowledged: "접수됨",
    mitigating: "완화 중",
    resolved: "해결됨"
  };
  return labels[status];
}

function escalationStatusLabel(status: Incident["escalation_status"]): string {
  const labels: Record<Incident["escalation_status"], string> = {
    not_required: "불필요",
    pending: "대기 중",
    canceled: "취소됨",
    escalated: "전달됨"
  };
  return labels[status];
}
