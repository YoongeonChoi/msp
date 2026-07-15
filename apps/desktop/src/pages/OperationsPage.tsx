import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { OperationCommandReceipt, OperationsSnapshot } from "../lib/operationsContracts";
import type {
  CommandReviewDraft,
  OperationCommandRequest,
  StepUpCommandDraft,
  UnknownResolutionRequestDraftV2,
  UnknownResolutionReviewDraftV2
} from "../lib/operationsContracts";
import {
  attachStepUpGrantToCommandDraft,
  attachStepUpGrantToReviewDraft,
  buildCommandReviewDraft,
  buildEmergencyStopCommandRequest,
  buildIncidentActionRequest,
  buildOperationCommandDraft,
  buildStepUpGrantDraftRequest,
  secureOperationId
} from "../lib/operationRequests";
import type { OperationIdFactory } from "../lib/operationRequests";
import {
  operationsDataApi,
  operationsErrorMessage,
  operationsSnapshotQueryKey,
  unknownResolutionDataApi,
  unknownResolutionSnapshotQueryKey
} from "../lib/operationsData";
import type { OperationsDataApi, UnknownResolutionDataApi } from "../lib/operationsData";
import {
  attachUnknownResolutionRequestGrant,
  attachUnknownResolutionReviewGrant,
  buildUnknownResolutionRequestDraft,
  buildUnknownResolutionReviewDraft,
  buildUnknownResolutionStepUpRequest
} from "../lib/unknownResolutionRequests";
import { useOnlineStatus } from "../lib/useOnlineStatus";
import { useControlPlaneRealtimeHealth } from "../lib/controlPlaneRealtime";
import type { ClientRealtimeHealth } from "../lib/controlPlaneRealtime";
import { ApprovalInbox } from "../components/operations/ApprovalInbox";
import { IncidentCenter } from "../components/operations/IncidentCenter";
import { ManualReconciliationCase } from "../components/operations/ManualReconciliationCase";
import {
  isCommandPostconditionVerified,
  SafetyCommandCenter
} from "../components/operations/SafetyCommandCenter";
import { StaleDataBoundary } from "../components/operations/StaleDataBoundary";
import { ErrorState, LoadingState, Metric, Pill } from "../components/ui";

export interface OperationsPageProps {
  readonly dataApi?: OperationsDataApi;
  readonly unknownDataApi?: UnknownResolutionDataApi;
  readonly onlineOverride?: boolean;
  readonly idFactory?: OperationIdFactory;
  readonly nowFactory?: () => Date;
}

export function OperationsPage({
  dataApi = operationsDataApi,
  unknownDataApi = unknownResolutionDataApi,
  onlineOverride,
  idFactory = secureOperationId,
  nowFactory = () => new Date()
}: OperationsPageProps = {}) {
  const queryClient = useQueryClient();
  const isOnline = useOnlineStatus(onlineOverride);
  const clientRealtime = useControlPlaneRealtimeHealth();
  const [notice, setNotice] = useState<string | null>(null);
  const snapshotQuery = useQuery({
    queryKey: operationsSnapshotQueryKey,
    queryFn: dataApi.fetchSnapshot,
    retry: false,
    refetchInterval: 15_000
  });
  const unknownSnapshotQuery = useQuery({
    queryKey: unknownResolutionSnapshotQueryKey,
    queryFn: unknownDataApi.fetchSnapshot,
    retry: false,
    refetchInterval: 15_000
  });

  const requestMutation = useMutation({
    mutationFn: async (
      submission:
        | { readonly kind: "emergency"; readonly request: OperationCommandRequest }
        | { readonly kind: "step_up"; readonly draft: StepUpCommandDraft }
    ) => {
      if (submission.kind === "emergency") {
        return dataApi.requestCommand(submission.request);
      }
      const grant = await dataApi.issueStepUpGrant(
        buildStepUpGrantDraftRequest("request", submission.draft)
      );
      return dataApi.requestCommand(attachStepUpGrantToCommandDraft(submission.draft, grant));
    },
    retry: false,
    networkMode: "always",
    onSuccess: () => {
      setNotice("명령별 step-up grant와 제어면 영수증이 확인되었습니다. Worker ACK가 오기 전에는 적용 완료가 아닙니다.");
      return queryClient.invalidateQueries({ queryKey: operationsSnapshotQueryKey });
    },
    onError: (error) => setNotice(operationsErrorMessage(error))
  });
  const reviewMutation = useMutation({
    mutationFn: async (draft: CommandReviewDraft) => {
      const grant = await dataApi.issueStepUpGrant(buildStepUpGrantDraftRequest("review", draft));
      return dataApi.reviewCommand(attachStepUpGrantToReviewDraft(draft, grant));
    },
    retry: false,
    networkMode: "always",
    onSuccess: () => {
      setNotice("승인 결과가 제어면에 기록되었습니다. Worker 적용 상태는 별도 ACK로 확인하세요.");
      return queryClient.invalidateQueries({ queryKey: operationsSnapshotQueryKey });
    },
    onError: (error) => setNotice(operationsErrorMessage(error))
  });
  const incidentMutation = useMutation({
    mutationFn: dataApi.actOnIncident,
    retry: false,
    networkMode: "always",
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: operationsSnapshotQueryKey });
      setNotice("사고 상태 변경이 감사 가능한 제어면 기록으로 반영되었습니다.");
    },
    onError: (error) => setNotice(operationsErrorMessage(error))
  });
  const unknownRequestMutation = useMutation({
    mutationFn: async (draft: UnknownResolutionRequestDraftV2) => {
      const grant = await unknownDataApi.issueStepUpGrant(
        buildUnknownResolutionStepUpRequest("request", draft)
      );
      return unknownDataApi.requestResolution(
        attachUnknownResolutionRequestGrant(draft, grant)
      );
    },
    retry: false,
    networkMode: "always",
    onSuccess: () => {
      setNotice("수동 대사 요청과 불변 증거 manifest가 저장되었습니다. 독립 승인과 Worker ACK 전에는 회계 조정 완료가 아닙니다.");
      void queryClient.invalidateQueries({ queryKey: operationsSnapshotQueryKey });
      return queryClient.invalidateQueries({ queryKey: unknownResolutionSnapshotQueryKey });
    },
    onError: (error) => setNotice(operationsErrorMessage(error))
  });
  const unknownReviewMutation = useMutation({
    mutationFn: async (draft: UnknownResolutionReviewDraftV2) => {
      const grant = await unknownDataApi.issueStepUpGrant(
        buildUnknownResolutionStepUpRequest("review", draft)
      );
      return unknownDataApi.reviewResolution(
        attachUnknownResolutionReviewGrant(draft, grant)
      );
    },
    retry: false,
    networkMode: "always",
    onSuccess: () => {
      setNotice("수동 대사 검토 receipt가 저장되었습니다. Worker claim/application과 회계 postcondition을 계속 확인하세요.");
      void queryClient.invalidateQueries({ queryKey: operationsSnapshotQueryKey });
      return queryClient.invalidateQueries({ queryKey: unknownResolutionSnapshotQueryKey });
    },
    onError: (error) => setNotice(operationsErrorMessage(error))
  });

  if (snapshotQuery.isLoading) {
    return <LoadingState label="schema_version=1 운영 read model을 불러오는 중" />;
  }
  if (snapshotQuery.error || !snapshotQuery.data) {
    return (
      <div className="space-y-3">
        <div className="rounded-md border border-red-300 bg-red-50 p-4" role="alert">
          <div className="flex items-center gap-2 font-semibold text-red-900">
            운영 경로 차단 <Pill tone="danger">LIVE 금지</Pill>
          </div>
          <p className="mt-2 text-sm text-red-800">
            {operationsErrorMessage(snapshotQuery.error)} 부분 응답이나 레거시 row를 기본값으로 보정하지 않습니다.
          </p>
        </div>
        <ErrorState message="api.get_desktop_operations_snapshot_v1이 OperationsSnapshotV1 전체 계약을 제공하지 않습니다." />
      </div>
    );
  }

  const snapshot = snapshotQuery.data;
  const snapshotEvaluationTime = nowFactory();
  const clientFresh = isOperationsSnapshotFresh(snapshot, snapshotEvaluationTime, clientRealtime);
  const mutationsAllowed = canMutateOperations(snapshot, isOnline, snapshotEvaluationTime, clientRealtime);
  const incidentMutationsAllowed = canMutateIncidentOperations(snapshot, isOnline, snapshotEvaluationTime, clientRealtime);
  const unknownSnapshot = unknownSnapshotQuery.error ? null : unknownSnapshotQuery.data ?? null;
  const unknownMutationsAllowed = mutationsAllowed &&
    unknownSnapshot !== null &&
    isTimestampFresh(
      unknownSnapshot.generated_at,
      snapshotEvaluationTime,
      snapshot.runtime_health.freshness_policy.snapshot_max_age_seconds
    );
  const pending = requestMutation.isPending ||
    reviewMutation.isPending ||
    incidentMutation.isPending ||
    unknownRequestMutation.isPending ||
    unknownReviewMutation.isPending;

  const rejectBlockedMutation = () => {
    setNotice("현재 상태에서는 변경할 수 없습니다. fresh 상태와 유효한 온라인 세션을 먼저 확인하세요.");
  };

  return (
    <StaleDataBoundary health={snapshot.runtime_health} isOnline={isOnline} clientFresh={clientFresh}>
      <section className="rounded-md border border-red-200 bg-red-50 p-4" aria-label="영구 안전 경계">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="font-semibold text-red-950">영구 안전 경계</h2>
          <Pill tone="danger">LIVE 금지</Pill>
        </div>
        <p className="mt-1 text-sm text-red-900">
          이 데스크톱은 PAPER와 CONTRACT TEST / 계약 테스트만 제어합니다. 브로커 주문 API나 LIVE 활성화 경로는 제공하지 않습니다.
        </p>
      </section>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Metric title="운영 상태" value={snapshot.runtime_health.overall_state} detail={mutationsAllowed ? "변경 가능" : "변경 차단"} tone={mutationsAllowed ? "safe" : "warning"} />
        <Metric title="승인 대기" value={`${snapshot.pending_reviews.length}건`} detail="제어면 receipt" tone={snapshot.pending_reviews.length > 0 ? "warning" : "neutral"} />
        <Metric title="ACK/postcondition 대기" value={`${pendingWorkerAckCount(snapshot)}건`} detail="승인과 분리" tone={pendingWorkerAckCount(snapshot) > 0 ? "warning" : "neutral"} />
        <Metric
          title="수동 대사"
          value={unknownSnapshot ? `${unknownSnapshot.cases.length}건` : "확인 불가"}
          detail="V2 증거·CAS 전용"
          tone={!unknownSnapshot || unknownSnapshot.cases.length > 0 ? "danger" : "safe"}
        />
      </div>

      {notice ? (
        <div className="rounded-md border border-sky-200 bg-sky-50 p-3 text-sm text-sky-900" role="status" aria-live="polite">
          {notice}
        </div>
      ) : null}

      <div className="grid gap-4 xl:grid-cols-[1.25fr_0.75fr]">
        <SafetyCommandCenter
          snapshot={snapshot}
          mutationsAllowed={mutationsAllowed}
          pending={pending}
          onRequest={(commandType) => {
            if (!mutationsAllowed) {
              rejectBlockedMutation();
              return;
            }
            if (commandType === "emergency_stop") {
              const request = buildEmergencyStopCommandRequest({ snapshot, now: nowFactory(), idFactory });
              if (request === null) {
                setNotice("최근 AAL2가 확인된 operator 단일 행위자 조건이 없어 비상 정지 요청을 전송하지 않았습니다.");
                return;
              }
              requestMutation.mutate({ kind: "emergency", request });
              return;
            }
            const draft = buildOperationCommandDraft({ commandType, snapshot, now: nowFactory(), idFactory });
            if (draft === null) {
              setNotice("AAL2, 요청 권한 또는 G1/G2 자격이 없어 명령 draft를 전송하지 않았습니다.");
              return;
            }
            requestMutation.mutate({ kind: "step_up", draft });
          }}
        />
        <ApprovalInbox
          snapshot={snapshot}
          mutationsAllowed={mutationsAllowed}
          pending={pending}
          onReview={(command, decision) => {
            if (!mutationsAllowed || isSelfReview(snapshot, command)) {
              rejectBlockedMutation();
              return;
            }
            const review = buildCommandReviewDraft({
              command,
              access: snapshot.access,
              decision,
              now: nowFactory(),
              idFactory
            });
            if (review === null) {
              setNotice("승인 역할 또는 maker-checker 분리 조건이 없어 검토 draft를 전송하지 않았습니다.");
              return;
            }
            reviewMutation.mutate(review);
          }}
        />
        <IncidentCenter
          snapshot={snapshot}
          mutationsAllowed={incidentMutationsAllowed}
          pending={pending}
          onAction={(incident, action) => {
            if (!incidentMutationsAllowed) {
              rejectBlockedMutation();
              return;
            }
            incidentMutation.mutate(
              buildIncidentActionRequest({ incident, action, now: nowFactory(), idFactory })
            );
          }}
        />
        <ManualReconciliationCase
          snapshot={snapshot}
          unknownSnapshot={unknownSnapshot}
          mutationsAllowed={unknownMutationsAllowed}
          pending={pending}
          now={snapshotEvaluationTime}
          onRequest={(context, evidence) => {
            if (!unknownMutationsAllowed) {
              rejectBlockedMutation();
              return;
            }
            try {
              const draft = buildUnknownResolutionRequestDraft({
                context,
                access: snapshot.access,
                evidence,
                now: nowFactory(),
                idFactory
              });
              if (draft === null) {
                setNotice("operator, AAL2, 최신 CAS 또는 명시적 증거 조건이 없어 수동 대사 요청을 전송하지 않았습니다.");
                return;
              }
              unknownRequestMutation.mutate(draft);
            } catch (error) {
              setNotice(operationsErrorMessage(error));
            }
          }}
          onReview={(context, decision) => {
            if (!unknownMutationsAllowed) {
              rejectBlockedMutation();
              return;
            }
            try {
              const draft = buildUnknownResolutionReviewDraft({
                context,
                access: snapshot.access,
                decision,
                now: nowFactory(),
                idFactory
              });
              if (draft === null) {
                setNotice("risk_approver, AAL2, maker-checker 또는 최신 receipt revision 조건이 없어 검토를 전송하지 않았습니다.");
                return;
              }
              unknownReviewMutation.mutate(draft);
            } catch (error) {
              setNotice(operationsErrorMessage(error));
            }
          }}
        />
      </div>
    </StaleDataBoundary>
  );
}

export function canMutateOperations(
  snapshot: OperationsSnapshot,
  isOnline: boolean,
  now = new Date(),
  realtime: ClientRealtimeHealth | null = null
): boolean {
  return (
    isOnline &&
    isOperationsSnapshotFresh(snapshot, now, realtime) &&
    snapshot.runtime_health.live_permitted === false &&
    snapshot.access.signed_in &&
    snapshot.access.session_state === "active" &&
    snapshot.access.assurance_level === "aal2" &&
    snapshot.access.actor !== null
  );
}

export function canMutateIncidentOperations(
  snapshot: OperationsSnapshot,
  isOnline: boolean,
  now = new Date(),
  realtime: ClientRealtimeHealth | null = null
): boolean {
  return (
    isOnline &&
    isControlPlaneSnapshotCurrent(snapshot, now, realtime) &&
    snapshot.runtime_health.live_permitted === false &&
    snapshot.access.signed_in &&
    snapshot.access.session_state === "active" &&
    snapshot.access.assurance_level === "aal2" &&
    snapshot.access.actor !== null
  );
}

export function isOperationsSnapshotFresh(
  snapshot: OperationsSnapshot,
  now: Date,
  realtime: ClientRealtimeHealth | null = null
): boolean {
  const health = snapshot.runtime_health;
  const nowMs = now.getTime();
  const maxFutureSkewMs = 30_000;
  const isWithinAge = (timestamp: string, maxAgeSeconds: number) => {
    const ageMs = nowMs - Date.parse(timestamp);
    return Number.isFinite(ageMs) && ageMs >= -maxFutureSkewMs && ageMs <= maxAgeSeconds * 1_000;
  };
  const realtimeHealthy = realtime === null
    ? health.realtime_connected &&
      health.realtime_last_seen_at !== null &&
      isWithinAge(health.realtime_last_seen_at, health.freshness_policy.realtime_max_age_seconds)
    : realtime.connected &&
      realtime.connectedAt !== null &&
      realtime.lastSignalAt !== null &&
      isWithinAge(realtime.lastSignalAt, health.freshness_policy.realtime_max_age_seconds);
  if (
    health.overall_state !== "fresh" ||
    !realtimeHealthy ||
    !isWithinAge(snapshot.generated_at, health.freshness_policy.snapshot_max_age_seconds) ||
    !isWithinAge(health.as_of, health.freshness_policy.snapshot_max_age_seconds) ||
    health.worker_heartbeat_at === null ||
    !isWithinAge(health.worker_heartbeat_at, health.freshness_policy.worker_heartbeat_max_age_seconds) ||
    health.components.some((component) => component.state !== "fresh")
  ) {
    return false;
  }
  const componentNames = new Set(health.components.map((component) => component.component));
  if (!componentNames.has("control_plane") || !componentNames.has("worker")) {
    return false;
  }
  return health.components.every((component) =>
    isWithinAge(
      component.observed_at,
      component.component === "worker"
        ? health.freshness_policy.worker_heartbeat_max_age_seconds
        : component.component === "realtime"
          ? health.freshness_policy.realtime_max_age_seconds
          : health.freshness_policy.snapshot_max_age_seconds
    )
  );
}

function isControlPlaneSnapshotCurrent(
  snapshot: OperationsSnapshot,
  now: Date,
  realtime: ClientRealtimeHealth | null
): boolean {
  const health = snapshot.runtime_health;
  const nowMs = now.getTime();
  const maxFutureSkewMs = 30_000;
  const isWithinAge = (timestamp: string, maxAgeSeconds: number) => {
    const ageMs = nowMs - Date.parse(timestamp);
    return Number.isFinite(ageMs) && ageMs >= -maxFutureSkewMs && ageMs <= maxAgeSeconds * 1_000;
  };
  const realtimeHealthy = realtime === null
    ? health.realtime_connected &&
      health.realtime_last_seen_at !== null &&
      isWithinAge(health.realtime_last_seen_at, health.freshness_policy.realtime_max_age_seconds)
    : realtime.connected &&
      realtime.connectedAt !== null &&
      realtime.lastSignalAt !== null &&
      isWithinAge(realtime.lastSignalAt, health.freshness_policy.realtime_max_age_seconds);
  const controlPlane = health.components.find((component) => component.component === "control_plane");
  return (
    health.overall_state !== "contract_error" &&
    health.overall_state !== "session_expired" &&
    realtimeHealthy &&
    isWithinAge(snapshot.generated_at, health.freshness_policy.snapshot_max_age_seconds) &&
    isWithinAge(health.as_of, health.freshness_policy.snapshot_max_age_seconds) &&
    controlPlane?.state === "fresh" &&
    isWithinAge(controlPlane.observed_at, health.freshness_policy.snapshot_max_age_seconds)
  );
}

function pendingWorkerAckCount(snapshot: OperationsSnapshot): number {
  return snapshot.commands.filter((command) =>
    command.state === "approved" ||
    command.state === "claimed" ||
    (command.state === "applied" && !isCommandPostconditionVerified(command, snapshot))
  ).length;
}

function isSelfReview(snapshot: OperationsSnapshot, command: OperationCommandReceipt): boolean {
  return snapshot.access.actor?.actor_id === command.requested_by.actor_id;
}

function isTimestampFresh(timestamp: string, now: Date, maxAgeSeconds: number): boolean {
  const ageMs = now.getTime() - Date.parse(timestamp);
  return Number.isFinite(ageMs) && ageMs >= -30_000 && ageMs <= maxAgeSeconds * 1_000;
}
