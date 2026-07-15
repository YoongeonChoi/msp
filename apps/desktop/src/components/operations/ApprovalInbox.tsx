import { CheckCircle2, UserRoundX, XCircle } from "lucide-react";
import type { OperationCommandReceipt, OperationsSnapshot } from "../../lib/operationsContracts";
import { formatKst } from "../../lib/formatters";
import { requiredReviewerRole } from "../../lib/operationRequests";
import { EmptyState, Panel, Pill, SectionTitle, pageButtonClass } from "../ui";
import { commandLabel } from "./SafetyCommandCenter";

export function ApprovalInbox({
  snapshot,
  mutationsAllowed,
  pending,
  onReview
}: {
  readonly snapshot: OperationsSnapshot;
  readonly mutationsAllowed: boolean;
  readonly pending: boolean;
  readonly onReview: (command: OperationCommandReceipt, decision: "approve" | "reject") => void;
}) {
  const canReview = snapshot.access.permissions.includes("review_command");
  const actorId = snapshot.access.actor?.actor_id ?? null;
  const actorRoles = snapshot.access.actor?.roles ?? [];
  const platformAdminBlocked = actorRoles.includes("platform_admin");

  return (
    <Panel>
      <SectionTitle
        title="승인 대기함"
        detail={<Pill tone={snapshot.pending_reviews.length > 0 ? "warning" : "safe"}>{snapshot.pending_reviews.length}건</Pill>}
      />
      <p className="mb-4 text-sm text-muted">
        요청자와 승인자를 분리합니다. 승인은 제어면 영수증이며 Worker 적용 완료를 의미하지 않습니다.
      </p>
      <p className="mb-4 text-xs text-muted">
        승인·거절도 확인 시점의 동일한 review draft에 5분·1회용 step-up grant를 발급해 즉시 전송합니다.
      </p>
      {snapshot.pending_reviews.length === 0 ? (
        <EmptyState title="대기 중인 승인이 없습니다" detail="새 요청은 서버 read model에 반영된 뒤 표시됩니다." />
      ) : (
        <div className="space-y-3">
          {snapshot.pending_reviews.map((command) => {
            const selfReview = actorId !== null && command.requested_by.actor_id === actorId;
            const reviewerRole = command.command_type === "emergency_stop" ? null : requiredReviewerRole(command.command_type);
            const roleAllowed = reviewerRole !== null && actorRoles.includes(reviewerRole) && !platformAdminBlocked;
            const disabled =
              pending || !mutationsAllowed || !canReview || selfReview || !roleAllowed;
            return (
              <article key={command.command_id} className="rounded-md border border-line p-3">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <p className="font-semibold text-ink">{commandLabel(command.command_type)}</p>
                    <p className="mt-1 text-xs text-muted">
                      요청자 {command.requested_by.display_name} · 요청 {formatKst(command.requested_at)} · 만료 {formatKst(command.expires_at)}
                    </p>
                  </div>
                  <Pill tone="warning">revision {command.control_plane_receipt.revision}</Pill>
                </div>
                {selfReview ? (
                  <p className="mt-3 flex items-center gap-2 rounded-md bg-amber-50 p-2 text-sm text-amber-900">
                    <UserRoundX size={16} aria-hidden="true" />
                    본인 요청은 승인하거나 거절할 수 없습니다.
                  </p>
                ) : null}
                {!selfReview && !roleAllowed ? (
                  <p className="mt-3 rounded-md bg-amber-50 p-2 text-sm text-amber-900">
                    독립 승인 역할이 필요합니다. platform_admin은 거래 승인을 할 수 없습니다.
                  </p>
                ) : null}
                <div className="mt-3 flex flex-wrap gap-2">
                  <button
                    type="button"
                    className={pageButtonClass("safe")}
                    disabled={disabled}
                    onClick={() => {
                      if (window.confirm("증거와 정책 조건을 확인했으며 이 요청을 승인할까요?")) {
                        onReview(command, "approve");
                      }
                    }}
                  >
                    <CheckCircle2 size={16} aria-hidden="true" />
                    승인
                  </button>
                  <button
                    type="button"
                    className={pageButtonClass("danger")}
                    disabled={disabled}
                    onClick={() => {
                      if (window.confirm("증거 미충족 사유로 이 요청을 거절할까요?")) {
                        onReview(command, "reject");
                      }
                    }}
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
      {!canReview ? <p className="mt-3 text-sm text-amber-800">현재 역할에는 review_command 권한이 없습니다.</p> : null}
    </Panel>
  );
}
