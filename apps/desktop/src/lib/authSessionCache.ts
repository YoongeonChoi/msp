import type { QueryClient } from "@tanstack/react-query";
import type { AuthChangeEvent, Session } from "@supabase/supabase-js";
import type { AuthRoleState } from "./authData";
import { authRoleQueryKey } from "./authQueryKey";

const signedOutRole: AuthRoleState = {
  signedIn: false,
  email: null,
  role: null,
  roles: [],
  warning: "이 기기를 운영 계정에 연결해야 합니다."
};

let authSessionEpoch = 0;

export interface AuthSessionBoundaryDecision {
  readonly nextPrincipalId: string | null;
  readonly principalChanged: boolean;
  readonly shouldPurgeSession: boolean;
  readonly shouldRefreshQueries: boolean;
}

export function captureAuthSessionEpoch(): number {
  return authSessionEpoch;
}

export function isAuthSessionEpochCurrent(epoch: number): boolean {
  return epoch === authSessionEpoch;
}

export function resetQueryCacheAfterSignOut(queryClient: QueryClient): void {
  resetAuthenticatedQueryCache(queryClient);
}

export function resetQueryCacheAfterPrincipalChange(queryClient: QueryClient): void {
  resetAuthenticatedQueryCache(queryClient);
}

export function evaluateAuthSessionBoundary(
  previousPrincipalId: string | null,
  event: AuthChangeEvent,
  session: Session | null
): AuthSessionBoundaryDecision {
  const nextPrincipalId = authSessionPrincipalId(session);
  return {
    nextPrincipalId,
    principalChanged:
      previousPrincipalId !== null &&
      nextPrincipalId !== null &&
      previousPrincipalId !== nextPrincipalId,
    shouldPurgeSession: event === "SIGNED_OUT" || nextPrincipalId === null,
    shouldRefreshQueries:
      nextPrincipalId !== null &&
      (event === "INITIAL_SESSION" || event === "SIGNED_IN" || event === "TOKEN_REFRESHED")
  };
}

function resetAuthenticatedQueryCache(queryClient: QueryClient): void {
  authSessionEpoch += 1;
  queryClient.getMutationCache().clear();
  queryClient.removeQueries();
  queryClient.setQueryData(authRoleQueryKey, signedOutRole);
}

export function shouldPurgeAuthSession(
  event: AuthChangeEvent,
  session: Session | null
): boolean {
  return evaluateAuthSessionBoundary(null, event, session).shouldPurgeSession;
}

export function shouldRefreshAuthenticatedQueries(
  event: AuthChangeEvent,
  session: Session | null
): boolean {
  return evaluateAuthSessionBoundary(null, event, session).shouldRefreshQueries;
}

function authSessionPrincipalId(session: Session | null): string | null {
  const principalId = session?.user?.id;
  if (typeof principalId !== "string" || principalId.trim().length === 0) {
    return null;
  }
  return principalId.trim();
}
