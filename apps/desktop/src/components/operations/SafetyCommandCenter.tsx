import { Suspense, lazy, useRef, useState } from "react";
import {
  Check,
  ChevronDown,
  Circle,
  CirclePause,
  CirclePlay,
  CircleStop,
  Clock3,
  FlaskConical,
  Minus,
  ShieldAlert,
  XCircle
} from "lucide-react";
import type {
  OperationCommandReceipt,
  OperationsSnapshot
} from "../../lib/operationsContracts";
import type { OperationCommandType } from "../../lib/operationRequests";
import type { ConfirmAction } from "../../lib/uiState";
import { formatKst } from "../../lib/formatters";
import { commandConfirmationGuard } from "../../lib/operationGuards";
import { KeyValue, Panel, Pill, SectionTitle, pageButtonClass } from "../ui";
import type { Tone } from "../ui";

const ConfirmDialog = lazy(async () => ({
  default: (await import("../DialogSurface")).ConfirmDialog
}));

interface CommandAction {
  readonly type: OperationCommandType;
  readonly label: string;
  readonly description: string;
  readonly tone: Tone;
  readonly requiresQualification: boolean;
  readonly contractTestOnly: boolean;
}

const commandActions: readonly CommandAction[] = [
  {
    type: "emergency_stop",
    label: "비상 정지 요청",
    description: "Worker 적용 뒤 최신 실행 상태에서 주문 생성 중지를 확인해야 완료됩니다.",
    tone: "danger",
    requiresQualification: false,
    contractTestOnly: false
  },
  {
    type: "pause_paper",
    label: "모의거래 일시정지",
    description: "현재 모의거래의 주문 생성을 안전하게 멈추도록 요청합니다.",
    tone: "warning",
    requiresQualification: false,
    contractTestOnly: false
  },
  {
    type: "resume_paper",
    label: "모의거래 재개 요청",
    description: "유효한 운영 준비 확인과 독립 검토를 거쳐 주문 생성을 재개합니다.",
    tone: "safe",
    requiresQualification: true,
    contractTestOnly: false
  },
  {
    type: "activate_paper_strategy",
    label: "모의거래 전략 적용",
    description: "운영 준비 확인에 고정된 전략 버전만 적용 요청합니다.",
    tone: "neutral",
    requiresQualification: true,
    contractTestOnly: false
  },
  {
    type: "apply_risk_policy_version",
    label: "위험 정책 적용",
    description: "운영 준비 확인에 고정된 위험 정책 버전만 적용 요청합니다.",
    tone: "neutral",
    requiresQualification: true,
    contractTestOnly: false
  },
  {
    type: "start_contract_test",
    label: "계약 테스트 시작",
    description: "외부 주문 전송 없이 계약 경계를 검증합니다.",
    tone: "info",
    requiresQualification: true,
    contractTestOnly: true
  }
];

const otherOperationTypes = [
  "activate_paper_strategy",
  "apply_risk_policy_version",
  "start_contract_test"
] as const satisfies readonly OperationCommandType[];

export function SafetyCommandCenter({
  snapshot,
  mutationsAllowed,
  pending,
  onRequest
}: {
  readonly snapshot: OperationsSnapshot;
  readonly mutationsAllowed: boolean;
  readonly pending: boolean;
  readonly onRequest: (type: OperationCommandType, openGuard: string) => void | Promise<void>;
}) {
  const [confirmAction, setConfirmAction] = useState<ConfirmAction<OperationCommandType> | null>(null);
  const confirmGuardRef = useRef<string | null>(null);
  const canRequest = snapshot.access.permissions.includes("request_command");
  const qualificationReady = isQualificationReady(snapshot);
  const paperRunning =
    snapshot.runtime_health.environment === "paper" && snapshot.runtime_health.execution_enabled;
  const primaryAction = actionFor(paperRunning ? "pause_paper" : "resume_paper");
  const selectedAction = confirmAction === null ? null : actionFor(confirmAction.kind);
  const selectedBlockReason = selectedAction
    ? commandBlockReason(selectedAction, snapshot, { mutationsAllowed, pending, canRequest, qualificationReady })
    : null;
  const urgentCommand = selectUrgentCommand(snapshot.commands, snapshot);

  const openConfirm = (action: CommandAction) => {
    confirmGuardRef.current = commandConfirmationGuard(snapshot, action.type);
    setConfirmAction({
      kind: action.type,
      label: action.label,
      tone: confirmTone(action)
    });
  };

  const closeConfirm = () => {
    confirmGuardRef.current = null;
    setConfirmAction(null);
  };

  return (
    <div className="grid gap-[14px]">
      <Panel className="overflow-hidden !p-0">
        <div className="p-5">
        <SectionTitle
          title="현재 실행 상태"
          detail={
            <Pill tone={runtimeTone(snapshot)}>
              {runtimeStateLabel(snapshot)}
            </Pill>
          }
        />

        <div className="flex flex-col gap-5 lg:flex-row lg:items-end lg:justify-between">
          <div className="max-w-2xl">
            <p className="text-base leading-6 text-muted">
              마지막 갱신 {formatKst(snapshot.runtime_health.as_of)} · 명령 접수만으로 완료되지 않으며,
              Worker 적용과 최신 실행 상태를 함께 확인합니다.
            </p>
            <p className="mt-1 text-xs leading-5 text-muted">
              비상 정지를 제외한 요청은 5분·1회용 작업 전용 2단계 인증과 독립 검토를 거칩니다.
              오프라인 요청은 저장하거나 자동 재전송하지 않습니다.
            </p>
          </div>

          <CommandButton
            action={primaryAction}
            snapshot={snapshot}
            mutationsAllowed={mutationsAllowed}
            pending={pending}
            canRequest={canRequest}
            qualificationReady={qualificationReady}
            priority="primary"
            onClick={() => openConfirm(primaryAction)}
          />
        </div>

        <div className="mt-5 grid gap-x-6 border-y border-lineSubtle bg-canvas px-1 sm:grid-cols-2">
          <KeyValue
            label="운영 준비 확인"
            value={<Pill tone={qualificationReady ? "safe" : "warning"}>{qualificationReady ? "유효" : "차단"}</Pill>}
          />
          <KeyValue label="Worker 배포 버전" value={snapshot.qualification?.release_sha.slice(0, 12) ?? "확인 불가"} />
          <KeyValue label="운영 준비 회계 기준점" value={snapshot.qualification?.ledger_checkpoint ?? "확인 불가"} />
          <KeyValue label="운영 준비 만료" value={formatKst(snapshot.qualification?.valid_until)} />
        </div>

        {!canRequest ? (
          <p className="mt-3 text-sm text-warning" role="status">
            현재 역할은 일반 운영 명령을 요청할 수 없습니다. 비상 정지는 최근 2단계 인증을 완료한 운영 담당자만
            별도 조건으로 요청할 수 있습니다.
          </p>
        ) : null}
      </div>

      <div className="border-t border-lineSubtle bg-dangerSoft p-5">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="max-w-2xl">
            <div className="flex items-center gap-2 font-semibold text-danger">
              <ShieldAlert size={18} aria-hidden="true" />
              비상 제어
            </div>
            <p className="mt-1 text-base leading-6 text-danger">
              중대한 이상이 확인된 경우에만 사용합니다. 접수 뒤에도 최신 실행 상태에서 주문 생성 중지를 확인하세요.
            </p>
          </div>
          <CommandButton
            action={actionFor("emergency_stop")}
            snapshot={snapshot}
            mutationsAllowed={mutationsAllowed}
            pending={pending}
            canRequest={canRequest}
            qualificationReady={qualificationReady}
            priority="danger"
            onClick={() => openConfirm(actionFor("emergency_stop"))}
          />
        </div>
      </div>

      <details className="group border-t border-lineSubtle">
        <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-3 px-5 py-4 font-semibold text-ink marker:content-none">
          <span>기타 운영 작업</span>
          <ChevronDown
            size={18}
            className="text-muted transition-transform duration-state group-open:rotate-180 motion-reduce:transition-none"
            aria-hidden="true"
          />
        </summary>
        <div className="grid gap-3 border-t border-lineSubtle bg-canvas p-5 sm:grid-cols-2 xl:grid-cols-3">
          {otherOperationTypes.map((type) => {
            const action = actionFor(type);
            return (
              <CommandButton
                key={type}
                action={action}
                snapshot={snapshot}
                mutationsAllowed={mutationsAllowed}
                pending={pending}
                canRequest={canRequest}
                qualificationReady={qualificationReady}
                priority="secondary"
                onClick={() => openConfirm(action)}
              />
            );
          })}
        </div>
        </details>
      </Panel>

      <Panel>
        <SectionTitle
          title="진행 중인 명령"
          detail={
            <span className="text-xs text-muted">
              {snapshot.commands.length === 0 ? "대기 중인 명령 없음" : `총 ${snapshot.commands.length}건 중 우선 확인 1건`}
            </span>
          }
        />
        {urgentCommand === null ? (
          <p className="rounded-lg border border-dashed border-lineSubtle p-4 text-sm text-muted">
            표시할 명령 영수증이 없습니다.
          </p>
        ) : (
          <CommandTimeline command={urgentCommand} snapshot={snapshot} />
        )}
      </Panel>

      {confirmAction !== null ? (
        <Suspense fallback={null}>
          <ConfirmDialog
            open
            title={confirmAction.label}
            description="확인하는 순간의 최신 상태로 요청 조건을 다시 검사합니다. 접수만으로 적용 완료가 되지 않습니다."
            confirmLabel="요청 생성"
            pendingLabel="요청 확인 중"
            tone={confirmAction.tone}
            pending={pending}
            confirmDisabled={selectedBlockReason !== null}
            error={selectedBlockReason}
            onCancel={closeConfirm}
            onConfirm={async () => {
              if (selectedBlockReason !== null) {
                return;
              }
              const operationType = confirmAction.kind;
              const openGuard = confirmGuardRef.current;
              if (openGuard === null) {
                return;
              }
              confirmGuardRef.current = null;
              setConfirmAction(null);
              await onRequest(operationType, openGuard);
            }}
          />
        </Suspense>
      ) : null}
    </div>
  );
}

function CommandButton({
  action,
  snapshot,
  mutationsAllowed,
  pending,
  canRequest,
  qualificationReady,
  priority,
  onClick
}: {
  readonly action: CommandAction;
  readonly snapshot: OperationsSnapshot;
  readonly mutationsAllowed: boolean;
  readonly pending: boolean;
  readonly canRequest: boolean;
  readonly qualificationReady: boolean;
  readonly priority: "primary" | "danger" | "secondary";
  readonly onClick: () => void;
}) {
  const blockReason = commandBlockReason(action, snapshot, {
    mutationsAllowed,
    pending,
    canRequest,
    qualificationReady
  });
  const descriptionId = `${action.type}-description`;
  const primaryClass =
    priority === "primary"
      ? "min-h-11 w-full shrink-0 !border-primaryAction !bg-primaryAction !text-white hover:brightness-110"
      : priority === "danger"
        ? "min-h-11 w-full shrink-0 !border-danger !bg-danger !text-canvas hover:brightness-110"
        : "min-h-24 w-full flex-col items-start text-left";

  return (
    <div className={priority === "secondary" ? "min-w-0" : "w-full sm:w-[220px] sm:shrink-0"}>
      <button
        type="button"
        className={`${pageButtonClass(priority === "primary" ? "neutral" : action.tone)} ${primaryClass} transition-transform duration-press ease-product active:scale-[0.985] motion-reduce:transform-none motion-reduce:transition-none`}
        data-command-priority={priority}
        disabled={blockReason !== null}
        aria-describedby={descriptionId}
        onClick={onClick}
      >
        <span className="flex items-center gap-2">
          <CommandIcon type={action.type} />
          {action.label}
        </span>
        {priority === "secondary" ? (
          <span className="text-xs font-normal leading-5">{action.description}</span>
        ) : null}
      </button>
      <p
        id={descriptionId}
        className={`mt-2 text-xs leading-5 ${blockReason === null ? "text-muted" : "font-medium text-warning"}`}
      >
        {blockReason ?? action.description}
      </p>
    </div>
  );
}

function commandBlockReason(
  action: CommandAction,
  snapshot: OperationsSnapshot,
  state: {
    readonly mutationsAllowed: boolean;
    readonly pending: boolean;
    readonly canRequest: boolean;
    readonly qualificationReady: boolean;
  }
): string | null {
  if (state.pending) {
    return "다른 운영 요청을 확인하고 있습니다. 처리가 끝난 뒤 다시 시도하세요.";
  }
  if (!state.mutationsAllowed) {
    return "기기 연결과 최신 전체 상태·실시간 신호·Worker 상태를 확인해야 요청할 수 있습니다.";
  }
  if (action.type !== "emergency_stop" && !state.canRequest) {
    return "현재 역할에는 운영 명령 요청 권한이 없습니다.";
  }
  if (
    action.type === "emergency_stop" &&
    (snapshot.access.assurance_level !== "aal2" || snapshot.access.actor?.roles.includes("operator") !== true)
  ) {
    return "최근 2단계 인증을 완료한 운영자 역할이 필요합니다.";
  }
  if (
    (action.type === "pause_paper" || action.type === "resume_paper") &&
    snapshot.runtime_health.environment !== "paper"
  ) {
    return "현재 환경이 모의거래가 아니므로 이 작업을 요청할 수 없습니다.";
  }
  if (action.requiresQualification && !state.qualificationReady) {
    return "유효한 운영 준비 확인과 고정된 배포 버전·회계 기준점이 필요합니다.";
  }
  if (action.contractTestOnly && snapshot.runtime_health.environment !== "contract_test") {
    return "계약 테스트 환경에서만 시작할 수 있습니다.";
  }
  return null;
}

function CommandTimeline({
  command,
  snapshot
}: {
  readonly command: OperationCommandReceipt;
  readonly snapshot: OperationsSnapshot;
}) {
  const reviewDone = ["approved", "claimed", "applied", "failed"].includes(command.state);
  const workerApplied = command.worker_ack?.state === "applied";
  const postconditionVerified = isCommandPostconditionVerified(command, snapshot);
  const terminalState = terminalCommandState(command.state);
  const terminal = ["rejected", "failed", "expired", "canceled"].includes(command.state);
  const steps: readonly ProgressStep[] = [
    { label: "요청 접수", detail: "제어면 영수증", state: "complete" },
    command.command_type === "emergency_stop"
      ? { label: "독립 검토자 승인", detail: "비상 정지는 검토 없음", state: "not_applicable" }
      : {
          label: "독립 검토자 승인",
          detail: reviewDone ? "승인 영수증 확인" : "검토 대기",
          state: command.state === "rejected" ? "blocked" : reviewDone ? "complete" : terminal ? "pending" : "current"
        },
    {
      label: "Worker 작업 인수 / 적용",
      detail: workerApplied ? "적용 보고 수신" : command.worker_ack === null ? "Worker 확인 대기" : "Worker 처리 중",
      state:
        command.state === "failed"
          ? "failed"
          : workerApplied
            ? "complete"
            : command.worker_ack !== null
              ? "current"
              : "pending"
    },
    {
      label: "최신 실행 상태 확인",
      detail: postconditionVerified ? "결과 조건 확인" : "최신 상태 대기",
      state: postconditionVerified ? "complete" : workerApplied ? "current" : "pending"
    }
  ];

  return (
    <article className="rounded-lg border border-lineSubtle bg-canvas p-4" aria-label={`${commandLabel(command.command_type)} 처리 상태`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="font-semibold text-ink">{commandLabel(command.command_type)}</p>
          <p className="mt-1 text-xs text-muted">
            요청 {formatKst(command.requested_at)} · 만료 {formatKst(command.expires_at)}
          </p>
        </div>
        <Pill tone={commandStateTone(command, postconditionVerified)}>
          {commandStateLabel(command.state, postconditionVerified)}
        </Pill>
      </div>

      <ol className="mt-4 grid gap-2 sm:grid-cols-2 xl:grid-cols-4" aria-label="명령 진행 4단계">
        {steps.map((step, index) => (
          <ProgressStepItem key={step.label} step={step} index={index + 1} />
        ))}
      </ol>

      <p className="mt-3 break-words text-xs text-muted">
        제어면 영수증 r{command.control_plane_receipt.revision} · Worker 적용 확인 {workerAckLabel(command)}
      </p>
      {command.worker_ack?.state === "applied" && !postconditionVerified ? (
        <p className="mt-2 flex items-start gap-2 text-xs leading-5 text-warning" role="status">
          <Clock3 className="mt-0.5 shrink-0" size={14} aria-hidden="true" />
          Worker 적용 보고는 수신했지만 최신 실행 상태에서 결과를 아직 확인하지 못했습니다. 완료로 표시하지 않습니다.
        </p>
      ) : null}
      {terminalState ? (
        <p className={`mt-2 text-xs leading-5 ${terminalState.className}`} role="status">
          {terminalState.message}
        </p>
      ) : null}
    </article>
  );
}

type ProgressState = "complete" | "current" | "pending" | "blocked" | "failed" | "not_applicable";

interface ProgressStep {
  readonly label: string;
  readonly detail: string;
  readonly state: ProgressState;
}

function ProgressStepItem({ step, index }: { readonly step: ProgressStep; readonly index: number }) {
  const style = progressStepStyle(step.state);
  return (
    <li className={`rounded-lg border px-3 py-3 text-xs ${style.className}`} aria-current={step.state === "current" ? "step" : undefined}>
      <span className="flex items-center gap-2 font-semibold">
        <span className="inline-flex size-5 shrink-0 items-center justify-center rounded-full border" aria-hidden="true">
          <ProgressIcon state={step.state} />
        </span>
        {index}. {step.label}
      </span>
      <span className="mt-1 block pl-7 leading-5">{style.stateLabel} · {step.detail}</span>
    </li>
  );
}

function ProgressIcon({ state }: { readonly state: ProgressState }) {
  if (state === "complete") {
    return <Check size={12} strokeWidth={3} />;
  }
  if (state === "current") {
    return <Clock3 size={11} />;
  }
  if (state === "blocked" || state === "failed") {
    return <XCircle size={12} />;
  }
  if (state === "not_applicable") {
    return <Minus size={12} />;
  }
  return <Circle size={8} />;
}

function progressStepStyle(state: ProgressState): { readonly className: string; readonly stateLabel: string } {
  if (state === "complete") {
    return { className: "border-success/30 bg-successSoft text-success", stateLabel: "확인됨" };
  }
  if (state === "current") {
    return { className: "border-primary/30 bg-primarySoft text-primary", stateLabel: "확인 중" };
  }
  if (state === "failed") {
    return { className: "border-danger/30 bg-dangerSoft text-danger", stateLabel: "중단됨" };
  }
  if (state === "blocked") {
    return { className: "border-warning/30 bg-warningSoft text-warning", stateLabel: "거절됨" };
  }
  if (state === "not_applicable") {
    return { className: "border-dashed border-lineSubtle bg-canvas text-mutedStrong", stateLabel: "해당 없음" };
  }
  return { className: "border-lineSubtle bg-canvas text-mutedStrong", stateLabel: "대기" };
}

function selectUrgentCommand(
  commands: readonly OperationCommandReceipt[],
  snapshot: OperationsSnapshot
): OperationCommandReceipt | null {
  if (commands.length === 0) {
    return null;
  }
  return [...commands].sort((left, right) => {
    const priorityDifference = commandUrgency(left, snapshot) - commandUrgency(right, snapshot);
    if (priorityDifference !== 0) {
      return priorityDifference;
    }
    return Date.parse(left.expires_at) - Date.parse(right.expires_at);
  })[0] ?? null;
}

function commandUrgency(command: OperationCommandReceipt, snapshot: OperationsSnapshot): number {
  if (command.state === "failed") {
    return 0;
  }
  if (command.state === "applied" && !isCommandPostconditionVerified(command, snapshot)) {
    return 1;
  }
  if (command.state === "claimed") {
    return 2;
  }
  if (command.state === "approved") {
    return 3;
  }
  if (command.state === "requested") {
    return 4;
  }
  if (["rejected", "expired", "canceled"].includes(command.state)) {
    return 5;
  }
  return 6;
}

function CommandIcon({ type }: { readonly type: OperationCommandType }) {
  if (type === "emergency_stop") {
    return <ShieldAlert size={17} aria-hidden="true" />;
  }
  if (type === "pause_paper") {
    return <CirclePause size={17} aria-hidden="true" />;
  }
  if (type === "resume_paper") {
    return <CirclePlay size={17} aria-hidden="true" />;
  }
  if (type === "start_contract_test") {
    return <FlaskConical size={17} aria-hidden="true" />;
  }
  return <CircleStop size={17} aria-hidden="true" />;
}

function actionFor(type: OperationCommandType): CommandAction {
  const action = commandActions.find((candidate) => candidate.type === type);
  if (!action) {
    throw new Error(`Unsupported operation command: ${type}`);
  }
  return action;
}

function confirmTone(action: CommandAction): ConfirmAction["tone"] {
  if (action.tone === "danger") {
    return "danger";
  }
  if (action.type === "pause_paper" || action.type === "resume_paper") {
    return "primary";
  }
  return "neutral";
}

function runtimeStateLabel(snapshot: OperationsSnapshot): string {
  const runtime = snapshot.runtime_health;
  if (runtime.overall_state !== "fresh") {
    return "상태 확인 필요";
  }
  if (runtime.environment === "contract_test") {
    return runtime.execution_enabled ? "계약 테스트 실행 중" : "계약 테스트 중지";
  }
  return runtime.execution_enabled ? "모의거래 실행 중" : "주문 생성 중지";
}

function runtimeTone(snapshot: OperationsSnapshot): Tone {
  if (snapshot.runtime_health.overall_state !== "fresh") {
    return "warning";
  }
  return snapshot.runtime_health.execution_enabled ? "info" : "neutral";
}

function isQualificationReady(snapshot: OperationsSnapshot): boolean {
  const qualification = snapshot.qualification;
  if (
    qualification === null ||
    qualification.status !== "qualified" ||
    qualification.environment !== snapshot.runtime_health.environment ||
    qualification.g1.status !== "pass" ||
    qualification.g2.status !== "pass"
  ) {
    return false;
  }
  const snapshotAt = Date.parse(snapshot.generated_at);
  return Date.parse(qualification.valid_from) <= snapshotAt && Date.parse(qualification.valid_until) > snapshotAt;
}

export function commandLabel(type: OperationCommandReceipt["command_type"]): string {
  const labels: Record<OperationCommandReceipt["command_type"], string> = {
    emergency_stop: "비상 정지",
    pause_paper: "모의거래 일시정지",
    resume_paper: "모의거래 재개",
    activate_paper_strategy: "모의거래 전략 적용",
    start_contract_test: "계약 테스트 시작",
    apply_risk_policy_version: "위험 정책 적용"
  };
  return labels[type];
}

function commandStateLabel(state: OperationCommandReceipt["state"], postconditionVerified = false): string {
  const labels: Record<OperationCommandReceipt["state"], string> = {
    requested: "요청 접수 · 검토 대기",
    approved: "검토 승인 · Worker 대기",
    claimed: "Worker 처리 중",
    applied: postconditionVerified ? "최신 실행 상태 확인 완료" : "Worker 적용 보고 · 확인 중",
    rejected: "검토 거절",
    failed: "적용 실패",
    expired: "요청 만료",
    canceled: "요청 취소"
  };
  return labels[state];
}

function commandStateTone(command: OperationCommandReceipt, postconditionVerified: boolean): Tone {
  if (command.state === "failed") {
    return "danger";
  }
  if (command.state === "rejected" || command.state === "expired") {
    return "warning";
  }
  if (command.state === "canceled") {
    return "neutral";
  }
  return postconditionVerified ? "safe" : "info";
}

function workerAckLabel(command: OperationCommandReceipt): string {
  if (command.worker_ack?.state === "applied") {
    return "적용 보고 수신";
  }
  if (command.worker_ack?.state === "claimed") {
    return "처리 중";
  }
  if (command.worker_ack?.state === "failed") {
    return "실패 보고 수신";
  }
  return "없음";
}

function terminalCommandState(state: OperationCommandReceipt["state"]): {
  readonly message: string;
  readonly className: string;
} | null {
  if (state === "failed") {
    return { message: "Worker 적용이 실패했습니다. 실패 코드를 확인하고 새 요청을 검토하세요.", className: "text-danger" };
  }
  if (state === "rejected") {
    return { message: "독립 검토자가 요청을 거절했습니다. 기존 요청은 다시 전송되지 않습니다.", className: "text-warning" };
  }
  if (state === "expired") {
    return { message: "유효 시간이 지나 요청이 만료됐습니다. 필요하면 최신 상태에서 새로 요청하세요.", className: "text-warning" };
  }
  if (state === "canceled") {
    return { message: "요청이 취소됐습니다. 취소된 요청은 자동 재개되지 않습니다.", className: "text-muted" };
  }
  return null;
}

export function isCommandPostconditionVerified(
  command: OperationCommandReceipt,
  snapshot: OperationsSnapshot
): boolean {
  const runtime = snapshot.runtime_health;
  const postStateVersion = command.worker_ack?.state === "applied"
    ? command.worker_ack.post_state_version
    : null;
  if (
    command.state !== "applied" ||
    postStateVersion === null ||
    runtime.overall_state !== "fresh" ||
    runtime.state_version < postStateVersion ||
    runtime.environment !== command.environment
  ) {
    return false;
  }

  if (command.command_type === "emergency_stop" || command.command_type === "pause_paper") {
    return runtime.execution_enabled === false;
  }
  if (command.command_type === "resume_paper") {
    return runtime.environment === "paper" && runtime.execution_enabled;
  }
  if (command.command_type === "start_contract_test") {
    return runtime.environment === "contract_test" && runtime.execution_enabled;
  }
  if (command.command_type === "activate_paper_strategy") {
    return (
      command.strategy_version_id !== null &&
      runtime.active_strategy_version_id === command.strategy_version_id
    );
  }
  return (
    command.risk_policy_version_id !== null &&
    runtime.active_risk_policy_version_id === command.risk_policy_version_id
  );
}
