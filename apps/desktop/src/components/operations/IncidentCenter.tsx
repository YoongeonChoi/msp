import { CheckCircle2, Siren } from "lucide-react";
import type { Incident, OperationsSnapshot } from "../../lib/operationsContracts";
import { formatKst } from "../../lib/formatters";
import { EmptyState, Panel, Pill, SectionTitle, pageButtonClass } from "../ui";
import type { Tone } from "../ui";

export function IncidentCenter({
  snapshot,
  mutationsAllowed,
  pending,
  onAction
}: {
  readonly snapshot: OperationsSnapshot;
  readonly mutationsAllowed: boolean;
  readonly pending: boolean;
  readonly onAction: (incident: Incident, action: "acknowledge" | "resolve") => void;
}) {
  const canAcknowledge = snapshot.access.permissions.includes("acknowledge_incident");
  const canResolve = snapshot.access.permissions.includes("resolve_incident");
  const openIncidents = snapshot.incidents.filter((incident) => incident.status !== "resolved");

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
            const acknowledgeDisabled =
              pending || !mutationsAllowed || !canAcknowledge || incident.status !== "open";
            const resolveDisabled =
              pending ||
              !mutationsAllowed ||
              !canResolve ||
              !["acknowledged", "mitigating"].includes(incident.status);
            return (
              <article key={incident.incident_id} className="rounded-md border border-line p-3">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <p className="flex items-center gap-2 font-semibold text-ink">
                      <Siren size={17} aria-hidden="true" />
                      {incident.title}
                    </p>
                    <p className="mt-1 text-sm text-muted">{incident.summary}</p>
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
                  ACK 기한 {formatKst(incident.ack_due_at)} · escalation {incident.escalation_status}
                </p>
                {incident.status !== "resolved" ? (
                  <div className="mt-3 flex flex-wrap gap-2">
                    <button
                      type="button"
                      className={pageButtonClass("warning")}
                      disabled={acknowledgeDisabled}
                      onClick={() => onAction(incident, "acknowledge")}
                    >
                      확인 접수
                    </button>
                    <button
                      type="button"
                      className={pageButtonClass("safe")}
                      disabled={resolveDisabled}
                      onClick={() => {
                        if (window.confirm("완화 증거를 확인했으며 사고를 해결 상태로 전환할까요?")) {
                          onAction(incident, "resolve");
                        }
                      }}
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
    </Panel>
  );
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
