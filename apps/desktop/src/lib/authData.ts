import { fetchOperationsSnapshot } from "./operationsData";
import { hasSupabaseConfig, supabase } from "./supabaseClient";

export class AuthDataError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AuthDataError";
  }
}

export interface AuthRoleState {
  readonly signedIn: boolean;
  readonly email: string | null;
  readonly role: string | null;
  readonly roles: readonly string[];
  readonly warning: string | null;
}

export interface AuthCredentials {
  readonly email: string;
  readonly password: string;
}

export const authRoleQueryKey = ["auth_role"] as const;

export function isSupabaseReady(): boolean {
  return hasSupabaseConfig && supabase !== null;
}

export async function fetchAuthRole(): Promise<AuthRoleState> {
  const client = requireClient();
  const userResult = await client.auth.getUser();
  if (userResult.error) {
    throw new AuthDataError("Supabase Auth 세션을 확인하지 못했습니다.");
  }
  if (!userResult.data.user) {
    return {
      signedIn: false,
      email: null,
      role: null,
      roles: [],
      warning: "Supabase Auth 로그인 세션이 필요합니다."
    };
  }

  const user = userResult.data.user;
  try {
    const snapshot = await fetchOperationsSnapshot();
    const roles = snapshot.access.actor?.roles ?? [];
    return {
      signedIn: true,
      email: user.email ?? null,
      role: roles[0] ?? null,
      roles,
      warning: snapshot.access.actor?.actor_id === user.id ? null : "Auth 사용자와 V1 access profile이 일치하지 않습니다."
    };
  } catch {
    return {
      signedIn: true,
      email: user.email ?? null,
      role: null,
      roles: [],
      warning: "api.get_desktop_operations_snapshot_v1 access profile을 확인할 수 없습니다."
    };
  }
}

export async function signInWithPassword(input: AuthCredentials): Promise<void> {
  const client = requireClient();
  const result = await client.auth.signInWithPassword({ email: input.email, password: input.password });
  if (result.error) {
    throw new AuthDataError("Supabase Auth 로그인에 실패했습니다.");
  }
}

export async function signOut(): Promise<void> {
  const client = requireClient();
  const result = await client.auth.signOut();
  if (result.error) {
    throw new AuthDataError("Supabase Auth 로그아웃에 실패했습니다.");
  }
}

function requireClient() {
  if (!isSupabaseReady() || supabase === null) {
    throw new AuthDataError("Supabase URL 또는 publishable key가 설정되지 않았습니다.");
  }
  return supabase;
}
