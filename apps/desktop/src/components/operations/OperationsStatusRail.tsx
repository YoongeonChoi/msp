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
  neutral: { icon: "bg-slate-100 text-mutedStrong", value: "text-ink" },
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
    <section className="border-t border-line/80" aria-label="운영 상태 레일">
      <div className="mx-auto max-w-[1440px] px-4 py-3 md:px-6" aria-live="polite">
        <dl className="grid grid-cols-2 gap-px overflow-hidden rounded-xl border border-line bg-line lg:grid-cols-3 xl:grid-cols-6">
          {model.items.map((item) => {
            const Icon = itemIcons[item.key];
            const tone = toneClasses[item.tone];
            return (
              <div key={item.key} className="min-w-0 bg-surface px-3 py-3">
                <dt className="flex items-center gap-2 text-xs font-medium text-mutedStrong">
                  <span className={`grid h-7 w-7 shrink-0 place-items-center rounded-full ${tone.icon}`}>
                    <Icon size={14} aria-hidden={true} />
                  </span>
                  {item.label}
                </dt>
                <dd className="mt-2 min-w-0 pl-9">
                  <span className={`block truncate text-sm font-bold ${tone.value}`} title={item.value}>
                    {item.value}
                  </span>
                  <span className="mt-0.5 block [overflow-wrap:anywhere] text-xs text-mutedStrong" title={item.detail}>
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
          className="border-t border-danger/25 bg-dangerSoft px-4 py-2.5 text-sm font-medium text-danger md:px-6"
          role="alert"
          aria-live="assertive"
        >
          <div className="mx-auto flex max-w-[1392px] items-start gap-2">
            <AlertTriangle className="mt-0.5 shrink-0" size={17} aria-hidden={true} />
            <span>중요 경고 · {criticalMessages.join(" · ")}</span>
          </div>
        </div>
      ) : null}
    </section>
  );
}
