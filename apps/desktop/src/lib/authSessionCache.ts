import type { QueryClient } from "@tanstack/react-query";
import type { AuthRoleState } from "./supabaseData";
import { authRoleQueryKey } from "./useAdminAccess";

const signedOutRole: AuthRoleState = {
  signedIn: false,
  email: null,
  role: null,
  warning: "Supabase Auth 로그인 세션이 필요합니다."
};

export function resetQueryCacheAfterSignOut(queryClient: QueryClient): void {
  queryClient.removeQueries();
  queryClient.setQueryData(authRoleQueryKey, signedOutRole);
}
