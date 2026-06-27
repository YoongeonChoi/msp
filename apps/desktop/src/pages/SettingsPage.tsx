import { useQuery } from "@tanstack/react-query";
import { fetchAuthRole, fetchBotSettings, isSupabaseReady } from "../lib/supabaseData";
import { formatKst } from "../lib/formatters";
import { ErrorState, KeyValue, LoadingState, Panel, Pill, SectionTitle } from "../components/ui";

const appVersion = "0.1.0-mvp";

export function SettingsPage() {
  const settings = useQuery({ queryKey: ["bot_settings"], queryFn: fetchBotSettings, retry: false });
  const role = useQuery({ queryKey: ["auth_role"], queryFn: fetchAuthRole, retry: false });

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
    </div>
  );
}
