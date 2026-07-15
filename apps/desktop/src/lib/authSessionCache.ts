import type { QueryClient } from "@tanstack/react-query";
import type { AuthChangeEvent, Session } from "@supabase/supabase-js";
import type { AuthRoleState } from "./authData";
import { authRoleQueryKey } from "./authQueryKey";

const signedOutRole: AuthRoleState = {
  signedIn: false,
  email: null,
  role: null,
  roles: [],
  warning: "운영 계정 로그인 세션이 필요합니다."
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
