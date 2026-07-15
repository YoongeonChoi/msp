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

export function isSupabaseReady(): boolean {
  return hasSupabaseConfig && supabase !== null;
}

export async function fetchAuthRole(): Promise<AuthRoleState> {
  const client = requireClient();
  const sessionResult = await client.auth.getSession();
  if (sessionResult.error) {
    throw new AuthDataError("로그인 세션을 확인하지 못했습니다.");
  }
  if (sessionResult.data.session === null) {
    return signedOutRoleState();
  }
  const userResult = await client.auth.getUser();
  if (userResult.error) {
    throw new AuthDataError("로그인 세션을 확인하지 못했습니다.");
  }
  if (!userResult.data.user) {
    return signedOutRoleState();
  }

  const user = userResult.data.user;
  return {
    signedIn: true,
    email: user.email ?? null,
    role: null,
    roles: [],
    warning: null
  };
}

function signedOutRoleState(): AuthRoleState {
  return {
    signedIn: false,
    email: null,
    role: null,
    roles: [],
    warning: "운영 계정 로그인 세션이 필요합니다."
  };
}

export async function signInWithPassword(input: AuthCredentials): Promise<void> {
  const client = requireClient();
  const result = await client.auth.signInWithPassword({ email: input.email, password: input.password });
  if (result.error) {
    throw new AuthDataError("운영 계정 로그인에 실패했습니다.");
  }
}

export async function signOut(): Promise<void> {
  const client = requireClient();
  const result = await client.auth.signOut();
  if (result.error) {
    throw new AuthDataError("운영 계정 로그아웃에 실패했습니다.");
  }
}

function requireClient() {
  if (!isSupabaseReady() || supabase === null) {
    throw new AuthDataError("인증 연결 정보가 설정되지 않았습니다.");
  }
  return supabase;
}
