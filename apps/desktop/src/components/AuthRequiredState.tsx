import { AlertTriangle } from "lucide-react";
import { Panel, Pill } from "./ui";

export function AuthRequiredState({ surface = "cockpit 데이터" }: { readonly surface?: string }) {
  return (
    <Panel className="border-amber-200 bg-amber-50">
      <AuthRequiredBlock surface={surface} />
    </Panel>
  );
}

export function AuthRequiredBlock({ surface = "cockpit 데이터" }: { readonly surface?: string }) {
  return (
    <div className="space-y-2 text-sm text-amber-900">
      <div className="flex flex-wrap items-center gap-2 font-semibold">
        <AlertTriangle size={16} aria-hidden="true" />
        <span>데이터 접근 권한 필요</span>
        <Pill tone="warning">권한 필요</Pill>
      </div>
      <p>
        {surface}는 Supabase RLS admin 세션이 있어야 표시됩니다. 거래 봇 정지와는 별개이며, Settings에서 admin 계정으로 로그인하세요.
      </p>
    </div>
  );
}
