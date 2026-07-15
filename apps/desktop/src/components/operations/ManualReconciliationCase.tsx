import { useState } from "react";
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

export interface ManualReconciliationCaseProps {
  readonly snapshot: OperationsSnapshot;
  readonly unknownSnapshot: UnknownResolutionSnapshotV2 | null;
  readonly mutationsAllowed: boolean;
  readonly pending: boolean;
  readonly now: Date;
  readonly onRequest: (
    context: UnknownResolutionContextV2,
    evidence: UnknownResolutionEvidenceInput
  ) => void;
  readonly onReview: (
    context: UnknownResolutionContextV2,
    decision: "approve" | "reject"
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
  const roles = new Set(snapshot.access.actor?.roles ?? []);
  const canView = roles.has("operator") || roles.has("risk_approver") || roles.has("auditor");
  const cases = unknownSnapshot?.cases ?? [];

  const updateDraft = (breakId: string, patch: Partial<DraftFields>) => {
    setDrafts((current) => ({
      ...current,
      [breakId]: { ...(current[breakId] ?? emptyDraft), ...patch }
    }));
    setValidationErrors((current) => ({ ...current, [breakId]: "" }));
  };

  return (
    <Panel className="xl:col-span-2">
      <SectionTitle
        title="수동 대사 케이스"
        detail={<Pill tone={cases.length > 0 ? "warning" : "safe"}>{cases.length}건 · V2</Pill>}
      />
      <p className="mb-4 text-sm text-muted">
        unknown 관찰의 불변 hash와 CAS revision을 검토합니다. 브로커 API를 직접 호출하거나 주문 상태를 임의 보정하지 않습니다.
        승인 후에도 Worker ACK와 회계 postcondition 전에는 완료로 표시하지 않습니다.
      </p>
      {!canView ? (
        <p className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
          operator, risk_approver 또는 auditor 역할이 없어 V2 수동 대사 증거를 표시하지 않습니다.
        </p>
      ) : unknownSnapshot === null ? (
        <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-900" role="alert">
          schema_version=2 수동 대사 projection을 확인할 수 없어 요청·승인을 모두 차단했습니다.
        </div>
      ) : cases.length === 0 ? (
        <EmptyState
          title="수동 대사 대상이 없습니다"
          detail="unknown_requires_manual_check 관찰만 이 전용 maker-checker 경로에 표시됩니다."
        />
      ) : (
        <div className="space-y-4">
          {cases.map((item) => {
            const draft = drafts[item.break_id] ?? emptyDraft;
            const requestEligible = canRequestUnknownResolution(snapshot.access, item);
            const requestAllowed = mutationsAllowed && requestEligible;
            const reviewAllowed = mutationsAllowed && canReviewUnknownResolution(snapshot.access, item, now);
            const selfReview = item.request?.requested_by.actor_id === snapshot.access.actor?.actor_id;
            const localError = validationErrors[item.break_id];
            return (
              <article key={item.break_id} className="rounded-md border border-line p-4">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <p className="flex items-center gap-2 font-semibold text-ink">
                      <FileSearch size={17} aria-hidden="true" />
                      {item.symbol} {item.side === "buy" ? "매수" : "매도"} · {item.requested_quantity}주
                    </p>
                    <p className="mt-1 text-xs text-muted">
                      break {item.break_id.slice(0, 8)} · intent {item.intent_id.slice(0, 8)} · 관찰 {item.unknown_observation.sequence}
                    </p>
                  </div>
                  <Pill tone={caseTone(item)}>{caseStatusLabel(item)}</Pill>
                </div>

                <dl className="mt-3 grid gap-2 text-sm sm:grid-cols-2 lg:grid-cols-4">
                  <EvidenceValue label="환경" value={item.environment === "paper" ? "PAPER" : "CONTRACT TEST"} />
                  <EvidenceValue label="provider" value={item.provider_identity.broker} />
                  <EvidenceValue label="provider order" value={item.provider_identity.provider_order_id} />
                  <EvidenceValue label="unknown 관찰 시각" value={formatKst(item.unknown_observation.observed_at)} />
                  <EvidenceValue label="누적 체결" value={`${item.unknown_observation.cumulative_quantity}주`} />
                  <EvidenceValue label="break revision" value={String(item.break_revision)} />
                  <EvidenceValue label="cash projection" value={String(item.cash_projection_version)} />
                  <EvidenceValue label="position projection" value={item.position_projection_version === null ? "없음" : String(item.position_projection_version)} />
                  <EvidenceValue label="reservation sequence" value={String(item.reservation_event_sequence)} />
                  <EvidenceValue label="control epoch" value={String(item.control_epoch)} />
                  <EvidenceValue label="observation hash" value={shortHash(item.unknown_observation.observation_sha256)} />
                  <EvidenceValue label="provider hash" value={shortHash(item.unknown_observation.provider_observation_sha256)} />
                </dl>

                <ResolutionTimeline context={item} />

                {item.request ? (
                  <section className="mt-4 rounded-md border border-slate-200 bg-slate-50 p-3" aria-label="제출된 회계 조정 증거">
                    <p className="text-sm font-semibold text-ink">요청자가 고정한 증거·누락 체결</p>
                    <dl className="mt-2 grid gap-2 text-sm sm:grid-cols-2">
                      <EvidenceValue label="요청자" value={item.request.requested_by.display_name} />
                      <EvidenceValue label="terminal" value={item.request.terminal_status} />
                      <EvidenceValue label="evidence SHA" value={shortHash(item.request.evidence_sha256)} />
                      <EvidenceValue label="request digest" value={shortHash(item.request.request_digest_sha256)} />
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
                            execution {fill.provider_execution_id} · settlement {fill.settlement_date} ·
                            evidence {shortHash(fill.evidence_sha256)}
                          </li>
                        ))}
                      </ol>
                    )}
                  </section>
                ) : null}

                {requestEligible ? (
                  <fieldset className="mt-4 rounded-md border border-amber-200 bg-amber-50 p-3">
                    <legend className="px-1 text-sm font-semibold text-amber-950">operator 회계 조정 요청</legend>
                    <p className="mb-3 text-xs text-amber-900">
                      모든 필드를 직접 입력하세요. 누락 체결이 없더라도 <code>[]</code>를 입력해야 하며 자동 기본값은 없습니다.
                    </p>
                    <div className="grid gap-3 sm:grid-cols-2">
                      <LabeledInput
                        label="Evidence artifact URI"
                        value={draft.evidenceArtifactUri}
                        placeholder="urn:sha256:<64 hex>"
                        onChange={(value) => updateDraft(item.break_id, { evidenceArtifactUri: value })}
                      />
                      <LabeledInput
                        label="Evidence SHA-256"
                        value={draft.evidenceSha256}
                        placeholder="64자리 소문자 hex"
                        onChange={(value) => updateDraft(item.break_id, { evidenceSha256: value })}
                      />
                      <LabeledInput
                        label="Evidence captured at"
                        value={draft.evidenceCapturedAt}
                        placeholder="2026-07-15T09:00:00+09:00"
                        onChange={(value) => updateDraft(item.break_id, { evidenceCapturedAt: value })}
                      />
                      <label className="text-sm text-ink">
                        <span className="mb-1 block font-medium">확정 terminal 상태</span>
                        <select
                          className="w-full rounded-md border border-line bg-white px-3 py-2 focus:outline-none focus:ring-2 focus:ring-slate-400"
                          value={draft.terminalStatus}
                          onChange={(event) => updateDraft(item.break_id, {
                            terminalStatus: event.target.value as DraftFields["terminalStatus"]
                          })}
                        >
                          <option value="">선택 필요</option>
                          <option value="filled">filled</option>
                          <option value="canceled">canceled</option>
                          <option value="expired">expired</option>
                          <option value="rejected">rejected</option>
                        </select>
                      </label>
                    </div>
                    <label className="mt-3 block text-sm text-ink">
                      <span className="mb-1 block font-medium">누락 체결 manifest (strict JSON array)</span>
                      <textarea
                        className="min-h-28 w-full rounded-md border border-line bg-white px-3 py-2 font-mono text-xs focus:outline-none focus:ring-2 focus:ring-slate-400"
                        value={draft.missingFillsJson}
                        placeholder='누락 없음은 []를 직접 입력'
                        onChange={(event) => updateDraft(item.break_id, { missingFillsJson: event.target.value })}
                        aria-describedby={`missing-fill-help-${item.break_id}`}
                      />
                      <span id={`missing-fill-help-${item.break_id}`} className="mt-1 block text-xs text-muted">
                        각 항목은 schema_version, 순번, provider IDs, 수량·가격·비용, 체결시각, 결제일, evidence SHA를 모두 포함해야 합니다.
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
                          if (!window.confirm(
                            "불변 evidence와 누락 체결 manifest를 수동 대사 요청으로 제출하시겠습니까? 제출 후에는 수정할 수 없습니다."
                          )) {
                            return;
                          }
                          onRequest(item, evidence);
                        } catch (error) {
                          setValidationErrors((current) => ({
                            ...current,
                            [item.break_id]: error instanceof DataContractError
                              ? "누락 체결 manifest가 schema_version=2 strict 계약과 일치하지 않습니다."
                              : "증거 입력을 해석할 수 없습니다. 시간·SHA·JSON을 다시 확인하세요."
                          }));
                        }
                      }}
                    >
                      AAL2 step-up 후 조정 요청
                    </button>
                  </fieldset>
                ) : null}

                {item.request?.state === "requested" ? (
                  <section className="mt-4 rounded-md border border-sky-200 bg-sky-50 p-3">
                    <p className="text-sm font-semibold text-sky-950">risk_approver 독립 검토</p>
                    {selfReview ? (
                      <p className="mt-1 text-sm text-red-800" role="alert">
                        본인이 요청한 회계 조정은 승인하거나 거절할 수 없습니다.
                      </p>
                    ) : (
                      <p className="mt-1 text-sm text-sky-900">
                        요청 digest, evidence SHA, terminal 상태와 모든 누락 체결을 검토한 뒤 결정하세요.
                      </p>
                    )}
                    <div className="mt-3 flex flex-wrap gap-2">
                      <button
                        type="button"
                        className={pageButtonClass("safe")}
                        disabled={pending || !reviewAllowed}
                        onClick={() => {
                          if (window.confirm("제출된 evidence와 모든 누락 체결을 독립 검토했으며 Worker 적용을 승인하시겠습니까?")) {
                            onReview(item, "approve");
                          }
                        }}
                      >
                        증거 승인
                      </button>
                      <button
                        type="button"
                        className={pageButtonClass("danger")}
                        disabled={pending || !reviewAllowed}
                        onClick={() => {
                          if (window.confirm("증거 불충분으로 수동 대사 요청을 거절하시겠습니까?")) {
                            onReview(item, "reject");
                          }
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
  const steps = [
    {
      label: "요청 저장",
      complete: context.request !== null,
      detail: context.request ? formatKst(context.request.requested_at) : "대기"
    },
    {
      label: "독립 검토",
      complete: context.review !== null,
      detail: context.review ? `${context.review.decision} · ${formatKst(context.review.reviewed_at)}` : "대기"
    },
    {
      label: "Worker ACK",
      complete: context.work_receipt?.state === "claimed" || context.work_receipt?.state === "applied",
      detail: context.work_receipt?.claimed_at ? formatKst(context.work_receipt.claimed_at) : "대기"
    },
    {
      label: "회계 postcondition",
      complete: context.postcondition.resolution_complete,
      detail: context.postcondition.resolution_complete && context.application_receipt
        ? `${formatKst(context.application_receipt.applied_at)} · ${shortHash(context.application_receipt.application_sha256)}`
        : context.application_receipt ? "application receipt 확인 · projection 검증 대기" : "대기"
    }
  ];
  return (
    <ol className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-4" aria-label="조정 요청·승인·Worker ACK·postcondition 타임라인">
      {steps.map((step, index) => (
        <li key={step.label} className="rounded-md border border-line bg-white p-2 text-sm">
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
        className="w-full rounded-md border border-line bg-white px-3 py-2 focus:outline-none focus:ring-2 focus:ring-slate-400"
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}

function caseTone(context: UnknownResolutionContextV2): Tone {
  if (context.postcondition.resolution_complete) {
    return "safe";
  }
  if (context.request?.state === "rejected") {
    return "danger";
  }
  return "warning";
}

function caseStatusLabel(context: UnknownResolutionContextV2): string {
  if (context.postcondition.resolution_complete) {
    return "Worker 적용·회계 확인";
  }
  if (context.request?.state === "rejected") {
    return "검토 거절 · 재요청 가능";
  }
  if (context.work_receipt?.state === "claimed") {
    return "Worker ACK · 적용 대기";
  }
  if (context.request?.state === "approved") {
    return "승인됨 · Worker ACK 대기";
  }
  if (context.request?.state === "requested") {
    return "독립 검토 대기";
  }
  return "operator 요청 대기";
}

function shortHash(value: string): string {
  return `${value.slice(0, 12)}…${value.slice(-8)}`;
}
