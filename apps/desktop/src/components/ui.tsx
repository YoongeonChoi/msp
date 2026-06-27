import type { ReactNode } from "react";
import { AlertTriangle, Loader2 } from "lucide-react";
import { summarizeJson } from "../lib/formatters";

export type Tone = "neutral" | "safe" | "danger" | "warning" | "info";

const toneClasses: Record<Tone, string> = {
  neutral: "border-line bg-white text-ink",
  safe: "border-emerald-200 bg-emerald-50 text-emerald-800",
  danger: "border-red-200 bg-red-50 text-red-800",
  warning: "border-amber-200 bg-amber-50 text-amber-800",
  info: "border-sky-200 bg-sky-50 text-sky-800"
};

export function Pill({ children, tone = "neutral" }: { readonly children: ReactNode; readonly tone?: Tone }) {
  return (
    <span className={`inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs font-medium ${toneClasses[tone]}`}>
      {children}
    </span>
  );
}

export function Panel({ children, className = "" }: { readonly children: ReactNode; readonly className?: string }) {
  return <section className={`rounded-md border border-line bg-white p-4 ${className}`}>{children}</section>;
}

export function Metric({
  title,
  value,
  detail,
  tone = "neutral"
}: {
  readonly title: string;
  readonly value: string;
  readonly detail: string;
  readonly tone?: Tone;
}) {
  return (
    <Panel>
      <div className="flex items-start justify-between gap-3">
        <p className="text-sm font-medium text-muted">{title}</p>
        <Pill tone={tone}>{detail}</Pill>
      </div>
      <p className="mt-3 text-2xl font-semibold text-ink">{value}</p>
    </Panel>
  );
}

export function SectionTitle({
  title,
  detail
}: {
  readonly title: string;
  readonly detail?: ReactNode;
}) {
  return (
    <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
      <h2 className="text-base font-semibold text-ink">{title}</h2>
      {detail}
    </div>
  );
}

export function LoadingState({ label = "불러오는 중" }: { readonly label?: string }) {
  return (
    <div className="flex min-h-28 items-center justify-center rounded-md border border-dashed border-line bg-slate-50 p-4 text-sm text-muted">
      <Loader2 className="mr-2 animate-spin" size={16} aria-hidden="true" />
      {label}
    </div>
  );
}

export function EmptyState({
  title,
  detail
}: {
  readonly title: string;
  readonly detail: string;
}) {
  return (
    <div className="rounded-md border border-dashed border-line bg-slate-50 p-6 text-center">
      <p className="font-medium text-ink">{title}</p>
      <p className="mt-1 text-sm text-muted">{detail}</p>
    </div>
  );
}

export function ErrorState({ message }: { readonly message: string }) {
  return (
    <div className="rounded-md border border-red-200 bg-red-50 p-4 text-sm text-red-800">
      <div className="flex items-center gap-2 font-semibold">
        <AlertTriangle size={16} aria-hidden="true" />
        데이터 접근 오류
      </div>
      <p className="mt-1">{message}</p>
      <p className="mt-1">Supabase 로그인, admin role, RLS policy를 확인하세요.</p>
    </div>
  );
}

export function KeyValue({
  label,
  value
}: {
  readonly label: string;
  readonly value: ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-line py-2 text-sm last:border-b-0">
      <span className="text-muted">{label}</span>
      <span className="text-right font-medium text-ink">{value}</span>
    </div>
  );
}

export function JsonSummary({ value }: { readonly value: unknown }) {
  return <span className="text-xs text-muted">{summarizeJson(value)}</span>;
}

export function pageButtonClass(tone: Tone = "neutral"): string {
  const base =
    "inline-flex items-center justify-center gap-2 rounded-md border px-4 py-2 text-sm font-semibold focus:outline-none focus:ring-2 focus:ring-offset-1 disabled:cursor-not-allowed disabled:opacity-60";
  if (tone === "danger") {
    return `${base} border-red-200 bg-red-50 text-red-800 focus:ring-red-400`;
  }
  if (tone === "safe") {
    return `${base} border-emerald-200 bg-emerald-50 text-emerald-800 focus:ring-emerald-400`;
  }
  if (tone === "warning") {
    return `${base} border-amber-200 bg-amber-50 text-amber-800 focus:ring-amber-400`;
  }
  return `${base} border-line bg-white text-ink focus:ring-slate-400`;
}
