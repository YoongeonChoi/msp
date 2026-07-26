import {
  Suspense,
  lazy,
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore
} from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AppLayout, type DeviceConnectionState } from "./components/Layout";
import { AuthRequiredState } from "./components/AuthRequiredState";
import { getPageLabel, pageRequiresConnection, parsePageKey } from "./lib/navigation";
import type { PageKey } from "./lib/navigation";
import { OperationsSnapshotProvider, useOperationsSnapshot } from "./lib/operationsSnapshotContext";
import {
  evaluateAuthSessionBoundary,
  resetQueryCacheAfterSignOut,
  resetQueryCacheAfterPrincipalChange
} from "./lib/authSessionCache";
import { clearPersistedSupabaseSession, supabase } from "./lib/supabaseClient";
import {
  blockDeviceConnectionCleanup,
  fetchAuthRole,
  getDeviceConnectionGuardSnapshot,
  isSupabaseReady,
  subscribeDeviceConnectionGuard,
  shouldDiscardPendingDeviceConnection
} from "./lib/authData";
import { authRoleQueryKey } from "./lib/authQueryKey";
import { ControlPage } from "./pages/ControlPage";
import { LoadingState } from "./components/ui";
import { LazySurfaceBoundary } from "./components/LazySurfaceBoundary";

const importSettingsPage = () => import("./pages/SettingsPage");
const SettingsPage = lazy(async () => ({ default: (await importSettingsPage()).SettingsPage }));

function App() {
  const queryClient = useQueryClient();
  const [page, setPageState] = useState<PageKey>(() => initialPage());
  const [sessionBoundaryKey, setSessionBoundaryKey] = useState(0);
  const [sessionGuarded, setSessionGuarded] = useState(false);
  const [authPrincipalId, setAuthPrincipalId] = useState<string | null>(null);
  const authPrincipalIdRef = useRef<string | null>(null);
  const sessionLostRef = useRef(false);
  const supabaseReady = isSupabaseReady();
  const deviceConnectionGuard = useSyncExternalStore(
    subscribeDeviceConnectionGuard,
    getDeviceConnectionGuardSnapshot,
    getDeviceConnectionGuardSnapshot
  );
  const authRole = useQuery({
    queryKey: authRoleQueryKey,
    queryFn: fetchAuthRole,
    enabled: supabaseReady,
    retry: false
  });
  const connectionState: DeviceConnectionState = !supabaseReady
    ? "setup-required"
    : deviceConnectionGuard.shouldDiscardAuthenticatedSession
      ? deviceConnectionGuard.cleanupError === null
        ? "checking"
        : "error"
      : authRole.isLoading
        ? "checking"
        : authRole.error
          ? "error"
          : authRole.data?.signedIn === true && sessionGuarded
            ? "error"
            : authRole.data?.signedIn === true
              ? "connected"
              : "disconnected";
  const operationsEnabled = connectionState === "connected";

  const purgeSession = useCallback(() => {
    setSessionGuarded(true);
    setAuthPrincipalId(null);
    authPrincipalIdRef.current = null;
    if (sessionLostRef.current) {
      return;
    }
    sessionLostRef.current = true;
    try {
      supabase?.auth.stopAutoRefresh();
    } catch {
      // Query and mutation state still fail closed below.
    }
    try {
      clearPersistedSupabaseSession();
    } catch (error) {
      blockDeviceConnectionCleanup(error);
    }
    resetQueryCacheAfterSignOut(queryClient);
    setSessionBoundaryKey((current) => current + 1);
  }, [queryClient]);

  const markSessionActive = useCallback(() => {
    sessionLostRef.current = false;
    setSessionGuarded(false);
  }, []);

  useEffect(() => {
    if (supabase === null) {
      return;
    }
    const { data: { subscription } } = supabase.auth.onAuthStateChange((event, session) => {
      const boundary = evaluateAuthSessionBoundary(authPrincipalIdRef.current, event, session);
      authPrincipalIdRef.current = boundary.nextPrincipalId;
      setAuthPrincipalId(boundary.nextPrincipalId);
      if (session !== null && shouldDiscardPendingDeviceConnection()) {
        purgeSession();
        return;
      }
      if (boundary.shouldPurgeSession) {
        purgeSession();
        return;
      }
      if (boundary.principalChanged) {
        resetQueryCacheAfterPrincipalChange(queryClient);
        setSessionBoundaryKey((current) => current + 1);
      }
      markSessionActive();
      if (boundary.shouldRefreshQueries) {
        void queryClient.invalidateQueries({
          queryKey: authRoleQueryKey,
          exact: true
        });
      }
    });
    return () => subscription.unsubscribe();
  }, [markSessionActive, purgeSession, queryClient]);

  useEffect(() => {
    const onPopState = () => setPageState(initialPage());
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  const setPage = useCallback((nextPage: PageKey) => {
    setPageState((current) => {
      if (current === nextPage) {
        return current;
      }
      const url = new URL(window.location.href);
      url.searchParams.set("page", nextPage);
      window.history.pushState({ page: nextPage }, "", `${url.pathname}${url.search}${url.hash}`);
      return nextPage;
    });
  }, []);

  useEffect(() => {
    if (!pageRequiresConnection(page) || connectionState === "checking" || connectionState === "connected") {
      return;
    }

    const url = new URL(window.location.href);
    url.searchParams.set("page", "settings");
    window.history.replaceState(
      { page: "settings" },
      "",
      `${url.pathname}${url.search}${url.hash}`
    );
    setPageState("settings");
  }, [connectionState, page]);

  const preloadPage = useCallback((nextPage: PageKey) => {
    if (nextPage === "settings") {
      void importSettingsPage();
    }
  }, []);

  return (
    <OperationsSnapshotProvider
      key={sessionBoundaryKey}
      enabled={operationsEnabled}
      principalId={authPrincipalId}
    >
      <SessionExpiryGuard onSessionActive={markSessionActive} onSessionLost={purgeSession} />
      <AppLayout
        page={page}
        setPage={setPage}
        preloadPage={preloadPage}
        connectionState={connectionState}
      >
        {pageRequiresConnection(page) && connectionState === "connected" ? (
          <ControlPage surface={page} />
        ) : null}
        {pageRequiresConnection(page) && connectionState === "checking" ? (
          <LoadingState label="저장된 기기 연결을 확인하는 중" />
        ) : null}
        {pageRequiresConnection(page) && connectionState !== "connected" && connectionState !== "checking" ? (
          <AuthRequiredState surface={getPageLabel(page)} />
        ) : null}
        {page === "settings" ? (
          <LazySurfaceBoundary
            key="settings"
            title="계정·보안 화면을 안전하게 열지 못했습니다"
            detail="화면을 다시 불러오기 전까지 인증·권한 변경 기능은 차단됩니다."
            logCode="settings_chunk_load_failed"
          >
            <Suspense fallback={<LoadingState label="계정·보안 화면을 불러오는 중" />}>
              <SettingsPage />
            </Suspense>
          </LazySurfaceBoundary>
        ) : null}
      </AppLayout>
    </OperationsSnapshotProvider>
  );
}

function SessionExpiryGuard({
  onSessionActive,
  onSessionLost
}: {
  readonly onSessionActive: () => void;
  readonly onSessionLost: () => void;
}) {
  const { snapshot } = useOperationsSnapshot();

  useEffect(() => {
    if (snapshot?.access.session_state === "active") {
      onSessionActive();
      return;
    }
    if (snapshot?.access.session_state === "expired") {
      onSessionLost();
    }
  }, [onSessionActive, onSessionLost, snapshot?.access.session_state]);

  return null;
}

function initialPage(): PageKey {
  const url = new URL(window.location.href);
  const searchPage = parsePageKey(url.searchParams.get("page") ?? "");
  const hashPage = parsePageKey(url.hash.replace(/^#/, ""));
  return searchPage ?? hashPage ?? "control";
}

export default App;
