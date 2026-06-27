export type JsonRecord = Record<string, unknown>;

export function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function stringValue(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

export function nullableString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

export function booleanValue(value: unknown, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}

export function numberValue(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string" && value.trim().length > 0) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

export function integerValue(value: unknown): number | null {
  const parsed = numberValue(value);
  return parsed === null ? null : Math.trunc(parsed);
}

export function recordValue(value: unknown): JsonRecord {
  return isRecord(value) ? value : {};
}

export function formatKst(value: string | null | undefined): string {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "-";
  }
  return new Intl.DateTimeFormat("ko-KR", {
    timeZone: "Asia/Seoul",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  }).format(date);
}

export function formatKrw(value: number | null | undefined): string {
  if (value === null || value === undefined) {
    return "-";
  }
  return `${new Intl.NumberFormat("ko-KR").format(value)}원`;
}

export function formatNumber(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) {
    return "-";
  }
  return value.toLocaleString("ko-KR", {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits
  });
}

export function formatRatio(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) {
    return "-";
  }
  return `${(value * 100).toLocaleString("ko-KR", {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits
  })}%`;
}

export function formatAge(value: string | null | undefined): string {
  if (!value) {
    return "없음";
  }
  const timestamp = new Date(value).getTime();
  if (Number.isNaN(timestamp)) {
    return "알 수 없음";
  }
  const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
  if (seconds < 60) {
    return `${seconds}초 전`;
  }
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return `${minutes}분 전`;
  }
  return `${Math.floor(minutes / 60)}시간 전`;
}

export function isOlderThan(value: string | null | undefined, seconds: number): boolean {
  if (!value) {
    return true;
  }
  const timestamp = new Date(value).getTime();
  return Number.isNaN(timestamp) || Date.now() - timestamp > seconds * 1000;
}

export function startOfKstTodayIso(): string {
  const kstOffsetMs = 9 * 60 * 60 * 1000;
  const nowKst = new Date(Date.now() + kstOffsetMs);
  const startKstUtcMs =
    Date.UTC(nowKst.getUTCFullYear(), nowKst.getUTCMonth(), nowKst.getUTCDate()) - kstOffsetMs;
  return new Date(startKstUtcMs).toISOString();
}

export function summarizeJson(value: unknown, maxLength = 120): string {
  if (typeof value === "string") {
    return value.length > maxLength ? `${value.slice(0, maxLength)}...` : value;
  }
  if (!isRecord(value)) {
    return "-";
  }
  const entries = Object.entries(value)
    .filter(([, entryValue]) => entryValue !== null && entryValue !== undefined)
    .slice(0, 4)
    .map(([key, entryValue]) => `${key}: ${stringifyShort(entryValue)}`);
  const summary = entries.join(" / ");
  return summary.length > maxLength ? `${summary.slice(0, maxLength)}...` : summary || "-";
}

export function nestedNumber(value: unknown, keys: readonly string[]): number | null {
  let current: unknown = value;
  for (const key of keys) {
    if (!isRecord(current)) {
      return null;
    }
    current = current[key];
  }
  return numberValue(current);
}

function stringifyShort(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  if (Array.isArray(value)) {
    return `[${value.length}]`;
  }
  if (isRecord(value)) {
    return "{...}";
  }
  return "-";
}
