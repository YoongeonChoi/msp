import { type FormEvent, useState } from "react";
import { CheckCircle2, UserCog, UserRoundX, XCircle } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  attachAccessStepUpGrantToRequestDraft,
  attachAccessStepUpGrantToReviewDraft,
  buildAccessChangeRequestDraft,
  buildAccessChangeReviewDraft,
  buildAccessStepUpGrantDraftRequest
} from "../../lib/accessChangeRequests";
import type { OperationIdFactory } from "../../lib/operationRequests";
import { secureOperationId } from "../../lib/operationRequests";
import type { AccessChangeReceipt, OperationsRole, OperationsSnapshot } from "../../lib/operationsContracts";
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
import { formatKst } from "../../lib/formatters";
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

export function AccessChangePanel({
  dataApi = accessChangeDataApi,
  snapshotApi = operationsDataApi,
  onlineOverride,
  nowFactory = () => new Date(),
  idFactory = secureOperationId
}: {
  readonly dataApi?: AccessChangeDataApi;
  readonly snapshotApi?: OperationsDataApi;
  readonly onlineOverride?: boolean;
  readonly nowFactory?: () => Date;
  readonly idFactory?: OperationIdFactory;
} = {}) {
  const queryClient = useQueryClient();
  const isOnline = useOnlineStatus(onlineOverride);
  const clientRealtime = useControlPlaneRealtimeHealth();
  const [subjectUserId, setSubjectUserId] = useState("");
  const [evidenceId, setEvidenceId] = useState("");
  const [requestedRole, setRequestedRole] = useState<OperationsRole>("viewer");
  const [changeType, setChangeType] = useState<"grant" | "revoke">("grant");
  const [notice, setNotice] = useState<string | null>(null);
  const snapshotQuery = useQuery({
    queryKey: operationsSnapshotQueryKey,
    queryFn: snapshotApi.fetchSnapshot,
    retry: false,
    refetchInterval: 15_000
  });
  const snapshot = snapshotQuery.data;

  const requestMutation = useMutation({
    mutationFn: async () => {
      if (snapshot === undefined || !canManageAccess(snapshot, isOnline, nowFactory(), clientRealtime)) {
        throw new AccessChangeBlockedError();
      }
      const draft = buildAccessChangeRequestDraft({
        snapshot,
        subjectUserId,
        requestedRole,
        changeType,
        evidenceId,
        now: nowFactory(),
        idFactory
      });
      if (draft === null) {
        throw new AccessChangeBlockedError();
      }
      const grant = await dataApi.issueStepUpGrant(buildAccessStepUpGrantDraftRequest("request", draft));
      return dataApi.requestChange(attachAccessStepUpGrantToRequestDraft(draft, grant));
    },
    retry: false,
    networkMode: "always",
    onSuccess: async () => {
      setSubjectUserId("");
      setEvidenceId("");
      setNotice("권한 변경 요청이 접수되었습니다. 다른 platform_admin의 독립 검토 전에는 적용되지 않습니다.");
      await queryClient.invalidateQueries({ queryKey: operationsSnapshotQueryKey });
    },
    onError: (error) => setNotice(accessChangeErrorMessage(error))
  });

  const reviewMutation = useMutation({
    mutationFn: async ({ receipt, decision }: { readonly receipt: AccessChangeReceipt; readonly decision: "approve" | "reject" }) => {
      if (snapshot === undefined || !canManageAccess(snapshot, isOnline, nowFactory(), clientRealtime)) {
        throw new AccessChangeBlockedError();
      }
      const draft = buildAccessChangeReviewDraft({ snapshot, receipt, decision, now: nowFactory(), idFactory });
      if (draft === null) {
        throw new AccessChangeBlockedError();
      }
      const grant = await dataApi.issueStepUpGrant(buildAccessStepUpGrantDraftRequest("review", draft));
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
        queryClient.invalidateQueries({ queryKey: operationsSnapshotQueryKey }),
        queryClient.invalidateQueries({ queryKey: ["auth_role"] })
      ]);
    },
    onError: (error) => setNotice(accessChangeErrorMessage(error))
  });

  if (snapshotQuery.isLoading) {
    return <LoadingState label="권한 변경 제어면을 불러오는 중" />;
  }
  if (snapshotQuery.error || snapshot === undefined) {
    return (
      <Panel className="lg:col-span-2">
        <SectionTitle title="Access & MFA 권한 변경" detail={<Pill tone="danger">구현/승인 전 차단</Pill>} />
        <ErrorState message="strict access_changes read model 또는 승인 RPC가 준비되지 않아 권한 변경을 차단했습니다." />
      </Panel>
    );
  }

  const actor = snapshot.access.actor;
  const platformAdmin = actor?.roles.includes("platform_admin") === true;
  const mutationsAllowed = canManageAccess(snapshot, isOnline, nowFactory(), clientRealtime);
  const pending = requestMutation.isPending || reviewMutation.isPending;

  const submitRequest = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!window.confirm("이 권한 변경을 독립 검토 대기 상태로 요청할까요?")) {
      return;
    }
    requestMutation.mutate();
  };

  return (
    <Panel className="lg:col-span-2">
      <SectionTitle
        title="Access & MFA 권한 변경"
        detail={<Pill tone={mutationsAllowed ? "safe" : "warning"}>{mutationsAllowed ? "변경 요청 가능" : "차단"}</Pill>}
      />
      <p className="text-sm text-muted">
        권한 변경은 platform_admin maker/checker 분리와 요청별 5분·1회용 step-up grant를 사용합니다. MFA AAL2 재인증만으로 요청이 승인되지는 않습니다.
      </p>
      {!platformAdmin ? (
        <p className="mt-3 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
          platform_admin 역할이 없어 요청·검토 기능을 차단했습니다.
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
        <Field label="증거 UUID">
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
            {roles.map((role) => <option key={role} value={role}>{role}</option>)}
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
            className={pageButtonClass("safe")}
            type="submit"
            disabled={!mutationsAllowed || pending || !subjectUserId || !evidenceId || actor?.actor_id === subjectUserId}
          >
            <UserCog size={16} aria-hidden="true" />
            변경 요청
          </button>
        </div>
      </form>
      {actor?.actor_id === subjectUserId && subjectUserId ? (
        <p className="mt-2 flex items-center gap-2 text-sm text-amber-900">
          <UserRoundX size={16} aria-hidden="true" /> 본인 권한 변경은 요청할 수 없습니다.
        </p>
      ) : null}

      <div className="mt-6">
        <SectionTitle title="권한 변경 승인 대기함" detail={<Pill tone="warning">{snapshot.access_changes.filter((item) => item.state === "requested").length}건</Pill>} />
        <div className="space-y-3">
          {snapshot.access_changes.filter((item) => item.state === "requested").length === 0 ? (
            <p className="rounded-md border border-dashed border-line p-4 text-sm text-muted">검토할 권한 변경 요청이 없습니다.</p>
          ) : snapshot.access_changes.filter((item) => item.state === "requested").map((receipt) => {
            const selfBlocked = actor?.actor_id === receipt.requested_by.actor_id || actor?.actor_id === receipt.subject_user_id;
            const expired = Date.parse(receipt.expires_at) <= nowFactory().getTime();
            const disabled = !mutationsAllowed || pending || selfBlocked || expired;
            return (
              <article key={receipt.request_id} className="rounded-md border border-line p-3">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <p className="font-semibold text-ink">{receipt.change_type === "grant" ? "부여" : "회수"} · {receipt.requested_role}</p>
                    <p className="mt-1 text-xs text-muted">
                      대상 {receipt.subject_user_id} · 요청자 {receipt.requested_by.display_name} · 만료 {formatKst(receipt.expires_at)}
                    </p>
                  </div>
                  <Pill tone={expired ? "danger" : "warning"}>{expired ? "만료" : "독립 검토 필요"}</Pill>
                </div>
                {selfBlocked ? (
                  <p className="mt-2 text-sm text-amber-900">요청자 또는 변경 대상 본인은 이 요청을 검토할 수 없습니다.</p>
                ) : null}
                <div className="mt-3 flex flex-wrap gap-2">
                  <button className={pageButtonClass("safe")} type="button" disabled={disabled} onClick={() => confirmReview(receipt, "approve", reviewMutation.mutate)}>
                    <CheckCircle2 size={16} aria-hidden="true" /> 승인
                  </button>
                  <button className={pageButtonClass("danger")} type="button" disabled={disabled} onClick={() => confirmReview(receipt, "reject", reviewMutation.mutate)}>
                    <XCircle size={16} aria-hidden="true" /> 거절
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      </div>
      {notice ? <p className="mt-4 rounded-md border border-sky-200 bg-sky-50 p-3 text-sm text-sky-900" role="status" aria-live="polite">{notice}</p> : null}
    </Panel>
  );
}

function Field({ label, children }: { readonly label: string; readonly children: React.ReactNode }) {
  return <label className="grid gap-1 text-sm"><span className="text-muted">{label}</span>{children}</label>;
}

function confirmReview(
  receipt: AccessChangeReceipt,
  decision: "approve" | "reject",
  mutate: (value: { readonly receipt: AccessChangeReceipt; readonly decision: "approve" | "reject" }) => void
) {
  if (window.confirm(`이 권한 변경 요청을 ${decision === "approve" ? "승인" : "거절"}할까요?`)) {
    mutate({ receipt, decision });
  }
}

function canManageAccess(
  snapshot: OperationsSnapshot,
  online: boolean,
  now: Date,
  realtime: ClientRealtimeHealth | null
): boolean {
  const controlPlane = snapshot.runtime_health.components.find((component) => component.component === "control_plane");
  const maxAge = snapshot.runtime_health.freshness_policy.snapshot_max_age_seconds * 1_000;
  const age = now.getTime() - Date.parse(snapshot.generated_at);
  const serverRealtimeLastSeenAt = snapshot.runtime_health.realtime_last_seen_at;
  const serverRealtimeAge = serverRealtimeLastSeenAt === null
    ? Number.POSITIVE_INFINITY
    : now.getTime() - Date.parse(serverRealtimeLastSeenAt);
  const realtimeHealthy = realtime === null
    ? snapshot.runtime_health.realtime_connected &&
      serverRealtimeLastSeenAt !== null &&
      serverRealtimeAge >= -30_000 &&
      serverRealtimeAge <= snapshot.runtime_health.freshness_policy.realtime_max_age_seconds * 1_000
    : realtime.connected &&
      realtime.connectedAt !== null &&
      realtime.lastSignalAt !== null &&
      (() => {
        const signalAge = now.getTime() - Date.parse(realtime.lastSignalAt);
        return Number.isFinite(signalAge) &&
          signalAge >= -30_000 &&
          signalAge <= snapshot.runtime_health.freshness_policy.realtime_max_age_seconds * 1_000;
      })();
  return (
    online &&
    snapshot.access.signed_in &&
    snapshot.access.session_state === "active" &&
    snapshot.access.assurance_level === "aal2" &&
    snapshot.access.actor?.roles.includes("platform_admin") === true &&
    realtimeHealthy &&
    controlPlane?.state === "fresh" &&
    Number.isFinite(age) &&
    age >= -30_000 &&
    age <= maxAge
  );
}

function accessChangeErrorMessage(error: unknown): string {
  if (error instanceof AccessChangeBlockedError) {
    return "AAL2, platform_admin, maker/checker 또는 fresh 제어면 조건이 충족되지 않아 전송하지 않았습니다.";
  }
  return operationsErrorMessage(error);
}

class AccessChangeBlockedError extends Error {}

const inputClass = "w-full rounded-md border border-line px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-slate-400";
