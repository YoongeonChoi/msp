import { useQuery } from "@tanstack/react-query";
import { fetchAuthRole } from "./supabaseData";
import type { AuthRoleState } from "./supabaseData";

export const authRoleQueryKey = ["auth_role"] as const;

export interface AdminAccessState {
  readonly isKnown: boolean;
  readonly isAdmin: boolean;
  readonly isLimited: boolean;
  readonly warning: string | null;
  readonly role: AuthRoleState | null;
}

export function useAdminAccess(): AdminAccessState {
  const role = useQuery({
    queryKey: authRoleQueryKey,
    queryFn: fetchAuthRole,
    retry: false,
    refetchInterval: 60_000
  });
  return adminAccessFromRole(role.data ?? null);
}

export function adminAccessFromRole(role: AuthRoleState | null): AdminAccessState {
  const isKnown = role !== null;
  const isAdmin = role?.role === "admin";
  return {
    isKnown,
    isAdmin,
    isLimited: isKnown && !isAdmin,
    warning: role?.warning ?? null,
    role
  };
}
