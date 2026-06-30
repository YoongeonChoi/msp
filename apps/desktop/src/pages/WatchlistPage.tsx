import { Edit3, Plus, Save } from "lucide-react";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchWatchlist, upsertWatchlistItem } from "../lib/supabaseData";
import type { WatchlistInput } from "../lib/supabaseData";
import type { WatchlistItem } from "../lib/rows";
import { formatKrw, formatRatio } from "../lib/formatters";
import { useAdminAccess } from "../lib/useAdminAccess";
import { AuthRequiredBlock } from "../components/AuthRequiredState";
import { EmptyState, ErrorState, LoadingState, pageButtonClass, Panel, Pill, SectionTitle } from "../components/ui";

interface WatchlistForm {
  readonly symbol: string;
  readonly name: string;
  readonly market: string;
  readonly sector: string;
  readonly enabled: boolean;
  readonly targetBuyKrw: string;
  readonly targetSellKrw: string;
  readonly stopLossPct: string;
  readonly maxPositionPct: string;
  readonly notes: string;
}

const emptyForm: WatchlistForm = {
  symbol: "",
  name: "",
  market: "KR",
  sector: "unknown",
  enabled: true,
  targetBuyKrw: "",
  targetSellKrw: "",
  stopLossPct: "",
  maxPositionPct: "",
  notes: ""
};

export function WatchlistPage() {
  const queryClient = useQueryClient();
  const adminAccess = useAdminAccess();
  const watchlist = useQuery({ queryKey: ["watchlist"], queryFn: fetchWatchlist });
  const [form, setForm] = useState<WatchlistForm>(emptyForm);
  const mutation = useMutation({
    mutationFn: upsertWatchlistItem,
    onSuccess: () => {
      setForm(emptyForm);
      return queryClient.invalidateQueries({ queryKey: ["watchlist"] });
    }
  });

  if (watchlist.isLoading) {
    return <LoadingState label="관심종목을 불러오는 중" />;
  }
  if (watchlist.error) {
    return <ErrorState message="watchlist를 읽지 못했습니다." />;
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[0.9fr_1.2fr]">
      <Panel>
        <SectionTitle title="관심종목 추가/수정" detail={<Plus size={18} aria-hidden="true" />} />
        {adminAccess.isLimited ? <AuthRequiredBlock surface="watchlist 추가/수정" /> : null}
        <form
          className="mt-3 space-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            const input = parseWatchlistForm(form);
            if (!input) {
              window.alert("종목코드 6자리와 숫자 필드를 확인하세요.");
              return;
            }
            mutation.mutate(input);
          }}
        >
          <TextInput label="종목코드" value={form.symbol} maxLength={6} onChange={(value) => setForm({ ...form, symbol: value })} />
          <TextInput label="종목명" value={form.name} onChange={(value) => setForm({ ...form, name: value })} />
          <TextInput label="시장" value={form.market} onChange={(value) => setForm({ ...form, market: value.toUpperCase() })} />
          <TextInput label="섹터" value={form.sector} onChange={(value) => setForm({ ...form, sector: value })} />
          <div className="grid gap-3 sm:grid-cols-2">
            <TextInput label="목표 매수가 KRW" value={form.targetBuyKrw} onChange={(value) => setForm({ ...form, targetBuyKrw: value })} />
            <TextInput label="목표 매도가 KRW" value={form.targetSellKrw} onChange={(value) => setForm({ ...form, targetSellKrw: value })} />
            <TextInput label="손절 %" value={form.stopLossPct} onChange={(value) => setForm({ ...form, stopLossPct: value })} />
            <TextInput label="최대 비중 %" value={form.maxPositionPct} onChange={(value) => setForm({ ...form, maxPositionPct: value })} />
          </div>
          <label className="flex items-center gap-2 text-sm font-medium text-ink">
            <input
              type="checkbox"
              checked={form.enabled}
              onChange={(event) => setForm({ ...form, enabled: event.currentTarget.checked })}
              className="h-4 w-4 rounded border-line focus:ring-2 focus:ring-slate-400"
            />
            활성화
          </label>
          <label className="block text-sm">
            <span className="font-medium text-muted">메모</span>
            <textarea
              value={form.notes}
              onChange={(event) => setForm({ ...form, notes: event.currentTarget.value })}
              className="mt-1 min-h-20 w-full rounded-md border border-line px-3 py-2 focus:outline-none focus:ring-2 focus:ring-slate-400"
            />
          </label>
          <button className={pageButtonClass("neutral")} disabled={mutation.isPending}>
            <Save size={16} aria-hidden="true" />
            저장
          </button>
        </form>
      </Panel>

      <Panel>
        <SectionTitle title="관심종목 목록" />
        {(watchlist.data ?? []).length === 0 ? (
          <EmptyState title="관심종목 없음" detail="Paper Trading 전에 종목코드와 섹터를 추가하세요." />
        ) : (
          <WatchlistRows items={watchlist.data ?? []} onEdit={(item) => setForm(formFromItem(item))} />
        )}
      </Panel>
    </div>
  );
}

function WatchlistRows({
  items,
  onEdit
}: {
  readonly items: readonly WatchlistItem[];
  readonly onEdit: (item: WatchlistItem) => void;
}) {
  return (
    <>
      <div className="hidden overflow-x-auto md:block">
        <table className="min-w-full text-sm">
          <thead className="text-left text-muted">
            <tr>
              <th className="py-2">종목</th>
              <th>시장</th>
              <th>섹터</th>
              <th>상태</th>
              <th>매수/매도</th>
              <th>손절/비중</th>
              <th>수정</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {items.map((item) => (
              <tr key={item.id}>
                <td className="py-2 font-medium text-ink">
                  {item.symbol}
                  <span className="ml-2 text-muted">{item.name ?? ""}</span>
                </td>
                <td>{item.market}</td>
                <td>{item.sector}</td>
                <td>
                  <Pill tone={item.enabled ? "safe" : "neutral"}>{item.enabled ? "활성" : "비활성"}</Pill>
                </td>
                <td>{formatKrw(item.targetBuyKrw)} / {formatKrw(item.targetSellKrw)}</td>
                <td>{formatRatio(item.stopLossPct)} / {formatRatio(item.maxPositionPct)}</td>
                <td>
                  <button className={pageButtonClass("neutral")} onClick={() => onEdit(item)}>
                    <Edit3 size={14} aria-hidden="true" />
                    편집
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="grid gap-3 md:hidden">
        {items.map((item) => (
          <div key={item.id} className="rounded-md border border-line p-3">
            <div className="flex items-start justify-between gap-2">
              <div>
                <p className="font-medium text-ink">{item.symbol} {item.name ?? ""}</p>
                <p className="text-sm text-muted">{item.market} · {item.sector}</p>
              </div>
              <Pill tone={item.enabled ? "safe" : "neutral"}>{item.enabled ? "활성" : "비활성"}</Pill>
            </div>
            <p className="mt-2 text-sm text-muted">매수 {formatKrw(item.targetBuyKrw)} · 매도 {formatKrw(item.targetSellKrw)}</p>
            <button className={`${pageButtonClass("neutral")} mt-3`} onClick={() => onEdit(item)}>
              <Edit3 size={14} aria-hidden="true" />
              편집
            </button>
          </div>
        ))}
      </div>
    </>
  );
}

function TextInput({
  label,
  value,
  onChange,
  maxLength
}: {
  readonly label: string;
  readonly value: string;
  readonly onChange: (value: string) => void;
  readonly maxLength?: number;
}) {
  return (
    <label className="block text-sm">
      <span className="font-medium text-muted">{label}</span>
      <input
        value={value}
        maxLength={maxLength}
        onChange={(event) => onChange(event.currentTarget.value)}
        className="mt-1 w-full rounded-md border border-line px-3 py-2 focus:outline-none focus:ring-2 focus:ring-slate-400"
      />
    </label>
  );
}

function parseWatchlistForm(form: WatchlistForm): WatchlistInput | null {
  if (!/^[0-9]{6}$/.test(form.symbol) || form.market !== "KR") {
    return null;
  }
  return {
    symbol: form.symbol,
    name: form.name.trim() || null,
    market: form.market,
    sector: form.sector.trim() || "unknown",
    enabled: form.enabled,
    targetBuyKrw: optionalInteger(form.targetBuyKrw),
    targetSellKrw: optionalInteger(form.targetSellKrw),
    stopLossPct: optionalPercent(form.stopLossPct),
    maxPositionPct: optionalPercent(form.maxPositionPct),
    notes: form.notes.trim() || null
  };
}

function formFromItem(item: WatchlistItem): WatchlistForm {
  return {
    symbol: item.symbol,
    name: item.name ?? "",
    market: item.market,
    sector: item.sector,
    enabled: item.enabled,
    targetBuyKrw: item.targetBuyKrw === null ? "" : String(item.targetBuyKrw),
    targetSellKrw: item.targetSellKrw === null ? "" : String(item.targetSellKrw),
    stopLossPct: item.stopLossPct === null ? "" : String(item.stopLossPct * 100),
    maxPositionPct: item.maxPositionPct === null ? "" : String(item.maxPositionPct * 100),
    notes: item.notes ?? ""
  };
}

function optionalInteger(value: string): number | null {
  if (value.trim() === "") {
    return null;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.trunc(parsed) : null;
}

function optionalPercent(value: string): number | null {
  if (value.trim() === "") {
    return null;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed / 100 : null;
}
