import { useState } from "react";
import { KeyRound, ShieldCheck } from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { mfaDataApi } from "../../lib/mfa";
import type { EphemeralTotpEnrollment, MfaDataApi } from "../../lib/mfa";
import { operationsSnapshotQueryKey } from "../../lib/operationsData";
import { authRoleQueryKey } from "../../lib/authData";
import { ErrorState, KeyValue, LoadingState, pageButtonClass, Panel, Pill, SectionTitle } from "../ui";

export const mfaStatusQueryKey = ["auth", "mfa", "status"] as const;

export function MfaSecurityPanel({ dataApi = mfaDataApi }: { readonly dataApi?: MfaDataApi }) {
  const queryClient = useQueryClient();
  const status = useQuery({ queryKey: mfaStatusQueryKey, queryFn: dataApi.fetchStatus, retry: false });
  const [enrollment, setEnrollment] = useState<EphemeralTotpEnrollment | null>(null);
  const [code, setCode] = useState("");
  const [selectedFactorId, setSelectedFactorId] = useState("");
  const [pendingOperation, setPendingOperation] = useState<"enroll" | "verify" | null>(null);
  const [operationError, setOperationError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const verifiedFactors = status.data?.verifiedTotpFactors ?? [];
  const selectedFactor =
    verifiedFactors.find((factor) => factor.id === selectedFactorId) ?? verifiedFactors[0] ?? null;

  if (status.isLoading) {
    return <LoadingState label="TOTP와 AAL 상태 확인 중" />;
  }
  if (status.error || !status.data) {
    return <ErrorState message="Supabase MFA 상태를 확인하지 못했습니다. 운영 변경은 AAL2가 확인될 때까지 차단됩니다." />;
  }
  if (!status.data.signedIn) {
    return (
      <Panel>
        <SectionTitle title="운영 사용자 TOTP" />
        <p className="text-sm text-amber-900">먼저 Supabase Auth로 로그인하세요. 로그인만으로는 운영 변경 권한이 생기지 않습니다.</p>
      </Panel>
    );
  }

  const currentLevel = status.data.currentLevel;
  const nextLevel = status.data.nextLevel;

  const beginEnrollment = async () => {
    setPendingOperation("enroll");
    setOperationError(null);
    setNotice(null);
    try {
      const nextEnrollment = await dataApi.enrollTotp();
      setEnrollment(nextEnrollment);
      setCode("");
    } catch {
      setEnrollment(null);
      setOperationError("TOTP 등록을 시작하지 못했습니다. 세션과 기존 factor 상태를 확인하세요.");
    } finally {
      setPendingOperation(null);
    }
  };

  const verify = async () => {
    const factorId = enrollment?.factorId ?? selectedFactor?.id ?? "";
    if (!/^\d{6}$/.test(code) || !factorId) {
      setOperationError("인증 앱의 6자리 숫자 코드를 입력하세요.");
      return;
    }
    setPendingOperation("verify");
    setOperationError(null);
    setNotice(null);
    try {
      await dataApi.verifyTotp({ factorId, code });
      setCode("");
      setEnrollment(null);
      setNotice("TOTP로 AAL2를 다시 검증했습니다. 명령별 hash-bound step-up grant는 실제 명령 제출 직전에 별도로 발급됩니다.");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: mfaStatusQueryKey }),
        queryClient.invalidateQueries({ queryKey: operationsSnapshotQueryKey }),
        queryClient.invalidateQueries({ queryKey: authRoleQueryKey })
      ]);
    } catch {
      setOperationError("TOTP 검증에 실패했습니다. 새 6자리 코드로 다시 시도하세요.");
    } finally {
      setPendingOperation(null);
    }
  };

  return (
    <Panel className="lg:col-span-2">
      <SectionTitle
        title="운영 사용자 TOTP · AAL2"
        detail={<Pill tone={currentLevel === "aal2" ? "safe" : "danger"}>{currentLevel === "aal2" ? "운영 변경 자격 확인" : "운영 변경 차단"}</Pill>}
      />
      <div className="grid gap-3 md:grid-cols-3">
        <KeyValue label="현재 AAL" value={currentLevel?.toUpperCase() ?? "확인 불가"} />
        <KeyValue label="다음 AAL" value={nextLevel?.toUpperCase() ?? "확인 불가"} />
        <KeyValue label="검증된 TOTP" value={`${verifiedFactors.length}개`} />
      </div>
      <p className="mt-3 text-sm text-muted">
        모든 운영 사용자는 TOTP를 등록하고 AAL2를 확인해야 합니다. AAL2 재검증만으로 명령별 step-up이 완료되지는 않습니다.
      </p>

      {verifiedFactors.length === 0 && enrollment === null ? (
        <button
          type="button"
          className={`${pageButtonClass("safe")} mt-4`}
          disabled={pendingOperation !== null}
          onClick={() => void beginEnrollment()}
        >
          <KeyRound size={16} aria-hidden="true" />
          TOTP 등록 시작
        </button>
      ) : null}

      {enrollment !== null ? (
        <div className="mt-4 grid gap-4 rounded-md border border-amber-300 bg-amber-50 p-4 md:grid-cols-[220px_1fr]">
          <img
            src={enrollment.qrCodeDataUrl}
            alt="TOTP 인증 앱 등록 QR"
            className="h-[220px] w-[220px] rounded bg-white p-2"
          />
          <div className="text-sm text-amber-950">
            <p className="font-semibold">인증 앱으로 QR을 스캔하세요.</p>
            <p className="mt-2">QR에는 등록 비밀값이 포함됩니다. 화면 캡처·로그·클립보드·로컬 저장소에 남기지 마세요.</p>
            <p className="mt-2">QR과 factor ID는 이 화면의 메모리에만 유지되며 검증 성공 또는 화면 종료 시 제거됩니다.</p>
          </div>
        </div>
      ) : null}

      {verifiedFactors.length > 0 && enrollment === null ? (
        <label className="mt-4 grid max-w-md gap-1 text-sm">
          <span className="text-muted">검증할 TOTP factor</span>
          <select
            value={selectedFactor?.id ?? ""}
            onChange={(event) => setSelectedFactorId(event.currentTarget.value)}
            className="rounded-md border border-line px-3 py-2"
          >
            {verifiedFactors.map((factor, index) => (
              <option key={factor.id} value={factor.id}>
                {factor.friendlyName ?? `TOTP factor ${index + 1}`}
              </option>
            ))}
          </select>
        </label>
      ) : null}

      {(verifiedFactors.length > 0 || enrollment !== null) ? (
        <div className="mt-4 flex max-w-xl flex-wrap items-end gap-3">
          <label className="grid flex-1 gap-1 text-sm">
            <span className="text-muted">인증 앱 6자리 코드</span>
            <input
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              pattern="[0-9]{6}"
              maxLength={6}
              value={code}
              onChange={(event) => setCode(event.currentTarget.value.replace(/\D/g, "").slice(0, 6))}
              className="rounded-md border border-line px-3 py-2 font-mono tracking-[0.3em]"
              aria-label="TOTP 6자리 코드"
            />
          </label>
          <button
            type="button"
            className={pageButtonClass("safe")}
            disabled={pendingOperation !== null || !/^\d{6}$/.test(code)}
            onClick={() => void verify()}
          >
            <ShieldCheck size={16} aria-hidden="true" />
            {enrollment ? "등록 및 AAL2 검증" : "TOTP로 AAL2 재검증"}
          </button>
        </div>
      ) : null}

      {operationError ? <p className="mt-3 text-sm text-red-800" role="alert">{operationError}</p> : null}
      {notice ? <p className="mt-3 text-sm text-emerald-800" role="status">{notice}</p> : null}
    </Panel>
  );
}
