import { useEffect, useRef, useState } from "react";
import { FileSearch } from "lucide-react";
import {
  DataContractError,
  parseDataContract,
  unknownResolutionMissingFillV2Schema
} from "../../lib/operationsContracts";
import type {
  OperationsSnapshot,
  UnknownResolutionContextV2,
  UnknownResolutionMissingFillV2,
  UnknownResolutionSnapshotV2
} from "../../lib/operationsContracts";
import type { UnknownResolutionEvidenceInput } from "../../lib/unknownResolutionRequests";
import {
  canRequestUnknownResolution,
  canReviewUnknownResolution
} from "../../lib/unknownResolutionRequests";
import { formatKst } from "../../lib/formatters";
import { unknownConfirmationGuard } from "../../lib/operationGuards";
import { ConfirmDialog, type ConfirmTone } from "../DialogSurface";
import { EmptyState, Panel, Pill, SectionTitle, pageButtonClass } from "../ui";
import type { Tone } from "../ui";

interface DraftFields {
  readonly evidenceArtifactUri: string;
  readonly evidenceSha256: string;
  readonly evidenceCapturedAt: string;
  readonly terminalStatus: "" | "filled" | "canceled" | "expired" | "rejected";
  readonly missingFillsJson: string;
}

const emptyDraft: DraftFields = {
  evidenceArtifactUri: "",
  evidenceSha256: "",
  evidenceCapturedAt: "",
  terminalStatus: "",
  missingFillsJson: ""
};

type ReconciliationConfirmation =
  | {
      readonly action: "request";
      readonly breakId: string;
      readonly label: string;
      readonly tone: ConfirmTone;
      readonly evidence: UnknownResolutionEvidenceInput;
    }
  | {
      readonly action: "review";
      readonly breakId: string;
      readonly decision: "approve" | "reject";
      readonly label: string;
      readonly tone: ConfirmTone;
    };

export interface ManualReconciliationCaseProps {
  readonly snapshot: OperationsSnapshot;
  readonly unknownSnapshot: UnknownResolutionSnapshotV2 | null;
  readonly mutationsAllowed: boolean;
  readonly pending: boolean;
  readonly now: Date;
  readonly onRequest: (
    context: UnknownResolutionContextV2,
    evidence: UnknownResolutionEvidenceInput,
    openGuard: string
  ) => void;
  readonly onReview: (
    context: UnknownResolutionContextV2,
    decision: "approve" | "reject",
    openGuard: string
  ) => void;
}

export function ManualReconciliationCase({
  snapshot,
  unknownSnapshot,
  mutationsAllowed,
  pending,
  now,
  onRequest,
  onReview
}: ManualReconciliationCaseProps) {
  const [drafts, setDrafts] = useState<Record<string, DraftFields>>({});
  const [validationErrors, setValidationErrors] = useState<Record<string, string>>({});
  const [confirmation, setConfirmation] = useState<ReconciliationConfirmation | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const confirmationGuardRef = useRef<string | null>(null);
  const roles = new Set(snapshot.access.actor?.roles ?? []);
  const canView = roles.has("operator") || roles.has("risk_approver") || roles.has("auditor");
  const cases = unknownSnapshot?.cases ?? [];

  useEffect(() => {
    const activeKeys = new Set((unknownSnapshot?.cases ?? []).map(draftKey));
    setDrafts((current) => Object.fromEntries(Object.entries(current).filter(([key]) => activeKeys.has(key))));
    setValidationErrors((current) => Object.fromEntries(Object.entries(current).filter(([key]) => activeKeys.has(key))));
  }, [unknownSnapshot]);

  const updateDraft = (breakId: string, patch: Partial<DraftFields>) => {
    setDrafts((current) => ({
      ...current,
      [breakId]: { ...(current[breakId] ?? emptyDraft), ...patch }
    }));
    setValidationErrors((current) => ({ ...current, [breakId]: "" }));
  };

  const closeConfirmation = () => {
    confirmationGuardRef.current = null;
    setConfirmation(null);
  };

  const openConfirmation = (
    nextConfirmation: ReconciliationConfirmation,
    context: UnknownResolutionContextV2
  ) => {
    confirmationGuardRef.current = unknownConfirmationGuard(snapshot, context);
    setNotice(null);
    setConfirmation(nextConfirmation);
  };

  const invalidateConfirmation = (reason: string) => {
    const discardedInput = confirmation?.action === "request"
      ? "증거 입력"
      : "검토 확인 정보";
    closeConfirmation();
    setNotice(`상태가 변경됨 — ${reason} ${discardedInput}는 폐기했으며 작업을 전송하지 않았습니다.`);
  };

  const submitConfirmedAction = () => {
    if (confirmation === null) {
      return;
    }

    const currentContext = unknownSnapshot?.cases.find(
      (context) => context.break_id === confirmation.breakId
    );
    if (currentContext === undefined || confirmationGuardRef.current === null) {
      invalidateConfirmation("대사 항목을 최신 목록에서 찾을 수 없습니다.");
      return;
    }

    if (pending || !mutationsAllowed) {
      invalidateConfirmation("현재 연결·인증 또는 데이터 최신성 조건에서 제출할 수 없습니다.");
      return;
    }

    const currentGuard = unknownConfirmationGuard(snapshot, currentContext);
    if (currentGuard !== confirmationGuardRef.current) {
      invalidateConfirmation("항목 버전·상태 또는 현재 역할을 다시 확인해 주세요.");
      return;
    }

    if (confirmation.action === "request") {
      if (!canRequestUnknownResolution(snapshot.access, currentContext)) {
        invalidateConfirmation("현재 항목은 더 이상 회계 조정 요청 조건을 충족하지 않습니다.");
        return;
      }
      const evidence = confirmation.evidence;
      const openGuard = confirmationGuardRef.current;
      const key = draftKey(currentContext);
      setDrafts((current) => {
        const next = { ...current };
        delete next[key];
        return next;
      });
      setValidationErrors((current) => {
        const next = { ...current };
        delete next[key];
        return next;
      });
      closeConfirmation();
      onRequest(currentContext, evidence, openGuard);
      return;
    }

    if (!canReviewUnknownResolution(snapshot.access, currentContext, now)) {
      invalidateConfirmation("검토 기한·역할 분리 또는 최신 상태 조건을 충족하지 않습니다.");
      return;
    }
    const decision = confirmation.decision;
    const openGuard = confirmationGuardRef.current;
    closeConfirmation();
    onReview(currentContext, decision, openGuard);
  };

  return (
    <Panel className="xl:col-span-2">
      <SectionTitle
        title="수동 대사 케이스"
        detail={<Pill tone={cases.length > 0 ? "warning" : "safe"}>{cases.length}건</Pill>}
      />
      <p className="mb-4 text-sm text-muted">
        확인되지 않은 거래 관찰의 불변 해시와 최신 버전을 검토합니다. 브로커 API를 직접 호출하거나 주문 상태를 임의 보정하지 않습니다.
        승인 후에도 Worker 적용 확인과 회계 반영 결과가 모두 확인되기 전에는 완료로 표시하지 않습니다.
      </p>
      {!canView ? (
        <p className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
          운영자, 위험 승인자 또는 감사자 역할이 없어 수동 대사 증거를 표시하지 않습니다.
        </p>
      ) : unknownSnapshot === null ? (
        <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-900" role="alert">
          수동 대사 회계 반영 정보를 확인할 수 없어 요청·승인을 모두 차단했습니다.
        </div>
      ) : cases.length === 0 ? (
        <EmptyState
          title="수동 대사 대상이 없습니다"
          detail="수동 확인이 필요한 거래 관찰만 요청자·검토자 분리 경로에 표시됩니다."
        />
      ) : (
        <div className="space-y-4">
          {cases.map((item) => {
            const key = draftKey(item);
            const draft = drafts[key] ?? emptyDraft;
            const requestEligible = canRequestUnknownResolution(snapshot.access, item);
            const requestAllowed = mutationsAllowed && requestEligible;
            const reviewAllowed = mutationsAllowed && canReviewUnknownResolution(snapshot.access, item, now);
            const selfReview = item.request?.requested_by.actor_id === snapshot.access.actor?.actor_id;
            const localError = validationErrors[key];
            return (
              <article key={item.break_id} className="rounded-md border border-line p-4">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <p className="flex items-center gap-2 font-semibold text-ink">
                      <FileSearch size={17} aria-hidden="true" />
                      {item.symbol} {item.side === "buy" ? "매수" : "매도"} · {item.requested_quantity}주
                    </p>
                    <p className="mt-1 text-xs text-muted">
                      대사 항목 {item.break_id.slice(0, 8)} · 주문 의도 {item.intent_id.slice(0, 8)} · 관찰 {item.unknown_observation.sequence}
                    </p>
                  </div>
                  <Pill tone={caseTone(item)}>{caseStatusLabel(item)}</Pill>
                </div>

                <dl className="mt-3 grid gap-2 text-sm sm:grid-cols-2 lg:grid-cols-4">
                  <EvidenceValue label="환경" value={item.environment === "paper" ? "모의거래" : "계약 테스트"} />
                  <EvidenceValue label="제공자" value={item.provider_identity.broker} />
                  <EvidenceValue label="제공자 주문" value={item.provider_identity.provider_order_id} />
                  <EvidenceValue label="관찰 시각" value={formatKst(item.unknown_observation.observed_at)} />
                  <EvidenceValue label="누적 체결" value={`${item.unknown_observation.cumulative_quantity}주`} />
                  <EvidenceValue label="최신 버전" value={String(item.break_revision)} />
                  <EvidenceValue label="현금 반영 버전" value={String(item.cash_projection_version)} />
                  <EvidenceValue label="보유 수량 반영 버전" value={item.position_projection_version === null ? "없음" : String(item.position_projection_version)} />
                  <EvidenceValue label="예약 기록 순번" value={String(item.reservation_event_sequence)} />
                  <EvidenceValue label="제어 기준 버전" value={String(item.control_epoch)} />
                  <EvidenceValue label="관찰 해시" value={shortHash(item.unknown_observation.observation_sha256)} />
                  <EvidenceValue label="제공자 관찰 해시" value={shortHash(item.unknown_observation.provider_observation_sha256)} />
                </dl>

                <ResolutionTimeline context={item} />

                {item.request ? (
                  <section className="mt-4 rounded-md border border-slate-200 bg-slate-50 p-3" aria-label="제출된 회계 조정 증거">
                    <p className="text-sm font-semibold text-ink">요청자가 고정한 증거·누락 체결</p>
                    <dl className="mt-2 grid gap-2 text-sm sm:grid-cols-2">
                      <EvidenceValue label="요청자" value={item.request.requested_by.display_name} />
                      <EvidenceValue label="최종 주문 상태" value={terminalStatusLabel(item.request.terminal_status)} />
                      <EvidenceValue label="증거 SHA" value={shortHash(item.request.evidence_sha256)} />
                      <EvidenceValue label="요청 요약 해시" value={shortHash(item.request.request_digest_sha256)} />
                    </dl>
                    {item.request.missing_fills.length === 0 ? (
                      <p className="mt-2 rounded border border-slate-200 bg-white p-2 text-sm text-ink">
                        요청자가 누락 체결 없음(<code>[]</code>)을 명시적으로 제출했습니다.
                      </p>
                    ) : (
                      <ol className="mt-2 space-y-2" aria-label="제출된 누락 체결 목록">
                        {item.request.missing_fills.map((fill) => (
                          <li key={fill.fill_sequence} className="rounded border border-slate-200 bg-white p-2 text-sm">
                            #{fill.fill_sequence} · {fill.quantity}주 × {fill.price_krw.toLocaleString("ko-KR")}원 ·
                            체결 ID {fill.provider_execution_id} · 결제일 {fill.settlement_date} ·
                            증거 {shortHash(fill.evidence_sha256)}
                          </li>
                        ))}
                      </ol>
                    )}
                  </section>
                ) : null}

                {requestEligible ? (
                  <fieldset className="mt-4 rounded-md border border-amber-200 bg-amber-50 p-3">
                    <legend className="px-1 text-sm font-semibold text-amber-950">운영자 회계 조정 요청</legend>
                    <p className="mb-3 text-xs text-amber-900">
                      모든 필드를 직접 입력하세요. 누락 체결이 없더라도 <code>[]</code>를 입력해야 하며 자동 기본값은 없습니다.
                    </p>
                    <div className="grid gap-3 sm:grid-cols-2">
                      <LabeledInput
                        label="증거 자료 위치"
                        value={draft.evidenceArtifactUri}
                        placeholder="urn:sha256:<64 hex>"
                        onChange={(value) => updateDraft(key, { evidenceArtifactUri: value })}
                      />
                      <LabeledInput
                        label="증거 SHA-256"
                        value={draft.evidenceSha256}
                        placeholder="64자리 소문자 hex"
                        onChange={(value) => updateDraft(key, { evidenceSha256: value })}
                      />
                      <LabeledInput
                        label="증거 수집 시각"
                        value={draft.evidenceCapturedAt}
                        placeholder="2026-07-15T09:00:00+09:00"
                        onChange={(value) => updateDraft(key, { evidenceCapturedAt: value })}
                      />
                      <label className="text-sm text-ink">
                        <span className="mb-1 block font-medium">확정된 최종 주문 상태</span>
                        <select
                          className="min-h-control w-full rounded-md border border-controlLine bg-surface px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary"
                          value={draft.terminalStatus}
                          onChange={(event) => updateDraft(key, {
                            terminalStatus: event.target.value as DraftFields["terminalStatus"]
                          })}
                        >
                          <option value="">선택 필요</option>
                          <option value="filled">전체 체결</option>
                          <option value="canceled">취소</option>
                          <option value="expired">기한 만료</option>
                          <option value="rejected">거절</option>
                        </select>
                      </label>
                    </div>
                    <label className="mt-3 block text-sm text-ink">
                      <span className="mb-1 block font-medium">누락 체결 목록 (엄격한 JSON 배열)</span>
                      <textarea
                        className="min-h-28 w-full rounded-md border border-controlLine bg-surface px-3 py-2 font-mono text-xs focus:outline-none focus:ring-2 focus:ring-primary"
                        value={draft.missingFillsJson}
                        placeholder='누락 없음은 []를 직접 입력'
                        onChange={(event) => updateDraft(key, { missingFillsJson: event.target.value })}
                        aria-describedby={`missing-fill-help-${item.break_id}`}
                      />
                      <span id={`missing-fill-help-${item.break_id}`} className="mt-1 block text-xs text-mutedStrong">
                        각 항목은 스키마 버전, 순번, 제공자 ID, 수량·가격·비용, 체결 시각, 결제일, 증거 SHA를 모두 포함해야 합니다.
                      </span>
                    </label>
                    {localError ? <p className="mt-2 text-sm text-red-800" role="alert">{localError}</p> : null}
                    <button
                      type="button"
                      className={`${pageButtonClass("warning")} mt-3`}
                      disabled={pending || !requestAllowed || !isDraftExplicit(draft)}
                      onClick={() => {
                        try {
                          const evidence = parseEvidenceInput(draft);
                          openConfirmation({
                            action: "request",
                            breakId: item.break_id,
                            label: `${item.symbol} ${item.side === "buy" ? "매수" : "매도"} 회계 조정`,
                            tone: "primary",
                            evidence
                          }, item);
                        } catch (error) {
                          setValidationErrors((current) => ({
                            ...current,
                            [key]: error instanceof DataContractError
                              ? "누락 체결 목록이 엄격한 데이터 계약과 일치하지 않습니다."
                              : "증거 입력을 해석할 수 없습니다. 시간·SHA·JSON을 다시 확인하세요."
                          }));
                        }
                      }}
                    >
                      추가 본인 확인 후 조정 요청
                    </button>
                  </fieldset>
                ) : null}

                {item.request?.state === "requested" ? (
                  <section className="mt-4 rounded-md border border-sky-200 bg-sky-50 p-3">
                    <p className="text-sm font-semibold text-sky-950">위험 승인자 독립 검토</p>
                    {selfReview ? (
                      <p className="mt-1 text-sm text-red-800" role="alert">
                        본인이 요청한 회계 조정은 승인하거나 거절할 수 없습니다.
                      </p>
                    ) : (
                      <p className="mt-1 text-sm text-sky-900">
                        요청 요약 해시, 증거 SHA, 최종 주문 상태와 모든 누락 체결을 검토한 뒤 결정하세요.
                      </p>
                    )}
                    <div className="mt-3 flex flex-wrap gap-2">
                      <button
                        type="button"
                        className={pageButtonClass("safe")}
                        disabled={pending || !reviewAllowed}
                        onClick={() => {
                          openConfirmation({
                            action: "review",
                            breakId: item.break_id,
                            decision: "approve",
                            label: `${item.symbol} 회계 조정 승인`,
                            tone: "primary"
                          }, item);
                        }}
                      >
                        증거 승인
                      </button>
                      <button
                        type="button"
                        className={pageButtonClass("danger")}
                        disabled={pending || !reviewAllowed}
                        onClick={() => {
                          openConfirmation({
                            action: "review",
                            breakId: item.break_id,
                            decision: "reject",
                            label: `${item.symbol} 회계 조정 거절`,
                            tone: "danger"
                          }, item);
                        }}
                      >
                        증거 거절
                      </button>
                    </div>
                  </section>
                ) : null}
              </article>
            );
          })}
        </div>
      )}
      {notice ? (
        <p className="mt-4 rounded-lg border border-warning/30 bg-warningSoft p-3 text-sm text-warning" role="alert" aria-live="assertive">
          {notice}
        </p>
      ) : null}
      <ConfirmDialog
        open={confirmation !== null}
        title={confirmation?.action === "request"
          ? "회계 조정 요청 확인"
          : confirmation?.decision === "approve"
            ? "회계 조정 승인 확인"
            : "회계 조정 거절 확인"}
        description={confirmation === null
          ? undefined
          : confirmation.action === "request"
            ? `${confirmation.label}에 불변 증거와 누락 체결 목록을 제출합니다. 제출 뒤에는 이 입력을 수정할 수 없습니다.`
            : `${confirmation.label} 전에 최신 항목 상태, 검토 기한과 요청자·검토자 분리를 다시 확인합니다.`}
        confirmLabel={confirmation?.action === "request"
          ? "요청 전송"
          : confirmation?.decision === "approve"
            ? "승인 전송"
            : "거절 전송"}
        pendingLabel="최신 상태 확인 중"
        tone={confirmation?.tone ?? "primary"}
        pending={pending}
        onConfirm={submitConfirmedAction}
        onCancel={closeConfirmation}
      />
    </Panel>
  );
}

function parseEvidenceInput(draft: DraftFields): UnknownResolutionEvidenceInput {
  if (!isDraftExplicit(draft)) {
    throw new Error("unknown_resolution_evidence_fields_missing");
  }
  const parsedJson: unknown = JSON.parse(draft.missingFillsJson);
  const missingFills = parseDataContract(
    unknownResolutionMissingFillV2Schema.array().max(100),
    parsedJson,
    "unknown_resolution_missing_fills_v2:operator_input"
  );
  return {
    evidenceArtifactUri: draft.evidenceArtifactUri.trim(),
    evidenceSha256: draft.evidenceSha256.trim(),
    evidenceCapturedAt: draft.evidenceCapturedAt.trim(),
    terminalStatus: draft.terminalStatus,
    missingFills: missingFills as UnknownResolutionMissingFillV2[]
  };
}

function draftKey(context: UnknownResolutionContextV2): string {
  return `${context.break_id}:${context.break_revision}`;
}

function isDraftExplicit(draft: DraftFields): draft is DraftFields & {
  readonly terminalStatus: "filled" | "canceled" | "expired" | "rejected";
} {
  return draft.evidenceArtifactUri.trim().length > 0 &&
    draft.evidenceSha256.trim().length > 0 &&
    draft.evidenceCapturedAt.trim().length > 0 &&
    draft.terminalStatus !== "" &&
    draft.missingFillsJson.trim().length > 0;
}

function ResolutionTimeline({ context }: { readonly context: UnknownResolutionContextV2 }) {
  const resolutionComplete = isAccountingResolutionComplete(context);
  const workerApplied = context.work_receipt?.state === "applied";
  const steps = [
    {
      label: "요청 저장",
      complete: context.request !== null,
      detail: context.request ? formatKst(context.request.requested_at) : "대기"
    },
    {
      label: "독립 검토",
      complete: context.review !== null,
      detail: context.review
        ? `${reviewDecisionLabel(context.review.decision)} · ${formatKst(context.review.reviewed_at)}`
        : "대기"
    },
    {
      label: "Worker 적용 확인",
      complete: workerApplied,
      detail: workerApplied && context.work_receipt?.applied_at
        ? formatKst(context.work_receipt.applied_at)
        : context.work_receipt?.state === "claimed" && context.work_receipt.claimed_at
          ? `${formatKst(context.work_receipt.claimed_at)} · 적용 대기`
          : "대기"
    },
    {
      label: "회계 반영 결과",
      complete: resolutionComplete,
      detail: resolutionComplete && context.application_receipt
        ? `${formatKst(context.application_receipt.applied_at)} · ${shortHash(context.application_receipt.application_sha256)}`
        : context.application_receipt ? "적용 기록 확인 · 회계 반영 검증 대기" : "대기"
    }
  ];
  return (
    <ol className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-4" aria-label="조정 요청·승인·Worker 적용·회계 반영 타임라인">
      {steps.map((step, index) => (
        <li key={step.label} className="rounded-md border border-line bg-surface p-2 text-sm">
          <p className="font-medium text-ink">{index + 1}. {step.label}</p>
          <p className={step.complete ? "text-emerald-700" : "text-muted"}>
            {step.complete ? "확인됨" : "미확인"} · {step.detail}
          </p>
        </li>
      ))}
    </ol>
  );
}

function EvidenceValue({ label, value }: { readonly label: string; readonly value: string }) {
  return <div><dt className="text-muted">{label}</dt><dd className="break-all font-medium text-ink">{value}</dd></div>;
}

function LabeledInput({
  label,
  value,
  placeholder,
  onChange
}: {
  readonly label: string;
  readonly value: string;
  readonly placeholder: string;
  readonly onChange: (value: string) => void;
}) {
  return (
    <label className="text-sm text-ink">
      <span className="mb-1 block font-medium">{label}</span>
      <input
        type="text"
        className="min-h-control w-full rounded-md border border-controlLine bg-surface px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary"
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}

function caseTone(context: UnknownResolutionContextV2): Tone {
  if (isAccountingResolutionComplete(context)) {
    return "safe";
  }
  if (context.request?.state === "rejected") {
    return "danger";
  }
  return "warning";
}

function caseStatusLabel(context: UnknownResolutionContextV2): string {
  if (isAccountingResolutionComplete(context)) {
    return "Worker 적용·회계 반영 확인";
  }
  if (context.request?.state === "rejected") {
    return "검토 거절 · 재요청 가능";
  }
  if (context.work_receipt?.state === "claimed") {
    return "Worker 확인 · 적용 대기";
  }
  if (context.work_receipt?.state === "applied") {
    return "Worker 적용 확인 · 회계 반영 검증 대기";
  }
  if (context.request?.state === "approved") {
    return "승인됨 · Worker 적용 확인 대기";
  }
  if (context.request?.state === "requested") {
    return "독립 검토 대기";
  }
  return "운영자 요청 대기";
}

function isAccountingResolutionComplete(context: UnknownResolutionContextV2): boolean {
  return context.postcondition.resolution_complete &&
    context.postcondition.accounting_application_recorded &&
    context.break_state === "resolved" &&
    context.reconciliation_state === "complete" &&
    context.request?.state === "applied" &&
    context.work_receipt?.state === "applied" &&
    context.application_receipt !== null;
}

function reviewDecisionLabel(decision: string): string {
  return decision === "approved" ? "승인" : "거절";
}

function terminalStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    filled: "전체 체결",
    canceled: "취소",
    expired: "기한 만료",
    rejected: "거절"
  };
  return labels[String(status)] ?? String(status);
}

function shortHash(value: string): string {
  return `${value.slice(0, 12)}…${value.slice(-8)}`;
}
