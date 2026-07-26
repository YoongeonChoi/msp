import {
  Activity,
  AlertTriangle,
  Clock3,
  Radio,
  ShieldCheck
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import type { ClientRealtimeHealth } from "../../lib/controlPlaneRealtime";
import type { OperationsSnapshot } from "../../lib/operationsContracts";
import { operationsErrorMessage } from "../../lib/operationsData";
import {
  buildSafetyRailModel,
  type SafetyRailItemKey,
  type SafetyRailTone
} from "../../lib/operationsStatusModel";
import { useOptionalOperationsSnapshot } from "../../lib/operationsSnapshotContext";

const itemIcons: Record<SafetyRailItemKey, LucideIcon> = {
  live: ShieldCheck,
  execution: Activity,
  overall: Activity,
  worker: Radio,
  snapshot: Clock3,
  command: ShieldCheck
};

const toneClasses: Record<SafetyRailTone, { readonly icon: string; readonly value: string }> = {
  neutral: { icon: "bg-surfaceRaised text-mutedStrong", value: "text-ink" },
  safe: { icon: "bg-successSoft text-success", value: "text-success" },
  warning: { icon: "bg-warningSoft text-warning", value: "text-warning" },
  danger: { icon: "bg-dangerSoft text-danger", value: "text-danger" },
  info: { icon: "bg-primarySoft text-primary", value: "text-primary" }
};

/** Uses the application-wide snapshot provider; no query or polling is owned here. */
export function OperationsStatusRail() {
  const source = useOptionalOperationsSnapshot();
  return (
    <OperationsStatusRailView
      snapshot={source?.snapshot ?? null}
      isOnline={source?.isOnline ?? true}
      errorMessage={source?.error ? operationsErrorMessage(source.error) : null}
      clientRealtime={source?.realtime ?? null}
    />
  );
}

export function OperationsStatusRailView({
  snapshot,
  isOnline,
  errorMessage,
  clientRealtime = null
}: {
  readonly snapshot: OperationsSnapshot | null;
  readonly isOnline: boolean;
  readonly errorMessage: string | null;
  readonly clientRealtime?: ClientRealtimeHealth | null;
}) {
  const model = buildSafetyRailModel(snapshot, isOnline, clientRealtime);
  const criticalMessages = errorMessage === null
    ? model.criticalMessages
    : [...model.criticalMessages, `운영 데이터 확인 실패: ${errorMessage}`];

  return (
    <section className="operations-status-rail" aria-label="운영 상태 레일">
      <div
        className="operations-status-rail__viewport"
        role="region"
        aria-label="운영 상태 항목"
        tabIndex={0}
      >
        <dl className="operations-status-rail__grid" aria-live="polite">
          {model.items.map((item) => {
            const Icon = itemIcons[item.key];
            const tone = toneClasses[item.tone];
            return (
              <div key={item.key} className="operations-status-rail__item">
                <dt>
                  <span className={`operations-status-rail__icon ${tone.icon}`}>
                    <Icon size={14} aria-hidden={true} />
                  </span>
                  {item.label}
                </dt>
                <dd>
                  <span className={`operations-status-rail__value ${tone.value}`} title={item.value}>
                    {item.value}
                  </span>
                  <span className="operations-status-rail__detail" title={item.detail}>
                    {item.detail}
                  </span>
                </dd>
              </div>
            );
          })}
        </dl>
      </div>

      {criticalMessages.length > 0 ? (
        <div
          className="operations-status-rail__alert"
          role="alert"
          aria-live="assertive"
        >
          <div>
            <AlertTriangle className="mt-0.5 shrink-0" size={17} aria-hidden={true} />
            <span>중요 경고 · {criticalMessages.join(" · ")}</span>
          </div>
        </div>
      ) : null}
    </section>
  );
}
