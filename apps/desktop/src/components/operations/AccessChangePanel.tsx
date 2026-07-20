import { type FormEvent, useRef, useState } from "react";
import { CheckCircle2, ShieldCheck, UserCog, XCircle } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  attachAccessStepUpGrantToRequestDraft,
  attachAccessStepUpGrantToReviewDraft,
  buildAccessChangeRequestDraft,
  buildAccessChangeReviewDraft,
  buildAccessStepUpGrantDraftRequest,
  sameUuidIdentity
} from "../../lib/accessChangeRequests";
import type { OperationIdFactory } from "../../lib/operationRequests";
import { secureOperationId } from "../../lib/operationRequests";
import type { OperationsRole, OperationsSnapshot } from "../../lib/operationsContracts";
import {
  accessChangeDataApi,
  operationsDataApi,
  operationsErrorMessage,
  operationsSnapshotQueryKey
} from "../../lib/operationsData";
import type { AccessChangeDataApi, OperationsDataApi } from "../../lib/operationsData";
import { useOnlineStatus } from "../../lib/useOnlineStatus";
import { useControlPlaneRealtimeHealth } from "../../lib/controlPlaneRealtime";
import type { ClientRealtimeHealth } from "../../lib/controlPlaneRealtime";
import { useOptionalOperationsSnapshot } from "../../lib/operationsSnapshotContext";
import { formatKst } from "../../lib/formatters";
import { operationsRoleLabels } from "../../lib/presentation";
import { captureAuthSessionEpoch, isAuthSessionEpochCurrent } from "../../lib/authSessionCache";
import { ConfirmDialog } from "../DialogSurface";
import { ErrorState, LoadingState, Panel, Pill, SectionTitle, pageButtonClass } from "../ui";

const roles: readonly OperationsRole[] = [
  "platform_admin",
  "operator",
  "risk_approver",
  "strategy_reviewer",
  "auditor",
  "release_manager",
  "viewer"
];

type ReviewDecision = "approve" | "reject";

interface RequestConfirmation {
  readonly kind: "request";
  readonly entityId: string;
  readonly label: string;
}

interface ReviewConfirmation {
  readonly kind: "review";
  readonly entityId: string;
  readonly decision: ReviewDecision;
  readonly label: string;
}

type AccessConfirmation = RequestConfirmation | ReviewConfirmation;

interface ConfirmationGuard {
  readonly snapshotFingerprint: string;
  readonly online: boolean;
  readonly realtimeFingerprint: string;
}

interface AccessChangePanelProps {
  readonly dataApi?: AccessChangeDataApi;
  readonly snapshotApi?: OperationsDataApi;
  readonly onlineOverride?: boolean;
  readonly nowFactory?: () => Date;
  readonly idFactory?: OperationIdFactory;
}

interface AccessChangePanelContentProps {
  readonly dataApi: AccessChangeDataApi;
  readonly snapshotApi: OperationsDataApi;
  readonly snapshot: OperationsSnapshot | undefined;
  readonly snapshotLoading: boolean;
  readonly snapshotError: unknown;
  readonly isOnline: boolean;
  readonly clientRealtime: ClientRealtimeHealth | null;
  readonly readDeviceOnline: () => boolean;
  readonly nowFactory: () => Date;
  readonly idFactory: OperationIdFactory;
}

/**
 * Production consumes the app-wide snapshot provider. Passing snapshotApi or
 * onlineOverride intentionally selects the standalone path retained for
 * focused tests and story fixtures.
 */
export function AccessChangePanel(props: AccessChangePanelProps = {}) {
  const sharedSnapshot = useOptionalOperationsSnapshot();
  const hasTestOverride = props.snapshotApi !== undefined || props.onlineOverride !== undefined;

  if (sharedSnapshot !== null && !hasTestOverride) {
    return (
      <AccessChangePanelContent
        dataApi={props.dataApi ?? accessChangeDataApi}
        snapshotApi={sharedSnapshot.dataApi}
        snapshot={sharedSnapshot.snapshot}
        snapshotLoading={sharedSnapshot.isLoading}
        snapshotError={sharedSnapshot.error}
        isOnline={sharedSnapshot.isOnline}
        clientRealtime={sharedSnapshot.realtime}
        readDeviceOnline={() => sharedSnapshot.isOnline && browserReportsOnline()}
        nowFactory={props.nowFactory ?? defaultNowFactory}
        idFactory={props.idFactory ?? secureOperationId}
      />
    );
  }

  return <StandaloneAccessChangePanel {...props} />;
}

function StandaloneAccessChangePanel({
  dataApi = accessChangeDataApi,
  snapshotApi = operationsDataApi,
  onlineOverride,
  nowFactory = defaultNowFactory,
  idFactory = secureOperationId
}: AccessChangePanelProps) {
  const isOnline = useOnlineStatus(onlineOverride);
  const clientRealtime = useControlPlaneRealtimeHealth();
  const snapshotQuery = useQuery({
    queryKey: operationsSnapshotQueryKey,
    queryFn: snapshotApi.fetchSnapshot,
    retry: false,
    refetchInterval: 15_000
  });

  return (
    <AccessChangePanelContent
      dataApi={dataApi}
      snapshotApi={snapshotApi}
      snapshot={snapshotQuery.data}
      snapshotLoading={snapshotQuery.isLoading}
      snapshotError={snapshotQuery.error}
      isOnline={isOnline}
      clientRealtime={clientRealtime}
      readDeviceOnline={() => onlineOverride ?? (isOnline && browserReportsOnline())}
      nowFactory={nowFactory}
      idFactory={idFactory}
    />
  );
}

function AccessChangePanelContent({
  dataApi,
  snapshotApi,
  snapshot,
  snapshotLoading,
  snapshotError,
  isOnline,
  clientRealtime,
  readDeviceOnline,
  nowFactory,
  idFactory
}: AccessChangePanelContentProps) {
  const queryClient = useQueryClient();
  const [subjectUserId, setSubjectUserId] = useState("");
  const [evidenceId, setEvidenceId] = useState("");
  const [requestedRole, setRequestedRole] = useState<OperationsRole>("viewer");
  const [changeType, setChangeType] = useState<"grant" | "revoke">("grant");
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmation, setConfirmation] = useState<AccessConfirmation | null>(null);
  const [confirmationError, setConfirmationError] = useState<string | null>(null);
  const confirmationGuardRef = useRef<ConfirmationGuard | null>(null);
  const onlineRef = useRef(isOnline);
  const realtimeRef = useRef(clientRealtime);
  const readDeviceOnlineRef = useRef(readDeviceOnline);
  onlineRef.current = isOnline;
  realtimeRef.current = clientRealtime;
  readDeviceOnlineRef.current = readDeviceOnline;

  const requestMutation = useMutation({
    mutationFn: async (action: RequestConfirmation) => {
      const sessionEpoch = captureAuthSessionEpoch();
      const firstSnapshot = await fetchRevalidatedSnapshot({
        action,
        expectedGuard: confirmationGuardRef.current,
        snapshotApi,
        readOnline: () => onlineRef.current && readDeviceOnlineRef.current(),
        readRealtime: () => realtimeRef.current,
        nowFactory
      });
      assertAccessSessionCurrent(sessionEpoch);
      const draft = buildAccessChangeRequestDraft({
        snapshot: firstSnapshot,
        subjectUserId: action.entityId,
        requestedRole,
        changeType,
        evidenceId,
        now: nowFactory(),
        idFactory
      });
      if (draft === null) {
        throw new AccessChangeBlockedError();
      }

      const firstGuard = createConfirmationGuard(
        firstSnapshot,
        action,
        onlineRef.current && readDeviceOnlineRef.current(),
        realtimeRef.current
      );
      assertAccessSessionCurrent(sessionEpoch);
      const grant = await dataApi.issueStepUpGrant(buildAccessStepUpGrantDraftRequest("request", draft));
      assertAccessSessionCurrent(sessionEpoch);
      await fetchRevalidatedSnapshot({
        action,
        expectedGuard: firstGuard,
        snapshotApi,
        readOnline: () => onlineRef.current && readDeviceOnlineRef.current(),
        readRealtime: () => realtimeRef.current,
        nowFactory
      });
      assertAccessSessionCurrent(sessionEpoch);
      return dataApi.requestChange(attachAccessStepUpGrantToRequestDraft(draft, grant));
    },
    retry: false,
    networkMode: "always",
    onSuccess: async () => {
      setSubjectUserId("");
      setEvidenceId("");
      setNotice("권한 변경 요청이 접수되었습니다. 다른 플랫폼 관리자의 독립 검토 전에는 적용되지 않습니다.");
      await queryClient.invalidateQueries({ queryKey: operationsSnapshotQueryKey, exact: true });
    }
  });

  const reviewMutation = useMutation({
    mutationFn: async (action: ReviewConfirmation) => {
      const sessionEpoch = captureAuthSessionEpoch();
      const firstSnapshot = await fetchRevalidatedSnapshot({
        action,
        expectedGuard: confirmationGuardRef.current,
        snapshotApi,
        readOnline: () => onlineRef.current && readDeviceOnlineRef.current(),
        readRealtime: () => realtimeRef.current,
        nowFactory
      });
      assertAccessSessionCurrent(sessionEpoch);
      const receipt = firstSnapshot.access_changes.find((item) => item.request_id === action.entityId);
      if (receipt === undefined) {
        throw new AccessStateChangedError();
      }
      const draft = buildAccessChangeReviewDraft({
        snapshot: firstSnapshot,
        receipt,
        decision: action.decision,
        now: nowFactory(),
        idFactory
      });
      if (draft === null) {
        throw new AccessChangeBlockedError();
      }

      const firstGuard = createConfirmationGuard(
        firstSnapshot,
        action,
        onlineRef.current && readDeviceOnlineRef.current(),
        realtimeRef.current
      );
      assertAccessSessionCurrent(sessionEpoch);
      const grant = await dataApi.issueStepUpGrant(buildAccessStepUpGrantDraftRequest("review", draft));
      assertAccessSessionCurrent(sessionEpoch);
      await fetchRevalidatedSnapshot({
        action,
        expectedGuard: firstGuard,
        snapshotApi,
        readOnline: () => onlineRef.current && readDeviceOnlineRef.current(),
        readRealtime: () => realtimeRef.current,
        nowFactory
      });
      assertAccessSessionCurrent(sessionEpoch);
      return dataApi.reviewChange(attachAccessStepUpGrantToReviewDraft(draft, grant));
    },
    retry: false,
    networkMode: "always",
    onSuccess: async (receipt) => {
      setNotice(
        receipt.state === "applied"
          ? "독립 검토와 서버 적용 영수증이 확인되었습니다."
          : "권한 변경 거절 영수증이 확인되었습니다."
      );
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: operationsSnapshotQueryKey, exact: true }),
        queryClient.invalidateQueries({ queryKey: ["auth_role"] })
      ]);
    }
  });

  if (snapshotLoading) {
    return <LoadingState label="접근권한 상태를 불러오는 중" />;
  }
  if (snapshotError || snapshot === undefined) {
    return (
      <Panel className="lg:col-span-2">
        <SectionTitle title="접근권한 변경" detail={<Pill tone="danger">변경 차단</Pill>} />
        <ErrorState message="접근권한 읽기 모델 또는 승인 요청 경로를 확인하지 못해 권한 변경을 차단했습니다." />
      </Panel>
    );
  }

  const actor = snapshot.access.actor;
  const platformAdmin = actor?.roles.includes("platform_admin") === true;
  if (!platformAdmin) {
    return (
      <Panel className="lg:col-span-2">
        <SectionTitle title="접근권한" detail={<Pill tone="neutral">읽기 전용</Pill>} />
        <div className="flex items-start gap-3 rounded-lg border border-line bg-canvas p-4">
          <ShieldCheck className="mt-0.5 shrink-0 text-mutedStrong" size={20} aria-hidden="true" />
          <div className="min-w-0">
            <p className="font-semibold text-ink">현재 역할</p>
            <p className="mt-1 break-words text-sm text-mutedStrong">
              {actor?.roles.map((role) => operationsRoleLabels[role]).join(" · ") || "확인 불가"}
            </p>
            <p className="mt-2 text-sm text-mutedStrong">
              역할 부여·회수 요청과 승인 목록은 플랫폼 관리자에게만 제공됩니다.
            </p>
          </div>
        </div>
      </Panel>
    );
  }

  const mutationsAllowed = canManageAccess(snapshot, isOnline, nowFactory(), clientRealtime);
  const mutationBlockedReason = accessMutationBlockedReason(snapshot, isOnline, nowFactory(), clientRealtime);
  const pending = requestMutation.isPending || reviewMutation.isPending;
  const pendingReceipts = snapshot.access_changes.filter((item) => item.state === "requested");
  const selfChangeRequested = subjectUserId !== "" && sameUuidIdentity(actor.actor_id, subjectUserId);
  const requestBlocked = pending || !mutationsAllowed || !subjectUserId || !evidenceId || selfChangeRequested;
  const requestBlockedReason = pending
    ? "다른 접근권한 작업을 처리하고 있습니다."
    : selfChangeRequested
      ? "본인의 역할은 변경 요청할 수 없습니다."
      : mutationBlockedReason;

  const closeConfirmation = () => {
    if (pending) {
      return;
    }
    confirmationGuardRef.current = null;
    setConfirmation(null);
    setConfirmationError(null);
  };

  const openConfirmation = (nextConfirmation: AccessConfirmation) => {
    confirmationGuardRef.current = createConfirmationGuard(
      snapshot,
      nextConfirmation,
      readDeviceOnlineRef.current(),
      realtimeRef.current
    );
    setConfirmationError(null);
    setConfirmation(nextConfirmation);
  };

  const submitRequest = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (requestBlocked) {
      return;
    }
    openConfirmation({
      kind: "request",
      entityId: subjectUserId,
      label: `${operationsRoleLabels[requestedRole]} ${changeType === "grant" ? "부여" : "회수"}`
    });
  };

  const submitConfirmedAction = async () => {
    if (confirmation === null) {
      return;
    }
    setConfirmationError(null);
    try {
      if (confirmation.kind === "request") {
        await requestMutation.mutateAsync(confirmation);
      } else {
        await reviewMutation.mutateAsync(confirmation);
      }
      confirmationGuardRef.current = null;
      setConfirmation(null);
    } catch (error) {
      setConfirmationError(accessChangeErrorMessage(error));
    }
  };

  return (
    <Panel className="lg:col-span-2">
      <SectionTitle
        title="접근권한 변경"
        detail={<Pill tone={mutationsAllowed ? "safe" : "warning"}>{mutationsAllowed ? "요청 가능" : "현재 차단"}</Pill>}
      />
      <p className="text-sm text-mutedStrong">
        요청자·대상자·검토자를 분리하고, 작업마다 5분 동안 한 번만 쓸 수 있는 추가 확인을 거칩니다.
        2단계 인증만으로 권한이 변경되지는 않습니다.
      </p>
      {requestBlockedReason ? (
        <p id="access-request-blocked" className="mt-3 rounded-lg bg-warningSoft p-3 text-sm text-warning">
          {requestBlockedReason}
        </p>
      ) : null}
      <form className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-5" onSubmit={submitRequest}>
        <Field label="대상 사용자 UUID">
          <input
            className={inputClass}
            value={subjectUserId}
            onChange={(event) => setSubjectUserId(event.currentTarget.value)}
            required
            pattern="[0-9a-fA-F-]{36}"
            autoComplete="off"
          />
        </Field>
        <Field label="근거 UUID">
          <input
            className={inputClass}
            value={evidenceId}
            onChange={(event) => setEvidenceId(event.currentTarget.value)}
            required
            pattern="[0-9a-fA-F-]{36}"
            autoComplete="off"
          />
        </Field>
        <Field label="역할">
          <select className={inputClass} value={requestedRole} onChange={(event) => setRequestedRole(event.currentTarget.value as OperationsRole)}>
            {roles.map((role) => <option key={role} value={role}>{operationsRoleLabels[role]}</option>)}
          </select>
        </Field>
        <Field label="변경">
          <select className={inputClass} value={changeType} onChange={(event) => setChangeType(event.currentTarget.value as "grant" | "revoke")}>
            <option value="grant">부여</option>
            <option value="revoke">회수</option>
          </select>
        </Field>
        <div className="flex items-end">
          <button
            className={`${pageButtonClass("safe")} w-full`}
            type="submit"
            disabled={requestBlocked}
            aria-describedby={requestBlockedReason ? "access-request-blocked" : undefined}
          >
            <UserCog size={17} aria-hidden="true" />
            변경 요청
          </button>
        </div>
      </form>

      <div className="mt-7 border-t border-line pt-6">
        <SectionTitle title="권한 변경 승인 대기함" detail={<Pill tone="warning">{pendingReceipts.length}건</Pill>} />
        <div className="space-y-3">
          {pendingReceipts.length === 0 ? (
            <p className="rounded-lg border border-dashed border-line p-4 text-sm text-mutedStrong">검토할 권한 변경 요청이 없습니다.</p>
          ) : pendingReceipts.map((receipt) => {
            const blockedReason = accessReviewBlockedReason({
              snapshot,
              receiptId: receipt.request_id,
              isOnline,
              clientRealtime,
              now: nowFactory(),
              pending
            });
            const disabled = blockedReason !== null;
            const blockedReasonId = `access-review-blocked-${receipt.request_id}`;
            return (
              <article key={receipt.request_id} className="rounded-lg border border-line p-4">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0">
                    <p className="font-semibold text-ink">
                      {receipt.change_type === "grant" ? "부여" : "회수"} · {operationsRoleLabels[receipt.requested_role]}
                    </p>
                    <p className="mt-1 break-all text-xs text-mutedStrong">
                      대상 {receipt.subject_user_id} · 요청자 {receipt.requested_by.display_name} · 만료 {formatKst(receipt.expires_at)}
                    </p>
                  </div>
                  <Pill tone={Date.parse(receipt.expires_at) <= nowFactory().getTime() ? "danger" : "warning"}>
                    {Date.parse(receipt.expires_at) <= nowFactory().getTime() ? "만료" : "독립 검토 필요"}
                  </Pill>
                </div>
                {blockedReason ? (
                  <p id={blockedReasonId} className="mt-3 rounded-lg bg-warningSoft p-3 text-sm text-warning">
                    {blockedReason}
                  </p>
                ) : null}
                <div className="mt-3 flex flex-wrap gap-2">
                  <button
                    className={pageButtonClass("safe")}
                    type="button"
                    disabled={disabled}
                    aria-describedby={disabled ? blockedReasonId : undefined}
                    onClick={() => openConfirmation({
                      kind: "review",
                      entityId: receipt.request_id,
                      decision: "approve",
                      label: operationsRoleLabels[receipt.requested_role]
                    })}
                  >
                    <CheckCircle2 size={16} aria-hidden="true" /> 승인
                  </button>
                  <button
                    className={pageButtonClass("neutral")}
                    type="button"
                    disabled={disabled}
                    aria-describedby={disabled ? blockedReasonId : undefined}
                    onClick={() => openConfirmation({
                      kind: "review",
                      entityId: receipt.request_id,
                      decision: "reject",
                      label: operationsRoleLabels[receipt.requested_role]
                    })}
                  >
                    <XCircle size={16} aria-hidden="true" /> 거절
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      </div>
      {notice ? (
        <p className="mt-4 rounded-lg border border-primary/25 bg-primarySoft p-3 text-sm text-primary" role="status" aria-live="polite">
          {notice}
        </p>
      ) : null}
      <ConfirmDialog
        open={confirmation !== null}
        title={confirmationTitle(confirmation)}
        description={confirmationDescription(confirmation)}
        confirmLabel={confirmationConfirmLabel(confirmation)}
        pendingLabel="최신 상태 확인 중"
        tone={confirmation?.kind === "review" && confirmation.decision === "reject" ? "neutral" : "primary"}
        pending={pending}
        error={confirmationError}
        onConfirm={submitConfirmedAction}
        onCancel={closeConfirmation}
      />
    </Panel>
  );
}

function Field({ label, children }: { readonly label: string; readonly children: React.ReactNode }) {
  return <label className="grid gap-1.5 text-sm font-medium"><span className="text-mutedStrong">{label}</span>{children}</label>;
}

async function fetchRevalidatedSnapshot({
  action,
  expectedGuard,
  snapshotApi,
  readOnline,
  readRealtime,
  nowFactory
}: {
  readonly action: AccessConfirmation;
  readonly expectedGuard: ConfirmationGuard | null;
  readonly snapshotApi: OperationsDataApi;
  readonly readOnline: () => boolean;
  readonly readRealtime: () => ClientRealtimeHealth | null;
  readonly nowFactory: () => Date;
}): Promise<OperationsSnapshot> {
  const onlineBeforeFetch = readOnline();
  if (!onlineBeforeFetch) {
    throw new AccessStateChangedError();
  }

  const latestSnapshot = await snapshotApi.fetchSnapshot();
  const onlineAfterFetch = readOnline();
  const realtimeAfterFetch = readRealtime();
  if (!canManageAccess(latestSnapshot, onlineAfterFetch, nowFactory(), realtimeAfterFetch)) {
    throw new AccessStateChangedError();
  }

  const currentGuard = createConfirmationGuard(latestSnapshot, action, onlineAfterFetch, realtimeAfterFetch);
  if (expectedGuard === null || !sameConfirmationGuard(expectedGuard, currentGuard)) {
    throw new AccessStateChangedError();
  }
  return latestSnapshot;
}

function createConfirmationGuard(
  snapshot: OperationsSnapshot,
  action: AccessConfirmation,
  online: boolean,
  realtime: ClientRealtimeHealth | null
): ConfirmationGuard {
  return {
    snapshotFingerprint: accessSnapshotFingerprint(snapshot, action),
    online,
    realtimeFingerprint: JSON.stringify({
      connected: realtime?.connected ?? null,
      connectedAt: realtime?.connectedAt ?? null,
      lastSignalAt: realtime?.lastSignalAt ?? null
    })
  };
}

function sameConfirmationGuard(left: ConfirmationGuard, right: ConfirmationGuard): boolean {
  return left.online === right.online &&
    left.realtimeFingerprint === right.realtimeFingerprint &&
    left.snapshotFingerprint === right.snapshotFingerprint;
}

function accessSnapshotFingerprint(snapshot: OperationsSnapshot, action: AccessConfirmation): string {
  const controlPlane = snapshot.runtime_health.components.find((component) => component.component === "control_plane");
  const receipt = action.kind === "review"
    ? snapshot.access_changes.find((item) => item.request_id === action.entityId) ?? null
    : null;
  return JSON.stringify({
    access: {
      signedIn: snapshot.access.signed_in,
      sessionState: snapshot.access.session_state,
      assuranceLevel: snapshot.access.assurance_level,
      actor: snapshot.access.actor,
      permissions: snapshot.access.permissions
    },
    stateVersion: snapshot.runtime_health.state_version,
    controlPlane: controlPlane === undefined
      ? null
      : {
          state: controlPlane.state,
          observedAt: controlPlane.observed_at,
          detailCode: controlPlane.detail_code
        },
    realtimeConnected: snapshot.runtime_health.realtime_connected,
    receipt
  });
}

function accessReviewBlockedReason({
  snapshot,
  receiptId,
  isOnline,
  clientRealtime,
  now,
  pending
}: {
  readonly snapshot: OperationsSnapshot;
  readonly receiptId: string;
  readonly isOnline: boolean;
  readonly clientRealtime: ClientRealtimeHealth | null;
  readonly now: Date;
  readonly pending: boolean;
}): string | null {
  if (pending) {
    return "다른 접근권한 작업을 처리하고 있습니다.";
  }
  const globalReason = accessMutationBlockedReason(snapshot, isOnline, now, clientRealtime);
  if (globalReason !== null) {
    return globalReason;
  }
  const receipt = snapshot.access_changes.find((item) => item.request_id === receiptId);
  if (receipt === undefined || receipt.state !== "requested") {
    return "요청 상태가 변경되어 다시 확인해야 합니다.";
  }
  if (Date.parse(receipt.expires_at) <= now.getTime()) {
    return "검토 기한이 지나 새 요청이 필요합니다.";
  }
  const actorId = snapshot.access.actor?.actor_id ?? null;
  if (actorId !== null && (
    sameUuidIdentity(actorId, receipt.requested_by.actor_id) ||
    sameUuidIdentity(actorId, receipt.subject_user_id)
  )) {
    return "요청자 또는 변경 대상 본인은 이 요청을 검토할 수 없습니다.";
  }
  return null;
}

function canManageAccess(
  snapshot: OperationsSnapshot,
  online: boolean,
  now: Date,
  realtime: ClientRealtimeHealth | null
): boolean {
  return accessMutationBlockedReason(snapshot, online, now, realtime) === null;
}

function accessMutationBlockedReason(
  snapshot: OperationsSnapshot,
  online: boolean,
  now: Date,
  realtime: ClientRealtimeHealth | null
): string | null {
  if (!online) {
    return "기기가 오프라인이라 권한 요청을 전송할 수 없습니다.";
  }
  if (!snapshot.access.signed_in || snapshot.access.session_state !== "active") {
    return "운영 세션이 만료되었거나 확인되지 않았습니다.";
  }
  if (snapshot.access.assurance_level !== "aal2") {
    return "2단계 인증을 완료한 뒤 다시 시도해 주세요.";
  }
  if (snapshot.access.actor?.roles.includes("platform_admin") !== true) {
    return "플랫폼 관리자 역할이 없어 접근권한 작업을 수행할 수 없습니다.";
  }

  const controlPlane = snapshot.runtime_health.components.find((component) => component.component === "control_plane");
  if (controlPlane?.state !== "fresh") {
    return "제어면 상태가 최신이 아니어서 권한 요청을 전송할 수 없습니다.";
  }

  const snapshotMaxAge = snapshot.runtime_health.freshness_policy.snapshot_max_age_seconds * 1_000;
  const snapshotAge = now.getTime() - Date.parse(snapshot.generated_at);
  if (!Number.isFinite(snapshotAge) || snapshotAge < -30_000 || snapshotAge > snapshotMaxAge) {
    return "권한 상태 기준 시각이 오래되어 새로고침이 필요합니다.";
  }

  const serverRealtimeLastSeenAt = snapshot.runtime_health.realtime_last_seen_at;
  const realtimeMaxAge = snapshot.runtime_health.freshness_policy.realtime_max_age_seconds * 1_000;
  if (realtime === null) {
    const serverRealtimeAge = serverRealtimeLastSeenAt === null
      ? Number.POSITIVE_INFINITY
      : now.getTime() - Date.parse(serverRealtimeLastSeenAt);
    if (!snapshot.runtime_health.realtime_connected ||
      serverRealtimeLastSeenAt === null ||
      !Number.isFinite(serverRealtimeAge) ||
      serverRealtimeAge < -30_000 ||
      serverRealtimeAge > realtimeMaxAge) {
      return "실시간 제어 신호가 최신이 아니어서 권한 요청을 전송할 수 없습니다.";
    }
    return null;
  }

  const signalAge = realtime.lastSignalAt === null
    ? Number.POSITIVE_INFINITY
    : now.getTime() - Date.parse(realtime.lastSignalAt);
  if (!realtime.connected || realtime.connectedAt === null || realtime.lastSignalAt === null ||
    !Number.isFinite(signalAge) || signalAge < -30_000 || signalAge > realtimeMaxAge) {
    return "실시간 제어 신호가 최신이 아니어서 권한 요청을 전송할 수 없습니다.";
  }
  return null;
}

function confirmationTitle(confirmation: AccessConfirmation | null): string {
  if (confirmation === null || confirmation.kind === "request") {
    return "접근권한 변경 요청";
  }
  return confirmation.decision === "approve" ? "접근권한 변경 승인" : "접근권한 변경 거절";
}

function confirmationDescription(confirmation: AccessConfirmation | null): string | undefined {
  if (confirmation === null) {
    return undefined;
  }
  if (confirmation.kind === "request") {
    return `${confirmation.label} 요청을 보냅니다. 확인 시 최신 연결·세션·역할 상태를 다시 검사합니다.`;
  }
  return `${confirmation.label} 요청을 ${confirmation.decision === "approve" ? "승인" : "거절"}합니다. 요청자·대상자 분리와 최신 상태를 다시 검사합니다.`;
}

function confirmationConfirmLabel(confirmation: AccessConfirmation | null): string {
  if (confirmation === null || confirmation.kind === "request") {
    return "요청 전송";
  }
  return confirmation.decision === "approve" ? "승인 전송" : "거절 전송";
}

function accessChangeErrorMessage(error: unknown): string {
  if (error instanceof AccessStateChangedError) {
    return "상태가 변경됨 — 최신 연결·세션·역할 또는 요청 상태를 다시 검토해 주세요. 추가 확인은 폐기되었고 변경 요청은 전송하지 않았습니다.";
  }
  if (error instanceof AccessChangeBlockedError) {
    return "2단계 인증, 플랫폼 관리자, 역할 분리 또는 최신 제어면 조건이 충족되지 않아 전송하지 않았습니다.";
  }
  return operationsErrorMessage(error);
}

function defaultNowFactory(): Date {
  return new Date();
}

function browserReportsOnline(): boolean {
  return typeof navigator === "undefined" ? true : navigator.onLine;
}

function assertAccessSessionCurrent(epoch: number): void {
  if (!isAuthSessionEpochCurrent(epoch)) {
    throw new AccessStateChangedError();
  }
}

class AccessChangeBlockedError extends Error {}
class AccessStateChangedError extends Error {}

const inputClass = "min-h-control w-full rounded-md border border-controlLine bg-surface px-3 py-2 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-primary";
