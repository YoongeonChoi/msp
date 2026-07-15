import { Suspense, lazy, useCallback, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { AppLayout } from "./components/Layout";
import { parsePageKey } from "./lib/navigation";
import type { PageKey } from "./lib/navigation";
import { OperationsSnapshotProvider, useOperationsSnapshot } from "./lib/operationsSnapshotContext";
import { resetQueryCacheAfterSignOut, shouldPurgeAuthSession } from "./lib/authSessionCache";
import { supabase } from "./lib/supabaseClient";
import { ControlPage } from "./pages/ControlPage";
import { LoadingState } from "./components/ui";
import { LazySurfaceBoundary } from "./components/LazySurfaceBoundary";

const importSettingsPage = () => import("./pages/SettingsPage");
const SettingsPage = lazy(async () => ({ default: (await importSettingsPage()).SettingsPage }));

function App() {
  const queryClient = useQueryClient();
  const [page, setPageState] = useState<PageKey>(() => initialPage());
  const [sessionBoundaryKey, setSessionBoundaryKey] = useState(0);
  const sessionLostRef = useRef(false);

  const purgeSession = useCallback(() => {
    if (sessionLostRef.current) {
      return;
    }
    sessionLostRef.current = true;
    resetQueryCacheAfterSignOut(queryClient);
    setSessionBoundaryKey((current) => current + 1);
  }, [queryClient]);

  const markSessionActive = useCallback(() => {
    sessionLostRef.current = false;
  }, []);

  useEffect(() => {
    if (supabase === null) {
      return;
    }
    const { data: { subscription } } = supabase.auth.onAuthStateChange((event, session) => {
      if (shouldPurgeAuthSession(event, session)) {
        purgeSession();
        return;
      }
      markSessionActive();
      if (event === "SIGNED_IN") {
        void queryClient.invalidateQueries();
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

  const preloadPage = useCallback((nextPage: PageKey) => {
    if (nextPage === "settings") {
      void importSettingsPage();
    }
  }, []);

  return (
    <OperationsSnapshotProvider key={sessionBoundaryKey}>
      <SessionExpiryGuard onSessionActive={markSessionActive} onSessionLost={purgeSession} />
      <AppLayout page={page} setPage={setPage} preloadPage={preloadPage}>
        {page === "control" ? <ControlPage /> : null}
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
