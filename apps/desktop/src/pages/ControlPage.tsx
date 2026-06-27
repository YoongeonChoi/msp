import { CircleStop, Lock, Power, Save } from "lucide-react";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchBotSettings, updateBotSettings } from "../lib/supabaseData";
import { formatKrw, formatRatio } from "../lib/formatters";
import { ErrorState, KeyValue, LoadingState, pageButtonClass, Panel, Pill, SectionTitle } from "../components/ui";

interface RiskForm {
  readonly maxOrderAmountKrw: string;
  readonly maxDailyLossPct: string;
  readonly maxDailyOrderCount: string;
  readonly maxPositionPct: string;
  readonly maxSectorPct: string;
}

const livePhrase = "실주문 위험을 이해했습니다";

export function ControlPage() {
  const queryClient = useQueryClient();
  const settings = useQuery({ queryKey: ["bot_settings"], queryFn: fetchBotSettings });
  const [confirmText, setConfirmText] = useState("");
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

  const updateMutation = useMutation({
    mutationFn: updateBotSettings,
    onSuccess: () => queryClient.invalidateQueries()
  });

  if (settings.isLoading) {
    return <LoadingState label="bot_settings를 불러오는 중" />;
  }
  if (settings.error) {
    return <ErrorState message="bot_settings를 읽지 못했습니다." />;
  }

  const current = settings.data;
  const canRequestLive = confirmText === livePhrase;

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_0.9fr]">
      <Panel>
        <SectionTitle title="자동매매 제어" />
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
            Paper 시작
          </button>
          <button
            className={pageButtonClass("warning")}
            disabled={updateMutation.isPending}
            onClick={() => {
              if (window.confirm("거래 loop를 정지할까요? enabled=false로 변경됩니다.")) {
                updateMutation.mutate({ enabled: false });
              }
            }}
          >
            <CircleStop size={16} aria-hidden="true" />
            Stop
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

        <div className="mt-5 rounded-md border border-line p-4">
          <SectionTitle title="현재 상태" />
          <KeyValue label="봇 상태" value={<Pill tone={current?.enabled ? "safe" : "danger"}>{current?.enabled ? "실행" : "정지"}</Pill>} />
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
            <h3 className="font-semibold">Live 전환 잠금</h3>
          </div>
          <p className="mt-2 text-sm text-red-800">
            문구를 입력해도 이 MVP Desktop은 live_order_allowed를 켜지 않습니다. 운영 전 확인 절차만 강제합니다.
          </p>
          <input
            value={confirmText}
            onChange={(event) => setConfirmText(event.currentTarget.value)}
            aria-label="실주문 확인 문구"
            className="mt-3 w-full rounded-md border border-red-200 bg-white px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-red-400"
            placeholder={livePhrase}
          />
          <button
            disabled={!canRequestLive}
            className={`${pageButtonClass("danger")} mt-3`}
            onClick={() => window.alert("Live 전환은 Desktop에서 실행되지 않습니다. 별도 release checklist와 worker feature flag가 필요합니다.")}
          >
            실주문 허용 요청
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

function RiskInput({
  label,
  value,
  onChange
}: {
  readonly label: string;
  readonly value: string;
  readonly onChange: (value: string) => void;
}) {
  return (
    <label className="block text-sm">
      <span className="font-medium text-muted">{label}</span>
      <input
        value={value}
        onChange={(event) => onChange(event.currentTarget.value)}
        className="mt-1 w-full rounded-md border border-line px-3 py-2 focus:outline-none focus:ring-2 focus:ring-slate-400"
        inputMode="decimal"
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
