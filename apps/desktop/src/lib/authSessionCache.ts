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

export function captureAuthSessionEpoch(): number {
  return authSessionEpoch;
}

export function isAuthSessionEpochCurrent(epoch: number): boolean {
  return epoch === authSessionEpoch;
}

export function resetQueryCacheAfterSignOut(queryClient: QueryClient): void {
  authSessionEpoch += 1;
  queryClient.getMutationCache().clear();
  queryClient.removeQueries();
  queryClient.setQueryData(authRoleQueryKey, signedOutRole);
}

export function shouldPurgeAuthSession(
  event: AuthChangeEvent,
  session: Session | null
): boolean {
  return event === "SIGNED_OUT" || session === null;
}

export function shouldRefreshAuthenticatedQueries(
  event: AuthChangeEvent,
  session: Session | null
): boolean {
  return session !== null && (
    event === "INITIAL_SESSION" ||
    event === "SIGNED_IN" ||
    event === "TOKEN_REFRESHED"
  );
}
