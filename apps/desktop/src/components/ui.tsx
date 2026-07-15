import type { ReactNode } from "react";
import { AlertTriangle, Loader2 } from "lucide-react";
import clsx from "clsx";
import { summarizeJson } from "../lib/formatters";

export type Tone = "neutral" | "safe" | "danger" | "warning" | "info";
export type ButtonTone = "primary" | "neutral" | "warning" | "danger";

const toneClasses: Record<Tone, string> = {
  neutral: "border-line bg-surface text-ink",
  safe: "border-success/25 bg-successSoft text-success",
  danger: "border-danger/25 bg-dangerSoft text-danger",
  warning: "border-warning/25 bg-warningSoft text-warning",
  info: "border-primary/25 bg-primarySoft text-primary"
};

const buttonToneClasses: Record<ButtonTone, string> = {
  primary: "border-primary bg-primary text-white hover:bg-blue-700 active:scale-[0.98] motion-reduce:transform-none",
  neutral: "border-controlLine bg-surface text-ink hover:bg-canvas active:scale-[0.98] motion-reduce:transform-none",
  warning: "border-warning bg-warningSoft text-warning hover:bg-amber-100 active:scale-[0.98] motion-reduce:transform-none",
  danger: "border-danger bg-danger text-white hover:bg-red-800 active:scale-[0.98] motion-reduce:transform-none"
};

export function Pill({ children, tone = "neutral" }: { readonly children: ReactNode; readonly tone?: Tone }) {
  return (
    <span className={clsx("inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-xs font-medium", toneClasses[tone])}>
      {children}
    </span>
  );
}

export function Panel({ children, className = "" }: { readonly children: ReactNode; readonly className?: string }) {
  return <section className={clsx("surface-gradient-border rounded-xl p-5", className)}>{children}</section>;
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
        <p className="text-sm font-medium text-mutedStrong">{title}</p>
        <Pill tone={tone}>{detail}</Pill>
      </div>
      <p className="mt-3 break-words text-2xl font-semibold text-ink">{value}</p>
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
      <h2 className="text-[17px] font-semibold leading-[26px] text-ink">{title}</h2>
      {detail}
    </div>
  );
}

export function LoadingState({ label = "불러오는 중" }: { readonly label?: string }) {
  return (
    <div className="flex min-h-28 items-center justify-center rounded-lg border border-dashed border-line bg-canvas p-4 text-sm text-mutedStrong">
      <Loader2 className="mr-2 animate-spin motion-reduce:animate-none" size={16} aria-hidden="true" />
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
    <div className="rounded-lg border border-dashed border-line bg-canvas p-6 text-center">
      <p className="font-medium text-ink">{title}</p>
      <p className="mt-1 text-sm text-mutedStrong">{detail}</p>
    </div>
  );
}

export function ErrorState({ message }: { readonly message: string }) {
  return (
    <div className="rounded-lg border border-danger/30 bg-dangerSoft p-4 text-sm text-danger" role="alert">
      <div className="flex items-center gap-2 font-semibold">
        <AlertTriangle size={16} aria-hidden="true" />
        데이터 접근 오류
      </div>
      <p className="mt-1">{message}</p>
      <p className="mt-1">로그인 상태, 현재 계정 권한과 연결 정책을 확인하세요.</p>
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
    <div className="grid min-w-0 grid-cols-[minmax(7rem,0.65fr)_minmax(0,1fr)] items-start gap-3 border-b border-line py-2 text-sm last:border-b-0">
      <span className="text-mutedStrong">{label}</span>
      <span className="min-w-0 overflow-wrap-anywhere text-right font-medium text-ink [overflow-wrap:anywhere]">{value}</span>
    </div>
  );
}

export function JsonSummary({ value }: { readonly value: unknown }) {
  return <span className="text-xs text-muted">{summarizeJson(value)}</span>;
}

export function pageButtonClass(tone: Tone | ButtonTone = "neutral"): string {
  const resolvedTone: ButtonTone = tone === "safe" || tone === "info" ? "primary" : tone;
  return clsx(
    "inline-flex min-h-control min-w-control items-center justify-center gap-2 rounded-md border px-4 py-2 text-sm font-semibold",
    "transition-[transform,opacity] duration-press ease-product",
    "disabled:cursor-not-allowed disabled:opacity-50 disabled:active:scale-100",
    buttonToneClasses[resolvedTone]
  );
}
