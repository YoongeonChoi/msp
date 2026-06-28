import assert from "node:assert/strict";

import {
  freshAcceptedLiveCommand,
  manualCommandTone,
  maxLiveEnableExpiresInMinutes,
  minLiveEnableExpiresInMinutes,
  parseLiveRequestForm
} from "../src/lib/liveApproval";
import type { ManualCommandRow } from "../src/lib/rows";

const nowMs = Date.parse("2026-06-28T00:00:00.000Z");

function command(overrides: Partial<ManualCommandRow>): ManualCommandRow {
  return {
    id: "cmd-1",
    commandType: "request_live_enable",
    status: "pending",
    payload: {
      provider_contract_version: "toss-openapi-1.1.5",
      risk_report_id: "risk-2026-06-28",
      release_version: "release-1"
    },
    requestedBy: "requester",
    reviewedBy: null,
    rejectionReason: null,
    expiresAt: new Date(nowMs + 30 * 60_000).toISOString(),
    reviewedAt: null,
    createdAt: new Date(nowMs - 60_000).toISOString(),
    appliedAt: null,
    ...overrides
  };
}

const accepted = command({
  status: "accepted",
  reviewedBy: "reviewer",
  reviewedAt: new Date(nowMs).toISOString()
});
assert.equal(freshAcceptedLiveCommand([accepted], nowMs)?.id, "cmd-1");
assert.equal(manualCommandTone(accepted, nowMs), "safe");

const applied = command({
  status: "applied",
  reviewedBy: "reviewer",
  reviewedAt: new Date(nowMs).toISOString(),
  appliedAt: new Date(nowMs + 60_000).toISOString()
});
assert.equal(freshAcceptedLiveCommand([applied], nowMs), null);
assert.equal(manualCommandTone(applied, nowMs), "neutral");

const acceptedButAppliedAtSet = command({
  status: "accepted",
  reviewedBy: "reviewer",
  reviewedAt: new Date(nowMs).toISOString(),
  appliedAt: new Date(nowMs + 60_000).toISOString()
});
assert.equal(freshAcceptedLiveCommand([acceptedButAppliedAtSet], nowMs), null);
assert.equal(manualCommandTone(acceptedButAppliedAtSet, nowMs), "warning");

const expired = command({
  status: "accepted",
  reviewedBy: "reviewer",
  reviewedAt: new Date(nowMs - 60 * 60_000).toISOString(),
  expiresAt: new Date(nowMs - 60_000).toISOString()
});
assert.equal(freshAcceptedLiveCommand([expired], nowMs), null);
assert.equal(manualCommandTone(expired, nowMs), "warning");

const selfReviewed = command({
  status: "accepted",
  reviewedBy: "requester",
  reviewedAt: new Date(nowMs).toISOString()
});
assert.equal(freshAcceptedLiveCommand([selfReviewed], nowMs), null);
assert.equal(manualCommandTone(selfReviewed, nowMs), "warning");

const rejected = command({
  status: "rejected",
  reviewedBy: "reviewer",
  rejectionReason: "operator_rejected"
});
assert.equal(manualCommandTone(rejected, nowMs), "danger");

assert.deepEqual(
  parseLiveRequestForm({
    providerContractVersion: " toss-openapi-1.1.5 ",
    riskReportId: " risk-2026-06-28 ",
    releaseVersion: " release-1 ",
    expiresInMinutes: "30"
  }),
  {
    providerContractVersion: "toss-openapi-1.1.5",
    riskReportId: "risk-2026-06-28",
    releaseVersion: "release-1",
    expiresInMinutes: 30
  }
);

assert.equal(
  parseLiveRequestForm({
    providerContractVersion: "toss-openapi-1.1.5",
    riskReportId: "",
    releaseVersion: "release-1",
    expiresInMinutes: "30"
  }),
  null
);

assert.equal(
  parseLiveRequestForm({
    providerContractVersion: "toss-openapi-1.1.5",
    riskReportId: "risk-2026-06-28",
    releaseVersion: "release-1",
    expiresInMinutes: "241"
  }),
  null
);

assert.equal(
  parseLiveRequestForm({
    providerContractVersion: "toss-openapi-1.1.5",
    riskReportId: "risk-2026-06-28",
    releaseVersion: "release-1",
    expiresInMinutes: "30.9"
  }),
  null
);

assert.equal(
  parseLiveRequestForm({
    providerContractVersion: "toss-openapi-1.1.5",
    riskReportId: "risk-2026-06-28",
    releaseVersion: "release-1",
    expiresInMinutes: String(minLiveEnableExpiresInMinutes - 1)
  }),
  null
);

assert.deepEqual(
  parseLiveRequestForm({
    providerContractVersion: "toss-openapi-1.1.5",
    riskReportId: "risk-2026-06-28",
    releaseVersion: "release-1",
    expiresInMinutes: String(maxLiveEnableExpiresInMinutes)
  }),
  {
    providerContractVersion: "toss-openapi-1.1.5",
    riskReportId: "risk-2026-06-28",
    releaseVersion: "release-1",
    expiresInMinutes: maxLiveEnableExpiresInMinutes
  }
);

console.log("liveApproval fixtures passed");
