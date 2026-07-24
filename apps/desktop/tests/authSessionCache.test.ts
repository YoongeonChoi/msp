import assert from "node:assert/strict";
import { QueryClient } from "@tanstack/react-query";

import {
  captureAuthSessionEpoch,
  evaluateAuthSessionBoundary,
  isAuthSessionEpochCurrent,
  resetQueryCacheAfterSignOut,
  resetQueryCacheAfterPrincipalChange,
  shouldPurgeAuthSession,
  shouldRefreshAuthenticatedQueries
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
  warning: "이 기기를 운영 계정에 연결해야 합니다."
});

const activeSession = sessionFor("auditor-user-id");
assert.equal(shouldPurgeAuthSession("SIGNED_OUT", activeSession), true);
assert.equal(shouldPurgeAuthSession("INITIAL_SESSION", null), true);
assert.equal(shouldPurgeAuthSession("TOKEN_REFRESHED", null), true);
assert.equal(shouldPurgeAuthSession("SIGNED_IN", activeSession), false);
assert.equal(shouldRefreshAuthenticatedQueries("INITIAL_SESSION", activeSession), true);
assert.equal(shouldRefreshAuthenticatedQueries("SIGNED_IN", activeSession), true);
assert.equal(shouldRefreshAuthenticatedQueries("TOKEN_REFRESHED", activeSession), true);
assert.equal(shouldRefreshAuthenticatedQueries("SIGNED_OUT", null), false);
assert.equal(shouldRefreshAuthenticatedQueries("INITIAL_SESSION", null), false);

const initialBoundary = evaluateAuthSessionBoundary(null, "INITIAL_SESSION", activeSession);
assert.deepEqual(initialBoundary, {
  nextPrincipalId: "auditor-user-id",
  principalChanged: false,
  shouldPurgeSession: false,
  shouldRefreshQueries: true
});
assert.equal(
  evaluateAuthSessionBoundary(initialBoundary.nextPrincipalId, "TOKEN_REFRESHED", sessionFor("auditor-user-id"))
    .principalChanged,
  false,
  "a token refresh for the same principal must preserve the authenticated cache boundary"
);

const switchedBoundary = evaluateAuthSessionBoundary(
  initialBoundary.nextPrincipalId,
  "SIGNED_IN",
  sessionFor("viewer-user-id")
);
assert.deepEqual(switchedBoundary, {
  nextPrincipalId: "viewer-user-id",
  principalChanged: true,
  shouldPurgeSession: false,
  shouldRefreshQueries: true
});

queryClient.getMutationCache().build(queryClient, {
  mutationKey: ["operations", "request", "previous-principal"],
  mutationFn: async () => ({ requestId: "previous-principal-request" })
});
queryClient.setQueryData(["audit_logs", "previous-principal"], [{ id: "previous-principal-audit" }]);
queryClient.setQueryData(["operations", "snapshot", 1], {
  actor: "previous-principal-operations-context"
});
const previousPrincipalEpoch = captureAuthSessionEpoch();
resetQueryCacheAfterPrincipalChange(queryClient);
assert.equal(
  isAuthSessionEpochCurrent(previousPrincipalEpoch),
  false,
  "a principal switch must invalidate in-flight authenticated work"
);
assert.equal(queryClient.getMutationCache().getAll().length, 0);
assert.equal(queryClient.getQueryData(["audit_logs", "previous-principal"]), undefined);
assert.equal(queryClient.getQueryData(["operations", "snapshot", 1]), undefined);
assert.deepEqual(queryClient.getQueryData(["auth_role"]), {
  signedIn: false,
  email: null,
  role: null,
  roles: [],
  warning: "이 기기를 운영 계정에 연결해야 합니다."
});

const malformedSession = { user: { id: " " } } as Session;
assert.equal(
  evaluateAuthSessionBoundary("auditor-user-id", "SIGNED_IN", malformedSession).shouldPurgeSession,
  true,
  "a session without a usable principal id must fail closed"
);

console.log("authenticated query cache reset fixtures passed");

function sessionFor(userId: string): Session {
  return { user: { id: userId } } as Session;
}
