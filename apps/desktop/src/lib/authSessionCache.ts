import type { QueryClient } from "@tanstack/react-query";
import { authRoleQueryKey } from "./authData";
import type { AuthRoleState } from "./authData";

const signedOutRole: AuthRoleState = {
  signedIn: false,
  email: null,
  role: null,
  roles: [],
  warning: "Supabase Auth 로그인 세션이 필요합니다."
};

export function resetQueryCacheAfterSignOut(queryClient: QueryClient): void {
  queryClient.removeQueries();
  queryClient.setQueryData(authRoleQueryKey, signedOutRole);
}
