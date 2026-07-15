import { CirclePause, CirclePlay, CircleStop, FlaskConical, ShieldAlert } from "lucide-react";
import type {
  OperationCommandReceipt,
  OperationsSnapshot
} from "../../lib/operationsContracts";
import type { OperationCommandType } from "../../lib/operationRequests";
import { formatKst } from "../../lib/formatters";
import { KeyValue, Panel, Pill, SectionTitle, pageButtonClass } from "../ui";
import type { Tone } from "../ui";

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
    description: "Worker가 claim하고 적용 결과를 ACK할 때까지 완료로 보지 않습니다.",
    tone: "danger",
    requiresQualification: false,
    contractTestOnly: false
  },
  {
    type: "pause_paper",
    label: "PAPER 일시정지",
    description: "현재 PAPER 실행을 안전 정지하도록 요청합니다.",
    tone: "warning",
    requiresQualification: false,
    contractTestOnly: false
  },
  {
    type: "resume_paper",
    label: "PAPER 재개 요청",
    description: "유효한 G1/G2 자격 묶음으로 재개 승인을 요청합니다.",
    tone: "safe",
    requiresQualification: true,
    contractTestOnly: false
  },
  {
    type: "activate_paper_strategy",
    label: "PAPER 전략 적용",
    description: "자격 묶음에 고정된 전략 버전만 요청합니다.",
    tone: "neutral",
    requiresQualification: true,
    contractTestOnly: false
  },
  {
    type: "apply_risk_policy_version",
    label: "위험 정책 적용",
    description: "자격 묶음에 고정된 위험 정책 버전만 요청합니다.",
    tone: "neutral",
    requiresQualification: true,
    contractTestOnly: false
  },
  {
    type: "start_contract_test",
    label: "CONTRACT TEST 시작",
    description: "외부 주문 전송 없이 계약 경계를 검증합니다.",
    tone: "info",
    requiresQualification: true,
    contractTestOnly: true
  }
];

export function SafetyCommandCenter({
  snapshot,
  mutationsAllowed,
  pending,
  onRequest
}: {
  readonly snapshot: OperationsSnapshot;
  readonly mutationsAllowed: boolean;
  readonly pending: boolean;
  readonly onRequest: (type: OperationCommandType) => void;
}) {
  const canRequest = snapshot.access.permissions.includes("request_command");
  const qualificationReady = snapshot.qualification?.status === "qualified";

  return (
    <Panel>
      <SectionTitle
        title="안전 명령 센터"
        detail={
          <div className="flex flex-wrap gap-2">
            <Pill tone="danger">LIVE 금지</Pill>
            <Pill tone={mutationsAllowed ? "safe" : "warning"}>
              {mutationsAllowed ? "변경 가능" : "읽기 전용"}
            </Pill>
          </div>
        }
      />
      <p className="text-sm text-muted">
        모든 명령은 제어면 영수증과 Worker ACK를 분리합니다. 승인만으로 실제 적용을 표시하지 않습니다.
      </p>
      <p className="mt-1 text-xs text-muted">
        비상 정지를 제외한 요청은 확인 직후 동일한 메모리 내 draft에 5분·1회용 step-up grant를 결합해 전송하며,
        재시도나 오프라인 대기열에 저장하지 않습니다.
      </p>

      <div className="mt-4 rounded-md border border-line p-3">
        <KeyValue
          label="G1/G2 자격"
          value={<Pill tone={qualificationReady ? "safe" : "danger"}>{qualificationReady ? "유효" : "차단"}</Pill>}
        />
        <KeyValue label="release SHA" value={snapshot.qualification?.release_sha.slice(0, 12) ?? "-"} />
        <KeyValue label="ledger checkpoint" value={snapshot.qualification?.ledger_checkpoint ?? "-"} />
        <KeyValue label="유효 기한" value={formatKst(snapshot.qualification?.valid_until)} />
      </div>

      <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {commandActions.map((action) => {
          const qualificationBlocked = action.requiresQualification && !qualificationReady;
          const environmentBlocked = action.contractTestOnly && snapshot.runtime_health.environment !== "contract_test";
          const emergencyAuthorized =
            action.type === "emergency_stop" &&
            snapshot.access.assurance_level === "aal2" &&
            snapshot.access.actor?.roles.includes("operator") === true;
          const commandAuthorized = action.type === "emergency_stop" ? emergencyAuthorized : true;
          const disabled =
            pending || !mutationsAllowed || !canRequest || !commandAuthorized || qualificationBlocked || environmentBlocked;
          return (
            <button
              key={action.type}
              type="button"
              className={`${pageButtonClass(action.tone)} min-h-24 flex-col items-start text-left`}
              disabled={disabled}
              aria-describedby={`${action.type}-description`}
              onClick={() => {
                if (window.confirm(`${action.label}을 생성할까요? 적용 완료는 Worker ACK로 별도 확인합니다.`)) {
                  onRequest(action.type);
                }
              }}
            >
              <span className="flex items-center gap-2">
                <CommandIcon type={action.type} />
                {action.label}
              </span>
              <span id={`${action.type}-description`} className="text-xs font-normal">
                {environmentBlocked
                  ? "계약 테스트 환경에서만 사용 가능"
                  : !commandAuthorized
                    ? "최근 AAL2 operator 세션 필요"
                    : action.description}
              </span>
            </button>
          );
        })}
      </div>

      {!canRequest ? (
        <p className="mt-3 text-sm text-amber-800">현재 역할에는 request_command 권한이 없습니다.</p>
      ) : null}

      <div className="mt-6">
        <SectionTitle title="명령 처리 추적" detail={<span className="text-xs text-muted">최근 {snapshot.commands.length}건</span>} />
        {snapshot.commands.length === 0 ? (
          <p className="rounded-md border border-dashed border-line p-4 text-sm text-muted">표시할 명령 영수증이 없습니다.</p>
        ) : (
          <div className="space-y-3">
            {snapshot.commands.slice(0, 6).map((command) => (
              <CommandTimeline key={command.command_id} command={command} snapshot={snapshot} />
            ))}
          </div>
        )}
      </div>
    </Panel>
  );
}

function CommandTimeline({
  command,
  snapshot
}: {
  readonly command: OperationCommandReceipt;
  readonly snapshot: OperationsSnapshot;
}) {
  const approved = ["approved", "claimed", "applied"].includes(command.state);
  const claimed = command.worker_ack !== null;
  const applied = isCommandPostconditionVerified(command, snapshot);
  const workerReportedApplied = command.worker_ack?.state === "applied";
  const terminalFailure = ["rejected", "failed", "expired", "canceled"].includes(command.state);
  const steps = [
    { label: "요청됨", detail: "제어면 접수", done: true },
    { label: "승인됨", detail: "제어면 영수증", done: approved },
    { label: "Claim됨", detail: "Worker ACK", done: claimed },
    { label: "적용됨", detail: "Worker post-state", done: applied }
  ] as const;

  return (
    <article className="rounded-md border border-line p-3" aria-label={`${commandLabel(command.command_type)} 처리 상태`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="font-semibold text-ink">{commandLabel(command.command_type)}</p>
          <p className="text-xs text-muted">요청 {formatKst(command.requested_at)} · 만료 {formatKst(command.expires_at)}</p>
        </div>
        <Pill tone={terminalFailure ? "danger" : applied ? "safe" : "warning"}>
          {commandStateLabel(command.state, applied)}
        </Pill>
      </div>
      <ol className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
        {steps.map((step) => (
          <li
            key={step.label}
            className={`rounded border px-2 py-2 text-xs ${
              step.done ? "border-emerald-200 bg-emerald-50 text-emerald-900" : "border-line bg-slate-50 text-muted"
            }`}
          >
            <span className="block font-semibold">{step.done ? "✓ " : "○ "}{step.label}</span>
            <span>{step.detail}</span>
          </li>
        ))}
      </ol>
      <p className="mt-2 text-xs text-muted">
        제어면 receipt r{command.control_plane_receipt.revision} · Worker ACK {command.worker_ack?.state ?? "없음"}
      </p>
      {workerReportedApplied && !applied ? (
        <p className="mt-1 text-xs text-amber-800">
          Worker applied ACK는 수신했지만 fresh runtime state_version에서 postcondition을 아직 확인하지 못했습니다.
        </p>
      ) : null}
    </article>
  );
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

export function commandLabel(type: OperationCommandReceipt["command_type"]): string {
  const labels: Record<OperationCommandReceipt["command_type"], string> = {
    emergency_stop: "비상 정지",
    pause_paper: "PAPER 일시정지",
    resume_paper: "PAPER 재개",
    activate_paper_strategy: "PAPER 전략 적용",
    start_contract_test: "CONTRACT TEST 시작",
    apply_risk_policy_version: "위험 정책 적용"
  };
  return labels[type];
}

function commandStateLabel(state: OperationCommandReceipt["state"], postconditionVerified = false): string {
  const labels: Record<OperationCommandReceipt["state"], string> = {
    requested: "요청됨",
    approved: "승인됨 · 적용 전",
    claimed: "Worker claim",
    applied: postconditionVerified ? "적용·postcondition 확인" : "Worker 적용 보고 · 확인 중",
    rejected: "거절됨",
    failed: "실패",
    expired: "만료",
    canceled: "취소"
  };
  return labels[state];
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
