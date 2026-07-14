import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { mapAuditLog } from "../src/lib/rows";
import { LogsPage } from "../src/pages/LogsPage";
import type { AuthRoleState } from "../src/lib/supabaseData";
import type { AuditLogRow } from "../src/lib/rows";

const targetId = "87654321-4321-4321-4321-210987654321";

const mapped = mapAuditLog({
  id: "audit-1",
  action: "update",
  target_table: "bot_settings",
  target_id: targetId,
  changed_fields: ["enabled", "notes"],
  created_at: "2026-07-14T00:00:00.000Z"
});

const auditRow: AuditLogRow = mapped;
const adminRole: AuthRoleState = {
  signedIn: true,
  email: "admin@example.com",
  role: "admin",
  warning: null
};
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
queryClient.setQueryData(["auth_role"], adminRole);
queryClient.setQueryData(["audit_logs", "recent"], [auditRow]);
queryClient.setQueryData(["engine_events"], []);

const markup = renderToStaticMarkup(
  <QueryClientProvider client={queryClient}>
    <LogsPage />
  </QueryClientProvider>
);
const text = new JSDOM(markup).window.document.body.textContent ?? "";
assert.match(text, /변경 감사 로그/);
assert.match(text, /거래 봇 설정/);
assert.match(text, /거래 봇 실행/);
assert.match(text, /notes/);
assert.match(text, /87654321…/, "target identifiers should be abbreviated");
assert.doesNotMatch(text, new RegExp(targetId), "full target UUID must not be rendered");

const testDir = dirname(fileURLToPath(import.meta.url));
const dataSource = readFileSync(resolve(testDir, "../src/lib/supabaseData.ts"), "utf8");
const fetchStart = dataSource.indexOf("export async function fetchAuditLogs");
const fetchEnd = dataSource.indexOf("\nexport async function", fetchStart + 1);
assert.ok(fetchStart >= 0, "fetchAuditLogs source must be present");
const fetchSource = dataSource.slice(fetchStart, fetchEnd < 0 ? dataSource.length : fetchEnd);
assert.match(fetchSource, /\.rpc\("get_audit_log_summaries"/, "audit query must use the safe summary RPC");
assert.doesNotMatch(fetchSource, /\.from\("audit_logs"\)/, "desktop must not query raw audit rows");
assert.doesNotMatch(fetchSource, /before_snapshot|after_snapshot|actor_user_id/, "desktop must not request sensitive audit fields");

console.log("audit log render fixtures passed");
