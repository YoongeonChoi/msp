import { Activity, Ban, Clock3, FileWarning, Radio, ShieldCheck } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import type { OperationsSnapshot, RuntimeHealth } from "../../lib/operationsContracts";
import {
  operationsDataApi,
  operationsErrorMessage,
  operationsSnapshotQueryKey
} from "../../lib/operationsData";
import type { OperationsDataApi } from "../../lib/operationsData";
import { formatKst } from "../../lib/formatters";
import { useOnlineStatus } from "../../lib/useOnlineStatus";
import { Pill } from "../ui";
import type { Tone } from "../ui";
import { runtimeStateLabel } from "./StaleDataBoundary";
import { isCommandPostconditionVerified } from "./SafetyCommandCenter";
import { useControlPlaneRealtimeHealth } from "../../lib/controlPlaneRealtime";
import type { ClientRealtimeHealth } from "../../lib/controlPlaneRealtime";

export function OperationsStatusRail({
  dataApi = operationsDataApi,
  onlineOverride
}: {
  readonly dataApi?: OperationsDataApi;
  readonly onlineOverride?: boolean;
}) {
  const isOnline = useOnlineStatus(onlineOverride);
  const clientRealtime = useControlPlaneRealtimeHealth();
  const snapshot = useQuery({
    queryKey: operationsSnapshotQueryKey,
    queryFn: dataApi.fetchSnapshot,
    retry: false,
    refetchInterval: 15_000
  });

  return (
    <OperationsStatusRailView
      snapshot={snapshot.data ?? null}
      isOnline={isOnline}
      errorMessage={snapshot.error ? operationsErrorMessage(snapshot.error) : null}
      clientRealtime={clientRealtime}
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
  const health = snapshot?.runtime_health ?? null;
  const displayState: RuntimeHealth["overall_state"] = !isOnline ? "offline" : health?.overall_state ?? "contract_error";
  const openIncidents = snapshot?.incidents.filter((incident) => incident.status !== "resolved").length;
  const pendingAck = snapshot?.commands.filter((command) =>
    command.state === "approved" ||
    command.state === "claimed" ||
    (command.state === "applied" && !isCommandPostconditionVerified(command, snapshot))
  ).length;
  const realtimeConnected = clientRealtime === null
    ? health?.realtime_connected ?? false
    : clientRealtime.connected && clientRealtime.connectedAt !== null;
  const realtimeLastSignalAt = clientRealtime === null
    ? health?.realtime_last_seen_at ?? null
    : clientRealtime.lastSignalAt;

  return (
    <header className="border-b border-line bg-white" aria-label="운영 상태 레일">
      <div className="flex flex-wrap items-center gap-2 px-4 py-3">
        <Pill tone="danger">
          <Ban size={13} aria-hidden="true" />
          LIVE 금지
        </Pill>
        <Pill tone={healthTone(displayState)}>
          <Activity size={13} aria-hidden="true" />
          운영 상태: {runtimeStateLabel(displayState)}
        </Pill>
        <Pill tone={health?.environment === "contract_test" ? "info" : "safe"}>
          <ShieldCheck size={13} aria-hidden="true" />
          환경: {health ? environmentLabel(health.environment) : "확인 불가"}
        </Pill>
        <Pill tone={snapshot?.qualification?.status === "qualified" ? "safe" : "warning"}>
          G1/G2: {snapshot?.qualification?.status === "qualified" ? "통과" : "미충족"}
        </Pill>
        <Pill tone={pendingAck && pendingAck > 0 ? "warning" : "neutral"}>
          <Radio size={13} aria-hidden="true" />
          Worker ACK 대기: {pendingAck ?? "-"}
        </Pill>
        <Pill tone={realtimeConnected ? "safe" : "danger"}>
          <Radio size={13} aria-hidden="true" />
          Realtime: {realtimeConnected
            ? `연결 · 서버 신호 ${realtimeLastSignalAt === null ? "대기" : formatKst(realtimeLastSignalAt)}`
            : "단절 · 15초 polling 조회 전용"}
        </Pill>
        <Pill tone={openIncidents && openIncidents > 0 ? "danger" : "neutral"}>
          <FileWarning size={13} aria-hidden="true" />
          미해결 사고: {openIncidents ?? "-"}
        </Pill>
        <Pill tone="neutral">
          <Clock3 size={13} aria-hidden="true" />
          기준 시각: {health ? formatKst(health.as_of) : "-"}
        </Pill>
      </div>
      {errorMessage ? (
        <div className="border-t border-red-200 bg-red-50 px-4 py-2 text-sm text-red-800" role="alert">
          {errorMessage}
        </div>
      ) : null}
    </header>
  );
}

function environmentLabel(environment: RuntimeHealth["environment"]): string {
  return environment === "paper" ? "PAPER" : "CONTRACT TEST / 계약 테스트";
}

function healthTone(state: RuntimeHealth["overall_state"]): Tone {
  if (state === "fresh") {
    return "safe";
  }
  if (state === "degraded" || state === "stale") {
    return "warning";
  }
  return "danger";
}
