import { Suspense, lazy, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import type { Incident, OperationCommandReceipt, OperationsSnapshot } from "../lib/operationsContracts";
import type { UnknownResolutionEvidenceInput } from "../lib/unknownResolutionRequests";
import type { OperationIdFactory, OperationCommandType } from "../lib/operationRequests";
import {
  operationsDataApi,
  operationsErrorMessage,
  operationsSnapshotQueryKey,
  unknownResolutionDataApi,
  unknownResolutionSnapshotQueryKey
} from "../lib/operationsData";
import type { OperationsDataApi, UnknownResolutionDataApi } from "../lib/operationsData";
import { useOnlineStatus } from "../lib/useOnlineStatus";
import { useControlPlaneRealtimeHealth } from "../lib/controlPlaneRealtime";
import type { ClientRealtimeHealth } from "../lib/controlPlaneRealtime";
import type { OperationsSnapshotContextValue } from "../lib/operationsSnapshotContext";
import { AttentionQueue } from "../components/operations/AttentionQueue";
import {
  commandLabel,
  isCommandPostconditionVerified,
  SafetyCommandCenter
} from "../components/operations/SafetyCommandCenter";
import { StaleDataBoundary } from "../components/operations/StaleDataBoundary";
import { ReadonlyReveal } from "../components/ReadonlyReveal";
import { KeyValue, LoadingState, Pill } from "../components/ui";
import { LazySurfaceBoundary } from "../components/LazySurfaceBoundary";
import type { DrawerState } from "../lib/uiState";
import { formatKst } from "../lib/formatters";
import { operationStateLabel, workerStateLabel } from "../lib/presentation";
import {
  commandConfirmationGuard,
  incidentConfirmationGuard,
  operationsStateGuard,
  reviewConfirmationGuard,
  unknownConfirmationGuard
} from "../lib/operationGuards";
import { captureAuthSessionEpoch, isAuthSessionEpochCurrent } from "../lib/authSessionCache";

const ApprovalInbox = lazy(async () => ({
  default: (await import("../components/operations/ApprovalInbox")).ApprovalInbox
}));
const IncidentCenter = lazy(async () => ({
  default: (await import("../components/operations/IncidentCenter")).IncidentCenter
}));
const ManualReconciliationCase = lazy(async () => ({
  default: (await import("../components/operations/ManualReconciliationCase")).ManualReconciliationCase
}));
const MfaSecurityPanel = lazy(async () => ({
  default: (await import("../components/operations/MfaSecurityPanel")).MfaSecurityPanel
}));
const DrawerSurface = lazy(async () => ({
  default: (await import("../components/DialogSurface")).DrawerSurface
}));

const secureOperationId: OperationIdFactory = () => crypto.randomUUID();

export interface OperationsPageProps {
  readonly dataApi?: OperationsDataApi;
  readonly unknownDataApi?: UnknownResolutionDataApi;
  readonly onlineOverride?: boolean;
  readonly idFactory?: OperationIdFactory;
  readonly nowFactory?: () => Date;
  readonly snapshotSource?: OperationsSnapshotContextValue;
}

export function OperationsPage(props: OperationsPageProps = {}) {
  if (props.snapshotSource) {
    return (
      <OperationsPageContent
        dataApi={props.dataApi ?? props.snapshotSource.dataApi}
        unknownDataApi={props.unknownDataApi}
        idFactory={props.idFactory}
        nowFactory={props.nowFactory}
        snapshotQuery={props.snapshotSource.query}
        isOnline={props.onlineOverride ?? props.snapshotSource.isOnline}
        clientRealtime={props.snapshotSource.realtime}
      />
    );
  }
  return <StandaloneOperationsPage {...props} />;
}

function StandaloneOperationsPage({
  dataApi = operationsDataApi,
  unknownDataApi = unknownResolutionDataApi,
  onlineOverride,
  idFactory = secureOperationId,
  nowFactory = () => new Date()
}: OperationsPageProps = {}) {
  const isOnline = useOnlineStatus(onlineOverride);
  const clientRealtime = useControlPlaneRealtimeHealth();
  const snapshotQuery = useQuery({
    queryKey: operationsSnapshotQueryKey,
    queryFn: dataApi.fetchSnapshot,
    retry: false,
    refetchInterval: 15_000
  });

  return (
    <OperationsPageContent
      dataApi={dataApi}
      unknownDataApi={unknownDataApi}
      idFactory={idFactory}
      nowFactory={nowFactory}
      snapshotQuery={snapshotQuery}
      isOnline={isOnline}
      clientRealtime={clientRealtime}
    />
  );
}

function OperationsPageContent({
  dataApi,
  unknownDataApi = unknownResolutionDataApi,
  idFactory = secureOperationId,
  nowFactory = () => new Date(),
  snapshotQuery,
  isOnline,
  clientRealtime
}: {
  readonly dataApi: OperationsDataApi;
  readonly unknownDataApi?: UnknownResolutionDataApi;
  readonly idFactory?: OperationIdFactory;
  readonly nowFactory?: () => Date;
  readonly snapshotQuery: UseQueryResult<OperationsSnapshot, Error>;
  readonly isOnline: boolean;
  readonly clientRealtime: ClientRealtimeHealth | null;
}) {
  const queryClient = useQueryClient();
  const [notice, setNotice] = useState<string | null>(null);
  const [drawer, setDrawer] = useState<DrawerState | null>(null);
  const unknownSnapshotQuery = useQuery({
    queryKey: unknownResolutionSnapshotQueryKey,
    queryFn: unknownDataApi.fetchSnapshot,
    retry: false,
    refetchInterval: 15_000
  });
  const onlineRef = useRef(isOnline);
  const realtimeRef = useRef(clientRealtime);
  onlineRef.current = isOnline;
  realtimeRef.current = clientRealtime;

  const requestMutation = useMutation({
    mutationFn: async ({ commandType, openGuard }: { readonly commandType: OperationCommandType; readonly openGuard: string }) => {
      const sessionEpoch = captureAuthSessionEpoch();
      const requests = await import("../lib/operationRequests");
      assertMutationSessionCurrent(sessionEpoch);
      const before = await dataApi.fetchSnapshot();
      assertMutationSessionCurrent(sessionEpoch);
      assertCommandMutationCurrent(before, onlineRef.current, nowFactory(), realtimeRef.current);
      if (commandConfirmationGuard(before, commandType) !== openGuard) {
        throw new SafetyStateChangedError();
      }
      if (commandType === "emergency_stop") {
        const request = requests.buildEmergencyStopCommandRequest({ snapshot: before, now: nowFactory(), idFactory });
        if (request === null || !onlineRef.current) {
          throw new SafetyStateChangedError();
        }
        assertMutationSessionCurrent(sessionEpoch);
        return dataApi.requestCommand(request);
      }
      const draft = requests.buildOperationCommandDraft({ commandType, snapshot: before, now: nowFactory(), idFactory });
      if (draft === null) {
        throw new SafetyStateChangedError();
      }
      const fingerprint = operationsStateGuard(before);
      assertMutationSessionCurrent(sessionEpoch);
      const grant = await dataApi.issueStepUpGrant(
        requests.buildStepUpGrantDraftRequest("request", draft)
      );
      assertMutationSessionCurrent(sessionEpoch);
      const after = await dataApi.fetchSnapshot();
      assertMutationSessionCurrent(sessionEpoch);
      assertCommandMutationCurrent(after, onlineRef.current, nowFactory(), realtimeRef.current);
      if (operationsStateGuard(after) !== fingerprint) {
        throw new SafetyStateChangedError();
      }
      assertMutationSessionCurrent(sessionEpoch);
      return dataApi.requestCommand(requests.attachStepUpGrantToCommandDraft(draft, grant));
    },
    retry: false,
    networkMode: "always",
    onSuccess: () => {
      setNotice("작업별 추가 확인과 요청 접수가 기록되었습니다. Worker 적용 확인 전에는 완료가 아닙니다.");
      return queryClient.invalidateQueries({ queryKey: operationsSnapshotQueryKey });
    },
    onError: (error) => setNotice(mutationErrorMessage(error))
  });
  const reviewMutation = useMutation({
    mutationFn: async ({ commandId, decision, openGuard }: { readonly commandId: string; readonly decision: "approve" | "reject"; readonly openGuard: string }) => {
      const sessionEpoch = captureAuthSessionEpoch();
      const requests = await import("../lib/operationRequests");
      assertMutationSessionCurrent(sessionEpoch);
      const before = await dataApi.fetchSnapshot();
      assertMutationSessionCurrent(sessionEpoch);
      assertCommandMutationCurrent(before, onlineRef.current, nowFactory(), realtimeRef.current);
      const command = before.pending_reviews.find((item) => item.command_id === commandId);
      if (!command || isSelfReview(before, command)) {
        throw new SafetyStateChangedError();
      }
      if (reviewConfirmationGuard(before, command) !== openGuard) {
        throw new SafetyStateChangedError();
      }
      const draft = requests.buildCommandReviewDraft({ command, access: before.access, decision, now: nowFactory(), idFactory });
      if (draft === null) {
        throw new SafetyStateChangedError();
      }
      const fingerprint = reviewConfirmationGuard(before, command);
      assertMutationSessionCurrent(sessionEpoch);
      const grant = await dataApi.issueStepUpGrant(requests.buildStepUpGrantDraftRequest("review", draft));
      assertMutationSessionCurrent(sessionEpoch);
      const after = await dataApi.fetchSnapshot();
      assertMutationSessionCurrent(sessionEpoch);
      assertCommandMutationCurrent(after, onlineRef.current, nowFactory(), realtimeRef.current);
      const current = after.pending_reviews.find((item) => item.command_id === commandId);
      if (!current || reviewConfirmationGuard(after, current) !== fingerprint) {
        throw new SafetyStateChangedError();
      }
      assertMutationSessionCurrent(sessionEpoch);
      return dataApi.reviewCommand(requests.attachStepUpGrantToReviewDraft(draft, grant));
    },
    retry: false,
    networkMode: "always",
    onSuccess: () => {
      setNotice("승인 결과가 기록되었습니다. Worker 적용 여부는 별도 상태에서 확인하세요.");
      return queryClient.invalidateQueries({ queryKey: operationsSnapshotQueryKey });
    },
    onError: (error) => setNotice(mutationErrorMessage(error))
  });
  const incidentMutation = useMutation({
    mutationFn: async ({ incidentId, action, openGuard }: { readonly incidentId: string; readonly action: "acknowledge" | "resolve"; readonly openGuard: string }) => {
      const sessionEpoch = captureAuthSessionEpoch();
      const requests = await import("../lib/operationRequests");
      assertMutationSessionCurrent(sessionEpoch);
      const latest = await dataApi.fetchSnapshot();
      assertMutationSessionCurrent(sessionEpoch);
      if (!canMutateIncidentOperations(latest, onlineRef.current, nowFactory(), realtimeRef.current)) {
        throw new SafetyStateChangedError();
      }
      const incident = latest.incidents.find((item) => item.incident_id === incidentId);
      const expectedStatus = action === "acknowledge" ? "open" : "acknowledged";
      if (
        !incident ||
        incident.status !== expectedStatus ||
        incidentConfirmationGuard(latest, incident, action) !== openGuard ||
        !canPerformIncidentAction(latest, incident, action) ||
        !onlineRef.current
      ) {
        throw new SafetyStateChangedError();
      }
      assertMutationSessionCurrent(sessionEpoch);
      return dataApi.actOnIncident(requests.buildIncidentActionRequest({ incident, action, now: nowFactory(), idFactory }));
    },
    retry: false,
    networkMode: "always",
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: operationsSnapshotQueryKey });
      setNotice("사고 상태 변경이 감사 가능한 제어면 기록으로 반영되었습니다.");
    },
    onError: (error) => setNotice(mutationErrorMessage(error))
  });
  const unknownRequestMutation = useMutation({
    mutationFn: async ({ breakId, evidence, openGuard }: { readonly breakId: string; readonly evidence: UnknownResolutionEvidenceInput; readonly openGuard: string }) => {
      const sessionEpoch = captureAuthSessionEpoch();
      const requests = await import("../lib/unknownResolutionRequests");
      assertMutationSessionCurrent(sessionEpoch);
      const [before, unknownBefore] = await Promise.all([dataApi.fetchSnapshot(), unknownDataApi.fetchSnapshot()]);
      assertMutationSessionCurrent(sessionEpoch);
      assertCommandMutationCurrent(before, onlineRef.current, nowFactory(), realtimeRef.current);
      const context = unknownBefore.cases.find((item) => item.break_id === breakId);
      if (
        !context ||
        unknownConfirmationGuard(before, context) !== openGuard ||
        !isTimestampFresh(unknownBefore.generated_at, nowFactory(), before.runtime_health.freshness_policy.snapshot_max_age_seconds)
      ) {
        throw new SafetyStateChangedError();
      }
      const draft = requests.buildUnknownResolutionRequestDraft({ context, access: before.access, evidence, now: nowFactory(), idFactory });
      if (draft === null) {
        throw new SafetyStateChangedError();
      }
      const fingerprint = unknownConfirmationGuard(before, context);
      assertMutationSessionCurrent(sessionEpoch);
      const grant = await unknownDataApi.issueStepUpGrant(
        requests.buildUnknownResolutionStepUpRequest("request", draft)
      );
      assertMutationSessionCurrent(sessionEpoch);
      const [after, unknownAfter] = await Promise.all([dataApi.fetchSnapshot(), unknownDataApi.fetchSnapshot()]);
      assertMutationSessionCurrent(sessionEpoch);
      assertCommandMutationCurrent(after, onlineRef.current, nowFactory(), realtimeRef.current);
      const current = unknownAfter.cases.find((item) => item.break_id === breakId);
      if (!current || unknownConfirmationGuard(after, current) !== fingerprint) {
        throw new SafetyStateChangedError();
      }
      assertMutationSessionCurrent(sessionEpoch);
      return unknownDataApi.requestResolution(
        requests.attachUnknownResolutionRequestGrant(draft, grant)
      );
    },
    retry: false,
    networkMode: "always",
    onSuccess: () => {
      setNotice("수동 대사 요청과 증거 요약이 저장되었습니다. 독립 승인, Worker 적용, 회계 반영 전에는 완료가 아닙니다.");
      void queryClient.invalidateQueries({ queryKey: operationsSnapshotQueryKey });
      return queryClient.invalidateQueries({ queryKey: unknownResolutionSnapshotQueryKey });
    },
    onError: (error) => setNotice(mutationErrorMessage(error))
  });
  const unknownReviewMutation = useMutation({
    mutationFn: async ({ breakId, decision, openGuard }: { readonly breakId: string; readonly decision: "approve" | "reject"; readonly openGuard: string }) => {
      const sessionEpoch = captureAuthSessionEpoch();
      const requests = await import("../lib/unknownResolutionRequests");
      assertMutationSessionCurrent(sessionEpoch);
      const [before, unknownBefore] = await Promise.all([dataApi.fetchSnapshot(), unknownDataApi.fetchSnapshot()]);
      assertMutationSessionCurrent(sessionEpoch);
      assertCommandMutationCurrent(before, onlineRef.current, nowFactory(), realtimeRef.current);
      const context = unknownBefore.cases.find((item) => item.break_id === breakId);
      if (
        !context ||
        unknownConfirmationGuard(before, context) !== openGuard ||
        !isTimestampFresh(unknownBefore.generated_at, nowFactory(), before.runtime_health.freshness_policy.snapshot_max_age_seconds)
      ) {
        throw new SafetyStateChangedError();
      }
      const draft = requests.buildUnknownResolutionReviewDraft({ context, access: before.access, decision, now: nowFactory(), idFactory });
      if (draft === null) {
        throw new SafetyStateChangedError();
      }
      const fingerprint = unknownConfirmationGuard(before, context);
      assertMutationSessionCurrent(sessionEpoch);
      const grant = await unknownDataApi.issueStepUpGrant(
        requests.buildUnknownResolutionStepUpRequest("review", draft)
      );
      assertMutationSessionCurrent(sessionEpoch);
      const [after, unknownAfter] = await Promise.all([dataApi.fetchSnapshot(), unknownDataApi.fetchSnapshot()]);
      assertMutationSessionCurrent(sessionEpoch);
      assertCommandMutationCurrent(after, onlineRef.current, nowFactory(), realtimeRef.current);
      const current = unknownAfter.cases.find((item) => item.break_id === breakId);
      if (!current || unknownConfirmationGuard(after, current) !== fingerprint) {
        throw new SafetyStateChangedError();
      }
      assertMutationSessionCurrent(sessionEpoch);
      return unknownDataApi.reviewResolution(
        requests.attachUnknownResolutionReviewGrant(draft, grant)
      );
    },
    retry: false,
    networkMode: "always",
    onSuccess: () => {
      setNotice("수동 대사 검토 결과가 저장되었습니다. Worker 적용과 최신 회계 반영을 계속 확인하세요.");
      void queryClient.invalidateQueries({ queryKey: operationsSnapshotQueryKey });
      return queryClient.invalidateQueries({ queryKey: unknownResolutionSnapshotQueryKey });
    },
    onError: (error) => setNotice(mutationErrorMessage(error))
  });
  const resetUnknownRequestMutation = unknownRequestMutation.reset;
  const resetUnknownReviewMutation = unknownReviewMutation.reset;
  const unknownRequestStatus = unknownRequestMutation.status;
  const unknownReviewStatus = unknownReviewMutation.status;

  useEffect(() => {
    if (snapshotQuery.data?.access.session_state !== "active") {
      setDrawer(null);
      resetUnknownRequestMutation();
      resetUnknownReviewMutation();
    }
  }, [resetUnknownRequestMutation, resetUnknownReviewMutation, snapshotQuery.data?.access.session_state]);

  useEffect(() => {
    if (unknownRequestStatus === "success" || unknownRequestStatus === "error") {
      resetUnknownRequestMutation();
    }
    if (unknownReviewStatus === "success" || unknownReviewStatus === "error") {
      resetUnknownReviewMutation();
    }
  }, [resetUnknownRequestMutation, resetUnknownReviewMutation, unknownRequestStatus, unknownReviewStatus]);

  if (snapshotQuery.isLoading) {
    return <LoadingState label="최신 운영 상태를 불러오는 중" />;
  }
  if (snapshotQuery.error || !snapshotQuery.data) {
    return (
      <div className="space-y-3">
        <div className="rounded-lg border border-danger/30 bg-dangerSoft p-4" role="alert">
          <div className="font-semibold text-danger">운영 데이터 확인 실패</div>
          <p className="mt-2 text-sm text-danger">{operationsErrorMessage(snapshotQuery.error)} 불완전한 값은 정상으로 추정하지 않습니다.</p>
          <details className="mt-3 text-sm text-danger">
            <summary className="min-h-control cursor-pointer py-2 font-semibold">연결 상세</summary>
            <p>운영 상태 전체를 확인할 수 없어 변경 작업을 안전하게 차단했습니다.</p>
          </details>
        </div>
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
    setNotice("현재 상태에서는 변경할 수 없습니다. 최신 상태와 유효한 온라인 세션을 먼저 확인하세요.");
  };

  return (
    <LazySurfaceBoundary
      title="운영 상세 화면을 안전하게 열지 못했습니다"
      detail="화면을 다시 불러오기 전까지 운영 변경 기능은 차단됩니다."
      logCode="operations_lazy_surface_load_failed"
    >
      <StaleDataBoundary health={snapshot.runtime_health} isOnline={isOnline} clientFresh={clientFresh}>
      {notice ? (
        <div className="rounded-md border border-sky-200 bg-sky-50 p-3 text-sm text-sky-900" role="status" aria-live="polite">
          {notice}
        </div>
      ) : null}

      <div className="grid min-w-0 gap-5 xl:grid-cols-12">
        <div className="min-w-0 xl:col-span-7">
          <SafetyCommandCenter
            snapshot={snapshot}
            mutationsAllowed={mutationsAllowed}
            pending={pending}
            onRequest={(commandType, openGuard) => {
              if (!mutationsAllowed) {
                rejectBlockedMutation();
                return;
              }
              requestMutation.mutate({ commandType, openGuard });
            }}
          />
        </div>
        <div className="min-w-0 xl:col-span-5">
          <AttentionQueue
            snapshot={snapshot}
            unknownSnapshot={unknownSnapshot}
            isOnline={isOnline}
            now={snapshotEvaluationTime}
            onOpen={setDrawer}
          />
        </div>
      </div>

      <ReadonlyReveal>
        <details className="rounded-xl border border-line bg-surface px-5 py-2">
          <summary className="flex min-h-control cursor-pointer items-center justify-between gap-3 font-semibold">
            <span>최근 기록</span>
            <span className="text-xs font-normal text-mutedStrong">
              명령 {snapshot.commands.length} · 승인 {snapshot.pending_reviews.length} · 사고 {snapshot.incidents.length}
            </span>
          </summary>
          <div className="grid gap-3 border-t border-line py-4 md:grid-cols-3">
            <RecordShortcut label="명령 기록" detail={`${snapshot.commands.length}건 · 최신 상태 확인 포함`} onClick={() => setDrawer({ kind: "command" })} />
            <RecordShortcut label="승인 기록" detail={`${snapshot.pending_reviews.length}건 · 독립 검토`} onClick={() => setDrawer({ kind: "approval" })} />
            <RecordShortcut label="사고 기록" detail={`${snapshot.incidents.length}건 · 확인 기한 포함`} onClick={() => setDrawer({ kind: "incident" })} />
          </div>
        </details>
      </ReadonlyReveal>

      <Suspense fallback={<LoadingState label="상세 화면을 불러오는 중" />}>
        {drawer?.kind === "command" ? (
          <DrawerSurface open readOnly title="명령 상세" description="요청·검토·Worker 적용·최신 실행 상태를 분리해 확인합니다." onRequestClose={() => setDrawer(null)}>
            <CommandDrawerContent snapshot={snapshot} commandId={drawer.entityId} />
          </DrawerSurface>
        ) : null}
        {drawer?.kind === "approval" ? (
          <DrawerSurface open readOnly={false} title="승인 상세" description="중요한 만료와 요청자·검토자 분리 조건은 접지 않습니다." onRequestClose={() => setDrawer(null)}>
            <Suspense fallback={<LoadingState label="승인 상세를 불러오는 중" />}>
              <ApprovalInbox
                snapshot={snapshot}
                mutationsAllowed={mutationsAllowed}
                pending={pending}
                onReview={(command, decision, openGuard) => {
                  if (!mutationsAllowed || isSelfReview(snapshot, command)) {
                    rejectBlockedMutation();
                    return;
                  }
                  reviewMutation.mutate({ commandId: command.command_id, decision, openGuard });
                }}
              />
            </Suspense>
          </DrawerSurface>
        ) : null}
        {drawer?.kind === "incident" ? (
          <DrawerSurface open readOnly={false} title="사고 상세" description="Worker가 오프라인이어도 최신 제어면과 2단계 인증이 확인되면 사고 확인은 가능합니다." onRequestClose={() => setDrawer(null)}>
            <Suspense fallback={<LoadingState label="사고 상세를 불러오는 중" />}>
              <IncidentCenter
                snapshot={snapshot}
                mutationsAllowed={incidentMutationsAllowed}
                pending={pending}
                onAction={(incident, action, openGuard) => {
                  if (!incidentMutationsAllowed) {
                    rejectBlockedMutation();
                    return;
                  }
                  incidentMutation.mutate({ incidentId: incident.incident_id, action, openGuard });
                }}
              />
            </Suspense>
          </DrawerSurface>
        ) : null}
        {drawer?.kind === "reconciliation" ? (
          <DrawerSurface open readOnly={false} title="수동 대사 상세" description="원시 증거는 현재 인증 세션의 메모리에만 유지되며 상세 화면을 닫으면 제거됩니다." onRequestClose={() => setDrawer(null)}>
            <Suspense fallback={<LoadingState label="수동 대사 상세를 불러오는 중" />}>
              <ManualReconciliationCase
                snapshot={snapshot}
                unknownSnapshot={unknownSnapshot}
                mutationsAllowed={unknownMutationsAllowed}
                pending={pending}
                now={snapshotEvaluationTime}
                onRequest={(context, evidence, openGuard) => {
                  if (!unknownMutationsAllowed) {
                    rejectBlockedMutation();
                    return;
                  }
                  unknownRequestMutation.mutate({ breakId: context.break_id, evidence, openGuard });
                }}
                onReview={(context, decision, openGuard) => {
                  if (!unknownMutationsAllowed) {
                    rejectBlockedMutation();
                    return;
                  }
                  unknownReviewMutation.mutate({ breakId: context.break_id, decision, openGuard });
                }}
              />
            </Suspense>
          </DrawerSurface>
        ) : null}
        {drawer?.kind === "mfa" ? (
          <DrawerSurface open readOnly={false} title="2단계 인증 관리" onRequestClose={() => setDrawer(null)}>
            <Suspense fallback={<LoadingState label="2단계 인증 관리를 불러오는 중" />}>
              <MfaSecurityPanel />
            </Suspense>
          </DrawerSurface>
        ) : null}
      </Suspense>
      </StaleDataBoundary>
    </LazySurfaceBoundary>
  );
}

function RecordShortcut({ label, detail, onClick }: { readonly label: string; readonly detail: string; readonly onClick: () => void }) {
  return (
    <button type="button" className="min-h-control rounded-lg border border-line bg-canvas p-4 text-left transition-[transform,opacity] duration-press active:scale-[0.99]" onClick={onClick}>
      <span className="block font-semibold text-ink">{label}</span>
      <span className="mt-1 block text-xs text-mutedStrong">{detail}</span>
    </button>
  );
}

function CommandDrawerContent({ snapshot, commandId }: { readonly snapshot: OperationsSnapshot; readonly commandId?: string }) {
  const commands = commandId
    ? snapshot.commands.filter((command) => command.command_id === commandId)
    : snapshot.commands;
  if (commands.length === 0) {
    return <p className="rounded-lg border border-dashed border-line p-4 text-sm text-mutedStrong">표시할 명령 기록이 없습니다.</p>;
  }
  return (
    <div className="space-y-3">
      {commands.map((command) => {
        const complete = isCommandPostconditionVerified(command, snapshot);
        return (
          <article key={command.command_id} className="rounded-lg border border-line bg-surface p-4">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div className="min-w-0">
                <h3 className="font-semibold">{commandLabel(command.command_type)}</h3>
                <p className="mt-1 break-all text-xs text-mutedStrong">{command.command_id}</p>
              </div>
              <Pill tone={complete ? "safe" : ["failed", "rejected"].includes(command.state) ? "danger" : "warning"}>
                {complete ? "최신 상태 확인 완료" : operationStateLabel(command.state)}
              </Pill>
            </div>
            <div className="mt-3">
              <KeyValue label="요청 시각" value={formatKst(command.requested_at)} />
              <KeyValue label="만료 시각" value={formatKst(command.expires_at)} />
              <KeyValue label="제어면 버전" value={`r${command.control_plane_receipt.revision}`} />
              <KeyValue label="Worker 적용 확인" value={workerStateLabel(command.worker_ack?.state)} />
            </div>
            {!complete && command.state === "applied" ? (
              <p className="mt-3 rounded-lg bg-warningSoft p-3 text-sm text-warning">최신 실행 상태의 결과 조건이 확인될 때까지 완료로 표시하지 않습니다.</p>
            ) : null}
          </article>
        );
      })}
    </div>
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

function isSelfReview(snapshot: OperationsSnapshot, command: OperationCommandReceipt): boolean {
  return snapshot.access.actor?.actor_id === command.requested_by.actor_id;
}

function isTimestampFresh(timestamp: string, now: Date, maxAgeSeconds: number): boolean {
  const ageMs = now.getTime() - Date.parse(timestamp);
  return Number.isFinite(ageMs) && ageMs >= -30_000 && ageMs <= maxAgeSeconds * 1_000;
}

function assertCommandMutationCurrent(
  snapshot: OperationsSnapshot,
  online: boolean,
  now: Date,
  realtime: ClientRealtimeHealth | null
): void {
  if (!canMutateOperations(snapshot, online, now, realtime)) {
    throw new SafetyStateChangedError();
  }
}

function assertMutationSessionCurrent(epoch: number): void {
  if (!isAuthSessionEpochCurrent(epoch)) {
    throw new SafetyStateChangedError();
  }
}

export function canPerformIncidentAction(
  snapshot: OperationsSnapshot,
  incident: Incident,
  action: "acknowledge" | "resolve"
): boolean {
  const actor = snapshot.access.actor;
  if (actor === null) return false;
  if (action === "acknowledge") {
    return incident.status === "open" &&
      snapshot.access.permissions.includes("acknowledge_incident") &&
      actor.roles.includes("operator");
  }
  return incident.status === "acknowledged" &&
    snapshot.access.permissions.includes("resolve_incident") &&
    actor.roles.includes("risk_approver") &&
    incident.owner !== null &&
    incident.owner.actor_id !== actor.actor_id;
}

function mutationErrorMessage(error: unknown): string {
  if (error instanceof SafetyStateChangedError) {
    return "상태가 변경됨 — 최신 상태를 다시 검토하세요. 발급된 확인 정보는 폐기했고 요청은 전송하지 않았습니다.";
  }
  return operationsErrorMessage(error);
}

class SafetyStateChangedError extends Error {}
