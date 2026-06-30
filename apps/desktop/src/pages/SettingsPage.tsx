import { type FormEvent, useState } from "react";
import { LogIn, LogOut } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchAuthRole, fetchBotSettings, isSupabaseReady, signInWithPassword, signOut } from "../lib/supabaseData";
import { formatKst } from "../lib/formatters";
import { ErrorState, KeyValue, LoadingState, pageButtonClass, Panel, Pill, SectionTitle } from "../components/ui";

const appVersion = "0.1.0-mvp";

export function SettingsPage() {
  const queryClient = useQueryClient();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const settings = useQuery({ queryKey: ["bot_settings"], queryFn: fetchBotSettings, retry: false });
  const role = useQuery({ queryKey: ["auth_role"], queryFn: fetchAuthRole, retry: false });
  const login = useMutation({
    mutationFn: signInWithPassword,
    onSuccess: async () => {
      setPassword("");
      await queryClient.invalidateQueries();
    }
  });
  const logout = useMutation({
    mutationFn: signOut,
    onSuccess: async () => {
      await queryClient.invalidateQueries();
    }
  });

  const submitLogin = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    login.mutate({ email, password });
  };

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Panel>
        <SectionTitle title="Supabase 연결" />
        <KeyValue label="연결 설정" value={<Pill tone={isSupabaseReady() ? "safe" : "danger"}>{isSupabaseReady() ? "설정됨" : "미설정"}</Pill>} />
        <KeyValue label="클라이언트 권한" value="publishable key" />
        <KeyValue label="bot_settings 접근" value={<Pill tone={settings.error ? "danger" : "safe"}>{settings.error ? "실패" : "가능"}</Pill>} />
        <KeyValue label="마지막 설정 변경" value={formatKst(settings.data?.updatedAt)} />
        <KeyValue label="앱 버전" value={appVersion} />
      </Panel>

      <Panel>
        <SectionTitle title="사용자 권한" />
        {role.isLoading ? <LoadingState label="사용자 권한 확인 중" /> : null}
        {role.error ? <ErrorState message="Supabase Auth 상태를 확인하지 못했습니다." /> : null}
        {role.data ? (
          <>
            <KeyValue label="로그인" value={<Pill tone={role.data.signedIn ? "safe" : "warning"}>{role.data.signedIn ? "예" : "아니오"}</Pill>} />
            <KeyValue label="계정" value={role.data.email ?? "-"} />
            <KeyValue label="role" value={<Pill tone={role.data.role === "admin" ? "safe" : "warning"}>{role.data.role ?? "읽기 불가"}</Pill>} />
            {role.data.warning ? (
              <div className="mt-3 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
                {role.data.warning}
              </div>
            ) : null}
          </>
        ) : null}
      </Panel>

      <Panel className="lg:col-span-2">
        <SectionTitle title="Supabase Auth 로그인" />
        <form className="grid gap-3 md:grid-cols-[1fr_1fr_auto_auto]" onSubmit={submitLogin}>
          <label className="grid gap-1 text-sm">
            <span className="text-muted">이메일</span>
            <input
              className="rounded-md border border-line px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-slate-400"
              type="email"
              value={email}
              onChange={(event) => setEmail(event.currentTarget.value)}
              autoComplete="username"
              placeholder="admin@example.com"
            />
          </label>
          <label className="grid gap-1 text-sm">
            <span className="text-muted">비밀번호</span>
            <input
              className="rounded-md border border-line px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-slate-400"
              type="password"
              value={password}
              onChange={(event) => setPassword(event.currentTarget.value)}
              autoComplete="current-password"
            />
          </label>
          <div className="flex items-end">
            <button className={pageButtonClass("safe")} type="submit" disabled={!email || !password || login.isPending}>
              <LogIn size={16} aria-hidden="true" />
              로그인
            </button>
          </div>
          <div className="flex items-end">
            <button
              className={pageButtonClass()}
              type="button"
              onClick={() => logout.mutate()}
              disabled={!role.data?.signedIn || logout.isPending}
            >
              <LogOut size={16} aria-hidden="true" />
              로그아웃
            </button>
          </div>
        </form>
        {login.error || logout.error ? (
          <div className="mt-3 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
            {errorMessage(login.error ?? logout.error)}
          </div>
        ) : null}
      </Panel>
    </div>
  );
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Supabase Auth 요청이 실패했습니다.";
}
