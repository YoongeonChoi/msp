import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { QueryClient } from "@tanstack/react-query";

import { resetQueryCacheAfterSignOut } from "../src/lib/authSessionCache";
import type { AuthRoleState } from "../src/lib/supabaseData";

const queryClient = new QueryClient();
queryClient.setQueryData(["auth_role"], {
  signedIn: true,
  email: "admin@example.com",
  role: "admin",
  warning: null
} satisfies AuthRoleState);
queryClient.setQueryData(["orders", "manual_check", "count"], 7);
queryClient.setQueryData(["orders", "manual_check", "page", 0, 25], [{ id: "sensitive-order" }]);
queryClient.setQueryData(["audit_logs", "recent"], [{ id: "sensitive-audit" }]);

resetQueryCacheAfterSignOut(queryClient);

assert.equal(queryClient.getQueryData(["orders", "manual_check", "count"]), undefined);
assert.equal(queryClient.getQueryData(["orders", "manual_check", "page", 0, 25]), undefined);
assert.equal(queryClient.getQueryData(["audit_logs", "recent"]), undefined);
assert.deepEqual(queryClient.getQueryData(["auth_role"]), {
  signedIn: false,
  email: null,
  role: null,
  warning: "Supabase Auth 로그인 세션이 필요합니다."
});

const testDir = dirname(fileURLToPath(import.meta.url));
for (const relativePath of [
  "../src/components/Layout.tsx",
  "../src/pages/DashboardPage.tsx",
  "../src/pages/OrdersPage.tsx",
  "../src/pages/LogsPage.tsx"
]) {
  const source = readFileSync(resolve(testDir, relativePath), "utf8");
  assert.match(source, /enabled: adminAccess\.isAdmin/, `${relativePath} must gate admin-only queries`);
}

console.log("authenticated query cache reset fixtures passed");
