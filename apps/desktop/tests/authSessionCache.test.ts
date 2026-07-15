import assert from "node:assert/strict";
import { QueryClient } from "@tanstack/react-query";

import {
  captureAuthSessionEpoch,
  isAuthSessionEpochCurrent,
  resetQueryCacheAfterSignOut,
  shouldPurgeAuthSession
} from "../src/lib/authSessionCache";
import type { AuthRoleState } from "../src/lib/authData";
import type { Session } from "@supabase/supabase-js";

const queryClient = new QueryClient();
queryClient.getMutationCache().build(queryClient, {
  mutationKey: ["operations", "request", "volatile"],
  mutationFn: async () => ({ requestId: "sensitive-pending-request" })
});
queryClient.setQueryData(["auth_role"], {
  signedIn: true,
  email: "admin@example.com",
  role: "admin",
  roles: ["admin"],
  warning: null
} satisfies AuthRoleState);
queryClient.setQueryData(["orders", "manual_check", "count"], 7);
queryClient.setQueryData(["orders", "manual_check", "page", 0, 25], [{ id: "sensitive-order" }]);
queryClient.setQueryData(["audit_logs", "recent"], [{ id: "sensitive-audit" }]);
queryClient.setQueryData(["operations", "snapshot", 1], { actor: "sensitive-operations-context" });

assert.equal(queryClient.getMutationCache().getAll().length, 1, "the fixture must contain an authenticated mutation");

const activeEpoch = captureAuthSessionEpoch();
assert.equal(isAuthSessionEpochCurrent(activeEpoch), true);
resetQueryCacheAfterSignOut(queryClient);

assert.equal(isAuthSessionEpochCurrent(activeEpoch), false, "sign-out must invalidate in-flight authenticated work");
assert.equal(queryClient.getMutationCache().getAll().length, 0, "sign-out must clear volatile mutation state");
assert.equal(queryClient.getQueryData(["orders", "manual_check", "count"]), undefined);
assert.equal(queryClient.getQueryData(["orders", "manual_check", "page", 0, 25]), undefined);
assert.equal(queryClient.getQueryData(["audit_logs", "recent"]), undefined);
assert.equal(queryClient.getQueryData(["operations", "snapshot", 1]), undefined);
assert.deepEqual(queryClient.getQueryData(["auth_role"]), {
  signedIn: false,
  email: null,
  role: null,
  roles: [],
  warning: "운영 계정 로그인 세션이 필요합니다."
});

const activeSession = {} as Session;
assert.equal(shouldPurgeAuthSession("SIGNED_OUT", activeSession), true);
assert.equal(shouldPurgeAuthSession("INITIAL_SESSION", null), true);
assert.equal(shouldPurgeAuthSession("TOKEN_REFRESHED", null), true);
assert.equal(shouldPurgeAuthSession("SIGNED_IN", activeSession), false);

console.log("authenticated query cache reset fixtures passed");
