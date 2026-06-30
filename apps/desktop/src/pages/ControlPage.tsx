import { CheckCircle2, CircleStop, Lock, Power, Save, ShieldCheck, XCircle } from "lucide-react";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  fetchBotSettings,
  fetchCurrentUserId,
  fetchLiveEnableCommands,
  requestLiveEnable,
  reviewLiveEnableCommand,
  updateBotSettings
} from "../lib/supabaseData";
import { formatKrw, formatKst, formatRatio } from "../lib/formatters";
import {
  freshAcceptedLiveCommand,
  manualCommandTone,
  parseLiveRequestForm,
  payloadValue
} from "../lib/liveApproval";
import type { LiveRequestForm } from "../lib/liveApproval";
import type { ManualCommandRow } from "../lib/rows";
import { AuthRequiredState } from "../components/AuthRequiredState";
import { ErrorState, KeyValue, LoadingState, pageButtonClass, Panel, Pill, SectionTitle } from "../components/ui";

interface RiskForm {
  readonly maxOrderAmountKrw: string;
  readonly maxDailyLossPct: string;
  readonly maxDailyOrderCount: string;
  readonly maxPositionPct: string;
  readonly maxSectorPct: string;
}

const livePhrase = "실주문 위험을 이해했습니다";

const liveRequestDefaults: LiveRequestForm = {
  providerContractVersion: "toss-openapi-1.1.5",
  riskReportId: "",
  releaseVersion: "",
  expiresInMinutes: "30"
};

const controlQueryTimeoutMs = 5_000;

export interface ControlPageDataApi {
  readonly fetchBotSettings: typeof fetchBotSettings;
  readonly fetchLiveEnableCommands: typeof fetchLiveEnableCommands;
  readonly fetchCurrentUserId: typeof fetchCurrentUserId;
  readonly updateBotSettings: typeof updateBotSettings;
  readonly requestLiveEnable: typeof requestLiveEnable;
  readonly reviewLiveEnableCommand: typeof reviewLiveEnableCommand;
}

export interface ControlPageProps {
  readonly dataApi?: ControlPageDataApi;
  readonly initialConfirmText?: string;
  readonly queryTimeoutMs?: number;
}

const defaultControlPageDataApi: ControlPageDataApi = {
  fetchBotSettings,
  fetchLiveEnableCommands,
  fetchCurrentUserId,
  updateBotSettings,
  requestLiveEnable,
  reviewLiveEnableCommand
};

export function ControlPage({
  dataApi = defaultControlPageDataApi,
  initialConfirmText = "",
  queryTimeoutMs = controlQueryTimeoutMs
}: ControlPageProps = {}) {
  const queryClient = useQueryClient();
  const settings = useQuery({
    queryKey: ["bot_settings"],
    queryFn: () => withControlQueryTimeout(dataApi.fetchBotSettings(), queryTimeoutMs, "bot_settings"),
    retry: false
  });
  const liveCommands = useQuery({
    queryKey: ["manual_commands", "request_live_enable"],
    queryFn: () => withControlQueryTimeout(dataApi.fetchLiveEnableCommands(20), queryTimeoutMs, "manual_commands"),
    retry: false,
    refetchInterval: 30_000
  });
  const currentUserId = useQuery({
    queryKey: ["auth", "current_user_id"],
    queryFn: () => withControlQueryTimeout(dataApi.fetchCurrentUserId(), queryTimeoutMs, "auth"),
    retry: false
  });
  const [settingsLoadTimedOut, setSettingsLoadTimedOut] = useState(false);
  const [confirmText, setConfirmText] = useState(initialConfirmText);
  const [liveForm, setLiveForm] = useState<LiveRequestForm>(liveRequestDefaults);
  const [riskForm, setRiskForm] = useState<RiskForm>({
    maxOrderAmountKrw: "100000",
    maxDailyLossPct: "2",
    maxDailyOrderCount: "10",
    maxPositionPct: "10",
    maxSectorPct: "30"
  });

  useEffect(() => {
    if (!settings.data) {
      return;
    }
    setRiskForm({
      maxOrderAmountKrw: String(settings.data.maxOrderAmountKrw),
      maxDailyLossPct: String(settings.data.maxDailyLossPct * 100),
      maxDailyOrderCount: String(settings.data.maxDailyOrderCount),
      maxPositionPct: String(settings.data.maxPositionPct * 100),
      maxSectorPct: String(settings.data.maxSectorPct * 100)
    });
  }, [settings.data]);

  useEffect(() => {
    if (!settings.isLoading || queryTimeoutMs <= 0) {
      setSettingsLoadTimedOut(false);
      return;
    }
    const timeoutId = setTimeout(() => {
      setSettingsLoadTimedOut(true);
    }, queryTimeoutMs);
    return () => {
      clearTimeout(timeoutId);
    };
  }, [queryTimeoutMs, settings.isLoading]);

  const updateMutation = useMutation({
    mutationFn: dataApi.updateBotSettings,
    onSuccess: () => queryClient.invalidateQueries()
  });
  const requestLiveMutation = useMutation({
    mutationFn: dataApi.requestLiveEnable,
    onSuccess: () => {
      setLiveForm(liveRequestDefaults);
      return queryClient.invalidateQueries({ queryKey: ["manual_commands", "request_live_enable"] });
    }
  });
  const reviewLiveMutation = useMutation({
    mutationFn: dataApi.reviewLiveEnableCommand,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["manual_commands", "request_live_enable"] })
  });

  if (settings.isLoading && !settingsLoadTimedOut) {
    return <LoadingState label="bot_settings를 불러오는 중" />;
  }
  if (settings.error || settingsLoadTimedOut) {
    return <ErrorState message="bot_settings를 읽지 못했습니다." />;
  }
  if (!settings.data) {
    return <AuthRequiredState surface="bot_settings 제어" />;
  }

  const current = settings.data;
  const liveRows = liveCommands.data ?? [];
  const acceptedLiveCommand = freshAcceptedLiveCommand(liveRows);
  const canActivateLive = confirmText === livePhrase && acceptedLiveCommand !== null;

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_0.9fr]">
      <Panel>
        <SectionTitle title="거래 봇 제어" />
        <div className="grid gap-3 sm:grid-cols-3">
          <button
            className={pageButtonClass("safe")}
            disabled={updateMutation.isPending}
            onClick={() => {
              if (window.confirm("Paper Trading을 시작할까요? live_order_allowed=false가 유지됩니다.")) {
                updateMutation.mutate({ enabled: true, mode: "paper", liveOrderAllowed: false });
              }
            }}
          >
            <Power size={16} aria-hidden="true" />
            거래 봇 시작
          </button>
          <button
            className={pageButtonClass("warning")}
            disabled={updateMutation.isPending}
            onClick={() => {
              if (window.confirm("거래 봇을 정지할까요? enabled=false로 변경되며 데이터 조회는 계속 가능합니다.")) {
                updateMutation.mutate({ enabled: false });
              }
            }}
          >
            <CircleStop size={16} aria-hidden="true" />
            거래 봇 정지
          </button>
          <button
            className={pageButtonClass("danger")}
            disabled={updateMutation.isPending}
            onClick={() => {
              if (window.confirm("Emergency Stop을 실행할까요? 모든 live 허용 상태를 false로 되돌립니다.")) {
                updateMutation.mutate({ enabled: false, liveOrderAllowed: false });
              }
            }}
          >
            <CircleStop size={16} aria-hidden="true" />
            Emergency Stop
          </button>
        </div>
        {updateMutation.error ? (
          <p className="mt-3 text-sm text-red-700">bot_settings 변경에 실패했습니다.</p>
        ) : null}

        <div className="mt-5 rounded-md border border-line p-4">
          <SectionTitle title="현재 상태" />
          <KeyValue label="거래 봇" value={<Pill tone={current?.enabled ? "safe" : "danger"}>{current?.enabled ? "실행" : "정지"}</Pill>} />
          <KeyValue label="모드" value={<Pill tone={current?.mode === "live" ? "danger" : "safe"}>{current?.mode ?? "-"}</Pill>} />
          <KeyValue
            label="실주문 허용"
            value={<Pill tone={current?.liveOrderAllowed ? "danger" : "safe"}>{current?.liveOrderAllowed ? "예" : "아니오"}</Pill>}
          />
          <KeyValue label="최대 주문 금액" value={formatKrw(current?.maxOrderAmountKrw)} />
          <KeyValue label="최대 일 손실" value={formatRatio(current?.maxDailyLossPct)} />
        </div>

        <div className="mt-5 rounded-md border border-red-200 bg-red-50 p-4">
          <div className="flex items-center gap-2 text-red-900">
            <Lock size={18} aria-hidden="true" />
            <h3 className="font-semibold">Live 승인 게이트</h3>
          </div>
          <div className="mt-3 space-y-2 text-sm text-red-800">
            <KeyValue
              label="fresh 승인"
              value={
                <Pill tone={acceptedLiveCommand ? "safe" : "danger"}>
                  {acceptedLiveCommand ? "있음" : "없음"}
                </Pill>
              }
            />
            <KeyValue
              label="승인 만료"
              value={acceptedLiveCommand ? formatKst(acceptedLiveCommand.expiresAt) : "-"}
            />
          </div>
          <form
            className="mt-4 grid gap-3"
            onSubmit={(event) => {
              event.preventDefault();
              const parsed = parseLiveRequestForm(liveForm);
              if (!parsed) {
                window.alert("Live 승인 요청 값을 확인하세요.");
                return;
              }
              if (window.confirm("Live 승인 요청을 manual_commands에 생성할까요?")) {
                requestLiveMutation.mutate(parsed);
              }
            }}
          >
            <div className="grid gap-3 sm:grid-cols-2">
              <RiskInput
                label="provider contract"
                value={liveForm.providerContractVersion}
                onChange={(value) => setLiveForm({ ...liveForm, providerContractVersion: value })}
                inputMode="text"
              />
              <RiskInput
                label="risk report id"
                value={liveForm.riskReportId}
                onChange={(value) => setLiveForm({ ...liveForm, riskReportId: value })}
                inputMode="text"
              />
              <RiskInput
                label="release version"
                value={liveForm.releaseVersion}
                onChange={(value) => setLiveForm({ ...liveForm, releaseVersion: value })}
                inputMode="text"
              />
              <RiskInput
                label="만료 분"
                value={liveForm.expiresInMinutes}
                onChange={(value) => setLiveForm({ ...liveForm, expiresInMinutes: value })}
              />
            </div>
            <button className={pageButtonClass("warning")} disabled={requestLiveMutation.isPending}>
              <ShieldCheck size={16} aria-hidden="true" />
              Live 승인 요청
            </button>
          </form>
          {requestLiveMutation.error ? (
            <p className="mt-3 text-sm text-red-700">Live 승인 요청 저장에 실패했습니다.</p>
          ) : null}
          {reviewLiveMutation.error ? (
            <p className="mt-3 text-sm text-red-700">Live 승인 검토 저장에 실패했습니다.</p>
          ) : null}
          <div className="mt-4 space-y-2">
            {liveCommands.error ? <p className="text-sm text-red-800">manual_commands를 읽지 못했습니다.</p> : null}
            {liveRows.slice(0, 3).map((command) => (
              <LiveCommandCard
                key={command.id}
                command={command}
                currentUserId={currentUserId.data ?? null}
                isReviewPending={reviewLiveMutation.isPending}
                onAccept={() => reviewLiveMutation.mutate({ id: command.id, status: "accepted" })}
                onReject={() =>
                  reviewLiveMutation.mutate({
                    id: command.id,
                    status: "rejected",
                    rejectionReason: "operator_rejected"
                  })
                }
              />
            ))}
          </div>
          <input
            value={confirmText}
            onChange={(event) => setConfirmText(event.currentTarget.value)}
            aria-label="실주문 확인 문구"
            className="mt-3 w-full rounded-md border border-red-200 bg-white px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-red-400"
            placeholder={livePhrase}
          />
          <button
            disabled={!canActivateLive || updateMutation.isPending}
            className={`${pageButtonClass("danger")} mt-3`}
            onClick={() => {
              if (window.confirm("실주문 live mode를 활성화할까요? DB 승인 게이트가 다시 검증됩니다.")) {
                updateMutation.mutate({ enabled: true, mode: "live", liveOrderAllowed: true });
              }
            }}
          >
            실주문 허용 활성화
          </button>
        </div>
      </Panel>

      <Panel>
        <SectionTitle title="위험 한도" />
        <form
          className="space-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            const parsed = parseRiskForm(riskForm);
            if (!parsed) {
              window.alert("위험 한도 값을 확인하세요.");
              return;
            }
            if (window.confirm("위험 한도를 변경할까요? 변경 내용은 worker risk gate에 적용됩니다.")) {
              updateMutation.mutate(parsed);
            }
          }}
        >
          <RiskInput label="최대 주문 금액 KRW" value={riskForm.maxOrderAmountKrw} onChange={(value) => setRiskForm({ ...riskForm, maxOrderAmountKrw: value })} />
          <RiskInput label="최대 일 손실 %" value={riskForm.maxDailyLossPct} onChange={(value) => setRiskForm({ ...riskForm, maxDailyLossPct: value })} />
          <RiskInput label="일 주문 횟수" value={riskForm.maxDailyOrderCount} onChange={(value) => setRiskForm({ ...riskForm, maxDailyOrderCount: value })} />
          <RiskInput label="종목 비중 %" value={riskForm.maxPositionPct} onChange={(value) => setRiskForm({ ...riskForm, maxPositionPct: value })} />
          <RiskInput label="섹터 비중 %" value={riskForm.maxSectorPct} onChange={(value) => setRiskForm({ ...riskForm, maxSectorPct: value })} />
          <button className={pageButtonClass("neutral")} disabled={updateMutation.isPending}>
            <Save size={16} aria-hidden="true" />
            저장
          </button>
        </form>
      </Panel>
    </div>
  );
}

function withControlQueryTimeout<T>(promise: Promise<T>, timeoutMs: number, label: string): Promise<T> {
  if (timeoutMs <= 0) {
    return promise;
  }
  let timeoutId: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timeoutId = setTimeout(() => {
      reject(new Error(`${label} query timed out after ${timeoutMs}ms`));
    }, timeoutMs);
  });
  return Promise.race([promise, timeout]).finally(() => {
    if (timeoutId !== undefined) {
      clearTimeout(timeoutId);
    }
  });
}

function LiveCommandCard({
  command,
  currentUserId,
  isReviewPending,
  onAccept,
  onReject
}: {
  readonly command: ManualCommandRow;
  readonly currentUserId: string | null;
  readonly isReviewPending: boolean;
  readonly onAccept: () => void;
  readonly onReject: () => void;
}) {
  const isOwnRequest = currentUserId !== null && command.requestedBy === currentUserId;
  const acceptDisabled = isReviewPending || isOwnRequest;

  return (
    <div className="rounded-md border border-red-200 bg-white p-3 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Pill tone={manualCommandTone(command)}>{command.status}</Pill>
        <span className="text-muted">만료 {formatKst(command.expiresAt)}</span>
      </div>
      <p className="mt-2 text-muted">
        {payloadValue(command, "release_version")} · {payloadValue(command, "risk_report_id")}
      </p>
      {isOwnRequest ? <p className="mt-2 text-xs text-red-700">본인 요청은 다른 admin 승인 필요</p> : null}
      {command.status === "pending" ? (
        <div className="mt-3 flex flex-wrap gap-2">
          <button
            className={pageButtonClass("safe")}
            disabled={acceptDisabled}
            onClick={() => {
              if (window.confirm("이 Live 승인 요청을 승인할까요?")) {
                onAccept();
              }
            }}
          >
            <CheckCircle2 size={15} aria-hidden="true" />
            승인
          </button>
          <button
            className={pageButtonClass("danger")}
            disabled={isReviewPending}
            onClick={() => {
              if (window.confirm("이 Live 승인 요청을 거절할까요?")) {
                onReject();
              }
            }}
          >
            <XCircle size={15} aria-hidden="true" />
            거절
          </button>
        </div>
      ) : null}
    </div>
  );
}

function RiskInput({
  label,
  value,
  onChange,
  inputMode = "decimal"
}: {
  readonly label: string;
  readonly value: string;
  readonly onChange: (value: string) => void;
  readonly inputMode?: "decimal" | "numeric" | "text";
}) {
  return (
    <label className="block text-sm">
      <span className="font-medium text-muted">{label}</span>
      <input
        value={value}
        onChange={(event) => onChange(event.currentTarget.value)}
        className="mt-1 w-full rounded-md border border-line px-3 py-2 focus:outline-none focus:ring-2 focus:ring-slate-400"
        inputMode={inputMode}
      />
    </label>
  );
}

function parseRiskForm(form: RiskForm) {
  const maxOrderAmountKrw = Number(form.maxOrderAmountKrw);
  const maxDailyLossPct = Number(form.maxDailyLossPct) / 100;
  const maxDailyOrderCount = Number(form.maxDailyOrderCount);
  const maxPositionPct = Number(form.maxPositionPct) / 100;
  const maxSectorPct = Number(form.maxSectorPct) / 100;
  if (
    !Number.isFinite(maxOrderAmountKrw) ||
    !Number.isFinite(maxDailyLossPct) ||
    !Number.isFinite(maxDailyOrderCount) ||
    !Number.isFinite(maxPositionPct) ||
    !Number.isFinite(maxSectorPct)
  ) {
    return null;
  }
  return {
    maxOrderAmountKrw: Math.trunc(maxOrderAmountKrw),
    maxDailyLossPct,
    maxDailyOrderCount: Math.trunc(maxDailyOrderCount),
    maxPositionPct,
    maxSectorPct
  };
}
