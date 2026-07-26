import { AlertTriangle } from "lucide-react";
import { Panel, Pill } from "./ui";

export function AuthRequiredState({ surface = "운영 데이터" }: { readonly surface?: string }) {
  return (
    <Panel className="!border-warning/30 !bg-warningSoft">
      <AuthRequiredBlock surface={surface} />
    </Panel>
  );
}

export function AuthRequiredBlock({ surface = "운영 데이터" }: { readonly surface?: string }) {
  return (
    <div className="space-y-2 text-sm text-warning">
      <div className="flex flex-wrap items-center gap-2 font-semibold">
        <AlertTriangle size={16} aria-hidden="true" />
        <span>데이터 접근 권한 필요</span>
        <Pill tone="warning">권한 필요</Pill>
      </div>
      <p>
        {surface}는 운영 관리자 세션이 있어야 표시됩니다. 거래 봇 정지와는 별개이며, 계정·보안에서 이 기기를 운영 계정에 연결하세요.
      </p>
    </div>
  );
}
