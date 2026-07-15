import { type FormEvent, useEffect, useState } from "react";
import { KeyRound, LogIn, LogOut, ShieldCheck, UserCog } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchAuthRole, isSupabaseReady, signInWithPassword, signOut } from "../lib/authData";
import { authRoleQueryKey } from "../lib/authQueryKey";
import { resetQueryCacheAfterSignOut } from "../lib/authSessionCache";
import { useOperationsSnapshot } from "../lib/operationsSnapshotContext";
import type { DrawerState } from "../lib/uiState";
import { formatOperationsRoles } from "../lib/presentation";
import { MfaSecurityPanel } from "../components/operations/MfaSecurityPanel";
import { AccessChangePanel } from "../components/operations/AccessChangePanel";
import { DrawerSurface } from "../components/DialogSurface";
import { ErrorState, KeyValue, LoadingState, pageButtonClass, Pill } from "../components/ui";

const appVersion = "0.1.0-mvp";

export function SettingsPage() {
  const queryClient = useQueryClient();
  const { snapshot } = useOperationsSnapshot();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [drawer, setDrawer] = useState<DrawerState | null>(null);
  const role = useQuery({ queryKey: authRoleQueryKey, queryFn: fetchAuthRole, retry: false });
  const login = useMutation({
    mutationFn: signInWithPassword,
    onSuccess: async () => {
      setPassword("");
      await queryClient.invalidateQueries();
    }
  });
  const logout = useMutation({
    mutationFn: signOut,
    onSuccess: () => {
      setEmail("");
      setPassword("");
      setDrawer(null);
      resetQueryCacheAfterSignOut(queryClient);
    }
  });

  useEffect(() => {
    if (snapshot?.access.session_state === "expired" || role.data?.signedIn === false) {
      setPassword("");
      setDrawer(null);
    }
  }, [role.data?.signedIn, snapshot?.access.session_state]);

  const submitLogin = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    login.mutate({ email, password });
  };

  if (role.isLoading) {
    return <LoadingState label="계정 상태를 확인하는 중" />;
  }
  if (role.error) {
    return <ErrorState message="로그인 상태를 확인하지 못했습니다." />;
  }

  const signedIn = role.data?.signedIn === true;
  const actor = signedIn ? snapshot?.access.actor ?? null : null;
  const roles = actor?.roles ?? [];
  const platformAdmin = roles.includes("platform_admin");

  if (!signedIn) {
    return (
      <div className="mx-auto max-w-2xl space-y-4">
        <section className="surface-gradient-border rounded-xl p-6">
          <div className="mb-5">
            <p className="text-xs font-semibold text-primary">보안 로그인</p>
            <h2 className="mt-1 text-xl font-bold">운영 계정으로 로그인</h2>
            <p className="mt-2 text-sm text-mutedStrong">로그인 후에도 거래 명령에는 2단계 인증과 작업별 확인이 별도로 필요합니다.</p>
          </div>
          <form className="grid gap-4" onSubmit={submitLogin}>
            <label className="grid gap-1.5 text-sm font-medium">
              이메일
              <input
                className="rounded-md border border-controlLine bg-surface px-3 py-2"
                type="email"
                value={email}
                onChange={(event) => setEmail(event.currentTarget.value)}
                autoComplete="username"
                placeholder="operator@example.com"
              />
            </label>
            <label className="grid gap-1.5 text-sm font-medium">
              비밀번호
              <input
                className="rounded-md border border-controlLine bg-surface px-3 py-2"
                type="password"
                value={password}
                onChange={(event) => setPassword(event.currentTarget.value)}
                autoComplete="current-password"
              />
            </label>
            <button className={pageButtonClass("primary")} type="submit" disabled={!email || !password || login.isPending}>
              <LogIn size={17} aria-hidden="true" />
              {login.isPending ? "로그인 확인 중" : "로그인"}
            </button>
          </form>
          {login.error ? <p className="mt-3 text-sm text-danger" role="alert">{errorMessage(login.error)}</p> : null}
        </section>

        <details className="rounded-lg border border-line bg-surface px-4 py-2">
          <summary className="flex min-h-control cursor-pointer items-center font-semibold">연결 정보 보기</summary>
          <div className="border-t border-line py-3">
            <ConnectionDetails />
          </div>
        </details>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <section className="surface-gradient-border rounded-xl p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <p className="text-xs font-semibold text-primary">현재 계정</p>
            <h2 className="mt-1 text-xl font-bold">{role.data?.email ?? "계정 확인 불가"}</h2>
            <p className="mt-1 text-sm text-mutedStrong">역할과 2단계 인증을 확인한 뒤 필요한 작업만 여세요.</p>
          </div>
          <button className={pageButtonClass("neutral")} type="button" onClick={() => logout.mutate()} disabled={logout.isPending}>
            <LogOut size={17} aria-hidden="true" />
            로그아웃
          </button>
        </div>
        <div className="mt-5 grid gap-3 md:grid-cols-3">
          <SummaryItem label="역할" value={roles.length > 0 ? formatOperationsRoles(roles) : "확인 불가"} tone={roles.length > 0 ? "safe" : "warning"} />
          <SummaryItem label="2단계 인증" value={snapshot?.access.assurance_level === "aal2" ? "확인됨" : "추가 인증 필요"} tone={snapshot?.access.assurance_level === "aal2" ? "safe" : "warning"} />
          <SummaryItem label="세션" value={snapshot?.access.session_state === "active" ? "활성" : "만료 또는 확인 불가"} tone={snapshot?.access.session_state === "active" ? "safe" : "danger"} />
        </div>
      </section>

      <div className="grid gap-4 md:grid-cols-2">
        <button type="button" className="surface-gradient-border min-h-32 rounded-xl p-5 text-left" onClick={() => setDrawer({ kind: "mfa" })}>
          <KeyRound className="text-primary" size={22} aria-hidden="true" />
          <span className="mt-3 block text-[17px] font-bold">TOTP 관리</span>
          <span className="mt-1 block text-sm text-mutedStrong">인증 수단 등록·선택과 2단계 인증 재확인</span>
        </button>
        {platformAdmin ? (
          <button type="button" className="surface-gradient-border min-h-32 rounded-xl p-5 text-left" onClick={() => setDrawer({ kind: "access" })}>
            <UserCog className="text-primary" size={22} aria-hidden="true" />
            <span className="mt-3 block text-[17px] font-bold">접근권한 작업</span>
            <span className="mt-1 block text-sm text-mutedStrong">독립 검토가 필요한 역할 부여·회수</span>
          </button>
        ) : (
          <div className="rounded-xl border border-line bg-surface p-5">
            <ShieldCheck className="text-mutedStrong" size={22} aria-hidden="true" />
            <h2 className="mt-3 text-[17px] font-bold">접근권한</h2>
            <p className="mt-1 text-sm text-mutedStrong">현재 역할: {roles.length > 0 ? formatOperationsRoles(roles) : "확인 불가"}</p>
            <p className="mt-2 text-sm text-mutedStrong">접근권한 요청·검토는 플랫폼 관리자에게만 표시됩니다.</p>
          </div>
        )}
      </div>

      <details className="rounded-lg border border-line bg-surface px-4 py-2">
        <summary className="flex min-h-control cursor-pointer items-center font-semibold">연결 정보 보기</summary>
        <div className="border-t border-line py-3"><ConnectionDetails /></div>
      </details>

      {drawer?.kind === "mfa" ? (
        <DrawerSurface open readOnly={false} title="TOTP 관리" description="비밀값과 코드는 이 상세 화면을 닫으면 메모리에서 제거됩니다." onRequestClose={() => setDrawer(null)}>
          <MfaSecurityPanel />
        </DrawerSurface>
      ) : null}
      {drawer?.kind === "access" ? (
        <DrawerSurface open readOnly={false} title="접근권한 작업" description="대상 UUID와 증거를 확인하고 독립 검토를 요청합니다." onRequestClose={() => setDrawer(null)}>
          <AccessChangePanel />
        </DrawerSurface>
      ) : null}
    </div>
  );
}

function SummaryItem({ label, value, tone }: { readonly label: string; readonly value: string; readonly tone: "safe" | "warning" | "danger" }) {
  return <div className="rounded-lg bg-canvas p-4"><p className="text-xs text-mutedStrong">{label}</p><div className="mt-2"><Pill tone={tone}>{value}</Pill></div></div>;
}

function ConnectionDetails() {
  return (
    <div>
      <KeyValue label="연결 설정" value={<Pill tone={isSupabaseReady() ? "safe" : "danger"}>{isSupabaseReady() ? "설정됨" : "미설정"}</Pill>} />
      <KeyValue label="클라이언트 권한" value="publishable key" />
      <KeyValue label="연결 상세" value="운영 전용 연결 V1" />
      <KeyValue label="public 테이블 직접 접근" value={<Pill tone="safe">비활성화</Pill>} />
      <KeyValue label="앱 버전" value={appVersion} />
    </div>
  );
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "인증 요청이 실패했습니다.";
}
