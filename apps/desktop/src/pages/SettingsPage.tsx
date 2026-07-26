import { type FormEvent, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { AlertTriangle, KeyRound, Link2, RotateCcw, ShieldCheck, Unplug, UserCog } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  discardPendingDeviceConnection,
  fetchAuthRole,
  getDeviceConnectionCleanupError,
  getDeviceConnectionGuardSnapshot,
  isSupabaseReady,
  retryDeviceConnectionCleanup,
  signInWithPassword,
  signOut,
  subscribeDeviceConnectionGuard,
  type AuthCredentials,
  type AuthRoleState,
  type DeviceConnectionGuardSnapshot,
  type DeviceConnectionResult
} from "../lib/authData";
import { authRoleQueryKey } from "../lib/authQueryKey";
import { resetQueryCacheAfterSignOut } from "../lib/authSessionCache";
import { useOperationsSnapshot } from "../lib/operationsSnapshotContext";
import type { DrawerState } from "../lib/uiState";
import { formatOperationsRoles } from "../lib/presentation";
import { MfaSecurityPanel } from "../components/operations/MfaSecurityPanel";
import { AccessChangePanel } from "../components/operations/AccessChangePanel";
import { ConfirmDialog, DrawerSurface } from "../components/DialogSurface";
import { ErrorState, KeyValue, LoadingState, pageButtonClass, Pill } from "../components/ui";

const appVersion = "0.1.0-mvp";
let pendingSettingsFocus: "connected" | "disconnected" | null = null;

export interface SettingsAuthApi {
  readonly isReady: () => boolean;
  readonly fetchRole: () => Promise<AuthRoleState>;
  readonly connectDevice: (input: AuthCredentials) => Promise<DeviceConnectionResult>;
  readonly discardPendingConnection: () => void;
  readonly getCleanupError: () => Error | null;
  readonly subscribeGuard: (listener: () => void) => () => void;
  readonly getGuardSnapshot: () => DeviceConnectionGuardSnapshot;
  readonly retryCleanup: () => Promise<void>;
  readonly disconnectDevice: () => Promise<void>;
}

const defaultSettingsAuthApi: SettingsAuthApi = {
  isReady: isSupabaseReady,
  fetchRole: fetchAuthRole,
  connectDevice: signInWithPassword,
  discardPendingConnection: discardPendingDeviceConnection,
  getCleanupError: getDeviceConnectionCleanupError,
  subscribeGuard: subscribeDeviceConnectionGuard,
  getGuardSnapshot: getDeviceConnectionGuardSnapshot,
  retryCleanup: retryDeviceConnectionCleanup,
  disconnectDevice: signOut
};

export function SettingsPage({ authApi = defaultSettingsAuthApi }: { readonly authApi?: SettingsAuthApi }) {
  const queryClient = useQueryClient();
  const { snapshot } = useOperationsSnapshot();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [deviceConnectionOpen, setDeviceConnectionOpen] = useState(false);
  const [deviceConnectionPending, setDeviceConnectionPending] = useState(false);
  const [deviceConnectionError, setDeviceConnectionError] = useState<Error | null>(() => authApi.getCleanupError());
  const [disconnectConfirmOpen, setDisconnectConfirmOpen] = useState(false);
  const [drawer, setDrawer] = useState<DrawerState | null>(null);
  const [focusDestination, setFocusDestination] = useState<"connected" | "disconnected" | null>(
    () => pendingSettingsFocus
  );
  const connectionSubmittingRef = useRef(false);
  const connectionAttemptRef = useRef(0);
  const mountedRef = useRef(true);
  const connectedHeadingRef = useRef<HTMLHeadingElement>(null);
  const connectButtonRef = useRef<HTMLButtonElement>(null);
  const supabaseReady = authApi.isReady();
  const deviceConnectionGuard = useSyncExternalStore(
    authApi.subscribeGuard,
    authApi.getGuardSnapshot,
    authApi.getGuardSnapshot
  );
  const role = useQuery({
    queryKey: authRoleQueryKey,
    queryFn: authApi.fetchRole,
    enabled: supabaseReady,
    retry: false
  });
  const requestSettingsFocus = (destination: "connected" | "disconnected") => {
    pendingSettingsFocus = destination;
    setFocusDestination(destination);
  };
  const disconnectDevice = useMutation({
    mutationFn: authApi.disconnectDevice,
    onSuccess: () => {
      requestSettingsFocus("disconnected");
      setEmail("");
      setPassword("");
      setDeviceConnectionOpen(false);
      setDeviceConnectionError(authApi.getCleanupError());
      setDisconnectConfirmOpen(false);
      setDrawer(null);
      resetQueryCacheAfterSignOut(queryClient);
    }
  });

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      connectionAttemptRef.current += 1;
      authApi.discardPendingConnection();
    };
  }, [authApi]);

  useEffect(() => {
    const destination = focusDestination ?? pendingSettingsFocus;
    const signedIn = role.data?.signedIn === true;
    if (
      role.isLoading ||
      deviceConnectionPending ||
      (destination === "connected" && !signedIn) ||
      (destination === "disconnected" && signedIn) ||
      destination === null
    ) {
      return;
    }

    const handle = window.setTimeout(() => {
      const target = destination === "connected" ? connectedHeadingRef.current : connectButtonRef.current;
      if (target === null || (target instanceof HTMLButtonElement && target.disabled)) {
        return;
      }
      target.focus({ preventScroll: true });
      pendingSettingsFocus = null;
      setFocusDestination(null);
    }, 0);
    return () => window.clearTimeout(handle);
  }, [deviceConnectionPending, focusDestination, role.data?.signedIn, role.isLoading]);

  useEffect(() => {
    if (snapshot?.access.session_state === "expired" || role.data?.signedIn === false) {
      connectionAttemptRef.current += 1;
      authApi.discardPendingConnection();
      setEmail("");
      setPassword("");
      setDeviceConnectionOpen(false);
      setDeviceConnectionError(null);
      setDisconnectConfirmOpen(false);
      setDrawer(null);
    }
  }, [authApi, role.data?.signedIn, snapshot?.access.session_state]);

  const submitDeviceConnection = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (connectionSubmittingRef.current || !email || !password) {
      return;
    }

    connectionSubmittingRef.current = true;
    const attempt = ++connectionAttemptRef.current;
    const credentials = { email, password };
    setPassword("");
    setDeviceConnectionPending(true);
    setDeviceConnectionError(null);
    try {
      const result = await authApi.connectDevice(credentials);
      if (!mountedRef.current || attempt !== connectionAttemptRef.current) {
        if (result === "connected") {
          try {
            await authApi.disconnectDevice();
          } finally {
            resetQueryCacheAfterSignOut(queryClient);
          }
        }
        return;
      }
      if (result === "discarded") {
        return;
      }

      requestSettingsFocus("connected");
      setEmail("");
      setDeviceConnectionOpen(false);
      await role.refetch();
    } catch (error) {
      const cleanupError = authApi.getCleanupError();
      if (mountedRef.current && (attempt === connectionAttemptRef.current || cleanupError !== null)) {
        setDeviceConnectionError(
          cleanupError ?? normalizeError(error, "이 기기를 운영 계정에 연결하지 못했습니다.")
        );
      }
    } finally {
      connectionSubmittingRef.current = false;
      if (mountedRef.current) {
        setDeviceConnectionPending(false);
      }
    }
  };

  const closeDeviceConnection = () => {
    connectionAttemptRef.current += 1;
    authApi.discardPendingConnection();
    setEmail("");
    setPassword("");
    setDeviceConnectionOpen(false);
    setDeviceConnectionError(null);
  };

  const retryConnectionCleanup = async () => {
    if (connectionSubmittingRef.current) {
      return;
    }
    connectionSubmittingRef.current = true;
    setDeviceConnectionPending(true);
    try {
      await authApi.retryCleanup();
      requestSettingsFocus("disconnected");
      resetQueryCacheAfterSignOut(queryClient);
      setDeviceConnectionError(null);
    } catch (error) {
      setDeviceConnectionError(
        authApi.getCleanupError() ?? normalizeError(error, "취소한 기기 연결을 정리하지 못했습니다.")
      );
    } finally {
      connectionSubmittingRef.current = false;
      if (mountedRef.current) {
        setDeviceConnectionPending(false);
      }
    }
  };

  if (!supabaseReady) {
    return <ConnectionSetupRequired />;
  }

  const cleanupRequired = deviceConnectionGuard.cleanupError !== null;

  if (role.isLoading && !cleanupRequired) {
    return <LoadingState label="저장된 운영 세션을 확인하는 중" />;
  }
  if (role.error && !cleanupRequired) {
    return (
      <div className="mx-auto max-w-3xl space-y-4">
        <ErrorState message="저장된 운영 세션을 확인하지 못했습니다." />
        <button
          className={pageButtonClass("neutral")}
          type="button"
          onClick={() => void role.refetch()}
          disabled={role.isFetching}
        >
          <RotateCcw size={17} aria-hidden="true" />
          {role.isFetching ? "상태 확인 중" : "상태 다시 확인"}
        </button>
        <section className="rounded-lg border border-line bg-surface p-4">
          <ConnectionDetails configured={supabaseReady} />
        </section>
      </div>
    );
  }

  const signedIn = role.data?.signedIn === true && !deviceConnectionGuard.shouldDiscardAuthenticatedSession;
  const actor = signedIn ? snapshot?.access.actor ?? null : null;
  const roles = actor?.roles ?? [];
  const platformAdmin = roles.includes("platform_admin");

  if (!signedIn) {
    return (
      <div className="mx-auto max-w-3xl space-y-4">
        {cleanupRequired ? (
          <section className="rounded-xl border border-danger/30 bg-dangerSoft p-5 text-danger" role="alert">
            <h2 className="font-bold">취소한 기기 연결 정리가 필요합니다</h2>
            <p className="mt-2 text-sm">
              이전 연결의 로컬 세션 제거를 완료하지 못해 모든 인증 상태를 차단했습니다. 네트워크와 앱 저장소를 확인한 뒤 다시 정리하세요.
            </p>
            {deviceConnectionGuard.cleanupError || deviceConnectionError ? (
              <p className="mt-2 text-sm">
                {errorMessage(deviceConnectionGuard.cleanupError ?? deviceConnectionError)}
              </p>
            ) : null}
            <button
              className={`${pageButtonClass("danger")} mt-4`}
              type="button"
              disabled={deviceConnectionPending}
              onClick={() => void retryConnectionCleanup()}
            >
              <RotateCcw size={17} aria-hidden="true" />
              {deviceConnectionPending ? "연결 정리 중" : "연결 정리 다시 시도"}
            </button>
          </section>
        ) : null}
        <section className="matte-surface rounded-xl p-6">
          <div>
            <p className="text-xs font-semibold text-primary">단일 사용자 기기</p>
            <h2 className="mt-1 text-xl font-bold">이 기기는 아직 연결되지 않았습니다</h2>
            <p className="mt-2 text-sm text-mutedStrong">
              한 번 연결하면 저장된 운영 세션을 자동으로 복구합니다. 비밀번호는 설정 파일이나 별도 앱 저장소에 보관하지 않습니다.
            </p>
          </div>
          <button
            ref={connectButtonRef}
            className={`${pageButtonClass("primary")} mt-5`}
            type="button"
            onClick={() => {
              setDeviceConnectionError(null);
              setDeviceConnectionOpen(true);
            }}
            disabled={deviceConnectionPending || deviceConnectionGuard.shouldDiscardAuthenticatedSession}
          >
            <Link2 size={17} aria-hidden="true" />
            {cleanupRequired
              ? "연결 정리 필요"
              : deviceConnectionPending || deviceConnectionGuard.shouldDiscardAuthenticatedSession
                ? "이전 연결 요청 정리 중"
                : "이 기기 연결"}
          </button>
        </section>

        <details className="rounded-lg border border-lineSubtle bg-surface px-4 py-2">
          <summary className="flex min-h-control cursor-pointer items-center font-semibold">연결 정보 보기</summary>
          <div className="border-t border-lineSubtle py-3">
            <ConnectionDetails configured={supabaseReady} />
          </div>
        </details>

        {deviceConnectionOpen ? (
          <DrawerSurface
            open
            readOnly={false}
            dirty={email.length > 0 || password.length > 0}
            title="이 기기 연결"
            description="최초 연결과 세션 만료 시에만 운영 계정 확인이 필요합니다. 연결 후에는 저장된 세션을 자동 복구합니다."
            closeLabel="기기 연결 닫기"
            onRequestClose={closeDeviceConnection}
          >
            <form className="grid gap-4" onSubmit={submitDeviceConnection}>
              <label className="grid gap-1.5 text-sm font-medium">
                운영 계정 이메일
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
              <p className="text-sm text-mutedStrong">
                연결만으로 운영 명령이 허용되지는 않습니다. 위험 작업에는 2단계 인증과 독립 검토가 계속 필요합니다.
              </p>
              <button
                className={pageButtonClass("primary")}
                type="submit"
                disabled={!email || !password || deviceConnectionPending}
                aria-busy={deviceConnectionPending}
              >
                <Link2 size={17} aria-hidden="true" />
                {deviceConnectionPending ? "기기 연결 확인 중" : "기기 연결"}
              </button>
            </form>
            {deviceConnectionError ? (
              <p className="mt-3 text-sm text-danger" role="alert">{errorMessage(deviceConnectionError)}</p>
            ) : null}
          </DrawerSurface>
        ) : null}
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl space-y-5">
      <section className="matte-surface rounded-xl p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <p className="text-xs font-semibold text-primary">이 기기에 연결된 운영 계정</p>
            <h2 ref={connectedHeadingRef} tabIndex={-1} className="mt-1 w-fit rounded-sm text-xl font-bold">
              {role.data?.email ?? "계정 확인 불가"}
            </h2>
            <p className="mt-1 text-sm text-mutedStrong">저장된 세션을 자동으로 복구하며, 위험 작업의 2단계 인증은 별도로 유지됩니다.</p>
          </div>
        </div>
        <div className="mt-5 grid gap-3 md:grid-cols-3">
          <SummaryItem label="역할" value={roles.length > 0 ? formatOperationsRoles(roles) : "확인 불가"} tone={roles.length > 0 ? "safe" : "warning"} />
          <SummaryItem label="2단계 인증" value={snapshot?.access.assurance_level === "aal2" ? "확인됨" : "추가 인증 필요"} tone={snapshot?.access.assurance_level === "aal2" ? "safe" : "warning"} />
          <SummaryItem label="세션" value={snapshot?.access.session_state === "active" ? "활성" : "만료 또는 확인 불가"} tone={snapshot?.access.session_state === "active" ? "safe" : "danger"} />
        </div>
      </section>

      <div className="grid gap-4 md:grid-cols-2">
        <button type="button" className="matte-surface min-h-32 rounded-xl p-5 text-left" onClick={() => setDrawer({ kind: "mfa" })}>
          <KeyRound className="text-primary" size={22} aria-hidden="true" />
          <span className="mt-3 block text-[17px] font-bold">2단계 인증 관리</span>
          <span className="mt-1 block text-sm text-mutedStrong">인증 수단 등록·선택과 2단계 인증 재확인</span>
        </button>
        {platformAdmin ? (
          <button type="button" className="matte-surface min-h-32 rounded-xl p-5 text-left" onClick={() => setDrawer({ kind: "access" })}>
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

      <details className="rounded-lg border border-lineSubtle bg-surface px-4 py-2">
        <summary className="flex min-h-control cursor-pointer items-center font-semibold">연결 정보 보기</summary>
        <div className="border-t border-lineSubtle py-3">
          <ConnectionDetails
            configured={supabaseReady}
            disconnecting={disconnectDevice.isPending}
            disconnectError={disconnectDevice.error}
            onDisconnect={() => {
              disconnectDevice.reset();
              setDisconnectConfirmOpen(true);
            }}
          />
        </div>
      </details>

      <ConfirmDialog
        open={disconnectConfirmOpen}
        title="이 기기 연결을 해제할까요?"
        description="현재 기기에 저장된 운영 세션과 휘발성 작업 입력이 제거됩니다. 다시 사용하려면 이 기기를 다시 연결해야 합니다."
        confirmLabel="연결 해제"
        cancelLabel="취소"
        pendingLabel="연결 해제 중"
        tone="danger"
        pending={disconnectDevice.isPending}
        error={disconnectDevice.error ? errorMessage(disconnectDevice.error) : undefined}
        onConfirm={async () => {
          try {
            await disconnectDevice.mutateAsync();
          } catch {
            // The mutation error remains visible in this fail-closed dialog.
          }
        }}
        onCancel={() => {
          if (!disconnectDevice.isPending) {
            setDisconnectConfirmOpen(false);
            disconnectDevice.reset();
          }
        }}
      />

      {drawer?.kind === "mfa" ? (
        <DrawerSurface open readOnly={false} title="2단계 인증 관리" description="비밀값과 인증 코드는 이 상세 화면을 닫으면 메모리에서 제거됩니다." onRequestClose={() => setDrawer(null)}>
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

function ConnectionSetupRequired() {
  const developmentBuild = import.meta.env?.DEV === true;
  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <section className="rounded-xl border border-warning/30 bg-warningSoft p-5 text-warning" role="alert">
        <div className="flex items-start gap-3">
          <AlertTriangle className="mt-0.5 shrink-0" size={19} aria-hidden="true" />
          <div>
            <h2 className="font-bold">운영 연결이 설정되지 않았습니다</h2>
            <p className="mt-2 text-sm">
              운영 연결 설정이 없어 모든 운영 변경을 안전하게 차단했습니다.
            </p>
            {developmentBuild ? (
              <p className="mt-2 text-sm">
                개발 환경에서는
                <code className="mx-1 rounded bg-surface px-1.5 py-0.5 text-ink">apps/desktop/.env.local</code>
                에 아래 두 값을 설정한 뒤 Tauri와 Vite를 완전히 종료하고 다시 실행하세요.
              </p>
            ) : (
              <p className="mt-2 text-sm">
                이 설치본에는 운영 연결 설정이 포함되지 않았습니다. 아래 두 값을 빌드 전에 설정하고 앱을 다시 빌드·설치하세요.
                설치 후 <code className="rounded bg-surface px-1.5 py-0.5 text-ink">.env.local</code>을 추가해도 반영되지 않습니다.
              </p>
            )}
            <ul className="mt-3 list-disc space-y-1 pl-5 text-sm">
              <li><code>VITE_SUPABASE_URL</code></li>
              <li><code>VITE_SUPABASE_PUBLISHABLE_KEY</code></li>
            </ul>
            <p className="mt-3 text-sm font-semibold">service role이나 서버 비밀키는 Desktop에 넣지 마세요.</p>
          </div>
        </div>
      </section>
      <section className="rounded-lg border border-line bg-surface p-4">
        <ConnectionDetails configured={false} />
      </section>
    </div>
  );
}

function ConnectionDetails({
  configured,
  disconnecting = false,
  disconnectError = null,
  onDisconnect
}: {
  readonly configured: boolean;
  readonly disconnecting?: boolean;
  readonly disconnectError?: Error | null;
  readonly onDisconnect?: () => void;
}) {
  return (
    <div>
      <KeyValue label="연결 설정" value={<Pill tone={configured ? "safe" : "danger"}>{configured ? "설정됨" : "미설정"}</Pill>} />
      <KeyValue label="클라이언트 권한" value="publishable key" />
      <KeyValue label="연결 상세" value="운영 전용 연결 V1" />
      <KeyValue label="public 테이블 직접 접근" value={<Pill tone="safe">비활성화</Pill>} />
      <KeyValue label="앱 버전" value={appVersion} />
      {onDisconnect ? (
        <div className="mt-4 border-t border-lineSubtle pt-4">
          <p className="mb-3 text-sm text-mutedStrong">이 작업은 현재 기기에 저장된 운영 세션과 휘발성 작업 입력을 제거합니다.</p>
          <button className={pageButtonClass("warning")} type="button" onClick={onDisconnect} disabled={disconnecting}>
            <Unplug size={17} aria-hidden="true" />
            {disconnecting ? "연결 해제 중" : "이 기기 연결 해제"}
          </button>
          {disconnectError ? <p className="mt-3 text-sm text-danger" role="alert">{errorMessage(disconnectError)}</p> : null}
        </div>
      ) : null}
    </div>
  );
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "인증 요청이 실패했습니다.";
}

function normalizeError(error: unknown, fallback: string): Error {
  return error instanceof Error ? error : new Error(fallback);
}
