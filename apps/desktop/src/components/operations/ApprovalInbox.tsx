import { useRef, useState } from "react";
import { CheckCircle2, UserRoundX, XCircle } from "lucide-react";
import type { OperationCommandReceipt, OperationsSnapshot } from "../../lib/operationsContracts";
import { formatKst } from "../../lib/formatters";
import { requiredReviewerRole } from "../../lib/operationRequests";
import { reviewConfirmationGuard } from "../../lib/operationGuards";
import type { ConfirmAction } from "../../lib/uiState";
import { ConfirmDialog } from "../DialogSurface";
import { EmptyState, Panel, Pill, SectionTitle, pageButtonClass } from "../ui";
import { commandLabel } from "./SafetyCommandCenter";

type ReviewDecision = "approve" | "reject";

export function ApprovalInbox({
  snapshot,
  mutationsAllowed,
  pending,
  onReview
}: {
  readonly snapshot: OperationsSnapshot;
  readonly mutationsAllowed: boolean;
  readonly pending: boolean;
  readonly onReview: (command: OperationCommandReceipt, decision: "approve" | "reject", openGuard: string) => void;
}) {
  const [confirmation, setConfirmation] = useState<ConfirmAction<ReviewDecision> | null>(null);
  const confirmationGuardRef = useRef<string | null>(null);
  const [confirmationError, setConfirmationError] = useState<string | null>(null);
  const canReview = snapshot.access.permissions.includes("review_command");

  const closeConfirmation = () => {
    confirmationGuardRef.current = null;
    setConfirmation(null);
    setConfirmationError(null);
  };

  const openConfirmation = (command: OperationCommandReceipt, decision: ReviewDecision) => {
    confirmationGuardRef.current = reviewConfirmationGuard(snapshot, command);
    setConfirmationError(null);
    setConfirmation({
      kind: decision,
      entityId: command.command_id,
      label: commandLabel(command.command_type),
      tone: decision === "approve" ? "primary" : "neutral"
    });
  };

  const submitReview = () => {
    if (confirmation === null) {
      return;
    }

    const currentCommand = snapshot.pending_reviews.find(
      (command) => command.command_id === confirmation.entityId
    );
    if (
      currentCommand === undefined ||
      currentCommand.state !== "requested" ||
      currentCommand.control_plane_receipt.state !== "requested"
    ) {
      setConfirmationError("상태가 변경됨 — 목록을 새로 확인한 뒤 다시 검토해 주세요.");
      return;
    }

    const blockedReason = reviewBlockedReason({
      snapshot,
      command: currentCommand,
      mutationsAllowed,
      pending
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

    onReview(currentCommand, confirmation.kind, openGuard);
    closeConfirmation();
  };

  return (
    <Panel>
      <SectionTitle
        title="승인 대기함"
        detail={<Pill tone={snapshot.pending_reviews.length > 0 ? "warning" : "safe"}>{snapshot.pending_reviews.length}건</Pill>}
      />
      <p className="mb-4 text-sm text-muted">
        요청자와 승인자를 분리합니다. 승인 기록만으로 Worker 적용이 완료되지는 않습니다.
      </p>
      <p className="mb-4 text-xs text-muted">
        승인·거절은 확인 시점의 최신 요청에만 5분·1회용 추가 확인을 발급해 즉시 전송합니다.
      </p>
      {snapshot.pending_reviews.length === 0 ? (
        <EmptyState title="대기 중인 승인이 없습니다" detail="새 요청은 서버 상태에 반영된 뒤 표시됩니다." />
      ) : (
        <div className="space-y-3">
          {snapshot.pending_reviews.map((command) => {
            const blockedReason = reviewBlockedReason({ snapshot, command, mutationsAllowed, pending });
            const disabled = blockedReason !== null;
            const blockedReasonId = `review-blocked-${command.command_id}`;
            return (
              <article key={command.command_id} className="rounded-lg border border-lineSubtle p-4">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <p className="font-semibold text-ink">{commandLabel(command.command_type)}</p>
                    <p className="mt-1 text-xs text-muted">
                      요청자 {command.requested_by.display_name} · 요청 {formatKst(command.requested_at)} · 만료 {formatKst(command.expires_at)}
                    </p>
                  </div>
                  <Pill tone="warning">제어 버전 {command.control_plane_receipt.revision}</Pill>
                </div>
                {blockedReason !== null ? (
                  <p
                    id={blockedReasonId}
                    className="mt-3 flex items-center gap-2 rounded-xl bg-warningSoft p-3 text-sm text-warning"
                  >
                    <UserRoundX size={16} aria-hidden="true" className="shrink-0" />
                    {blockedReason}
                  </p>
                ) : null}
                <div className="mt-3 flex flex-wrap gap-2">
                  <button
                    type="button"
                    className={`${pageButtonClass("safe")} min-h-11`}
                    disabled={disabled}
                    aria-describedby={disabled ? blockedReasonId : undefined}
                    onClick={() => openConfirmation(command, "approve")}
                  >
                    <CheckCircle2 size={16} aria-hidden="true" />
                    승인
                  </button>
                  <button
                    type="button"
                    className={`${pageButtonClass()} min-h-11`}
                    disabled={disabled}
                    aria-describedby={disabled ? blockedReasonId : undefined}
                    onClick={() => openConfirmation(command, "reject")}
                  >
                    <XCircle size={16} aria-hidden="true" />
                    거절
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      )}
      {!canReview ? <p className="mt-3 text-sm text-warning">현재 역할에는 독립 검토 권한이 없습니다.</p> : null}
      <ConfirmDialog
        open={confirmation !== null}
        title={confirmation?.kind === "approve" ? "운영 요청 승인" : "운영 요청 거절"}
        description={
          confirmation === null
            ? undefined
            : `${confirmation.label} 요청의 최신 상태와 역할 분리를 다시 확인한 뒤 전송합니다.`
        }
        confirmLabel={confirmation?.kind === "approve" ? "승인 전송" : "거절 전송"}
        pendingLabel="검토 전송 중"
        tone={confirmation?.tone ?? "neutral"}
        pending={pending}
        error={confirmationError}
        onConfirm={submitReview}
        onCancel={closeConfirmation}
      />
    </Panel>
  );
}

function reviewBlockedReason({
  snapshot,
  command,
  mutationsAllowed,
  pending
}: {
  readonly snapshot: OperationsSnapshot;
  readonly command: OperationCommandReceipt;
  readonly mutationsAllowed: boolean;
  readonly pending: boolean;
}): string | null {
  if (pending) {
    return "다른 운영 요청을 처리하고 있습니다.";
  }
  if (!mutationsAllowed) {
    return "현재 연결·인증 또는 데이터 최신성 조건에서 검토를 전송할 수 없습니다.";
  }
  if (!snapshot.access.permissions.includes("review_command")) {
    return "현재 역할에는 독립 검토 권한이 없습니다.";
  }
  if (command.state !== "requested" || command.control_plane_receipt.state !== "requested") {
    return "요청 상태가 변경되어 다시 검토해야 합니다.";
  }

  const actorId = snapshot.access.actor?.actor_id ?? null;
  if (actorId !== null && command.requested_by.actor_id === actorId) {
    return "본인이 요청한 작업은 승인하거나 거절할 수 없습니다.";
  }

  const actorRoles = snapshot.access.actor?.roles ?? [];
  if (actorRoles.includes("platform_admin")) {
    return "플랫폼 관리자는 거래 운영 요청을 검토할 수 없습니다.";
  }

  const reviewerRole = command.command_type === "emergency_stop" ? null : requiredReviewerRole(command.command_type);
  if (reviewerRole === null || !actorRoles.includes(reviewerRole)) {
    return "이 요청에 필요한 독립 검토 역할이 없습니다.";
  }

  return null;
}
