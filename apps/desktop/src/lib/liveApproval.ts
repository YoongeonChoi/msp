import { stringValue } from "./formatters";
import type { ManualCommandRow } from "./rows";

export interface LiveRequestForm {
  readonly providerContractVersion: string;
  readonly riskReportId: string;
  readonly releaseVersion: string;
  readonly expiresInMinutes: string;
}

export interface ParsedLiveRequest {
  readonly providerContractVersion: string;
  readonly riskReportId: string;
  readonly releaseVersion: string;
  readonly expiresInMinutes: number;
}

export type ApprovalTone = "neutral" | "safe" | "danger" | "warning";

export const minLiveEnableExpiresInMinutes = 6;
export const maxLiveEnableExpiresInMinutes = 240;

export function parseLiveRequestForm(form: LiveRequestForm): ParsedLiveRequest | null {
  const providerContractVersion = form.providerContractVersion.trim();
  const riskReportId = form.riskReportId.trim();
  const releaseVersion = form.releaseVersion.trim();
  const expiresInMinutes = Number(form.expiresInMinutes);
  if (
    providerContractVersion.length === 0 ||
    riskReportId.length === 0 ||
    releaseVersion.length === 0 ||
    !Number.isFinite(expiresInMinutes) ||
    !Number.isInteger(expiresInMinutes) ||
    expiresInMinutes < minLiveEnableExpiresInMinutes ||
    expiresInMinutes > maxLiveEnableExpiresInMinutes
  ) {
    return null;
  }
  return {
    providerContractVersion,
    riskReportId,
    releaseVersion,
    expiresInMinutes
  };
}

export function freshAcceptedLiveCommand(
  rows: readonly ManualCommandRow[],
  nowMs = Date.now()
): ManualCommandRow | null {
  return (
    rows.find((row) => {
      if (row.status !== "accepted" || !row.expiresAt || row.appliedAt) {
        return false;
      }
      if (!row.requestedBy || !row.reviewedBy || row.requestedBy === row.reviewedBy) {
        return false;
      }
      const expiresAt = new Date(row.expiresAt).getTime();
      return Number.isFinite(expiresAt) && expiresAt > nowMs;
    }) ?? null
  );
}

export function payloadValue(command: ManualCommandRow, key: string): string {
  return stringValue(command.payload[key], "-") || "-";
}

export function manualCommandTone(command: ManualCommandRow, nowMs = Date.now()): ApprovalTone {
  if (command.status === "accepted") {
    return freshAcceptedLiveCommand([command], nowMs) ? "safe" : "warning";
  }
  if (command.status === "applied") {
    return "neutral";
  }
  if (command.status === "rejected") {
    return "danger";
  }
  return "warning";
}
