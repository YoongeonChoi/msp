import { useState } from "react";
import { AppLayout } from "./components/Layout";
import { parsePageKey } from "./lib/navigation";
import type { PageKey } from "./lib/navigation";
import { ControlPage } from "./pages/ControlPage";
import { SettingsPage } from "./pages/SettingsPage";
import { ControlPlaneRealtimeProvider } from "./lib/controlPlaneRealtime";

function App() {
  const [page, setPageState] = useState<PageKey>(() => initialPage());

  const setPage = (nextPage: PageKey) => {
    setPageState(nextPage);
    const url = new URL(window.location.href);
    url.searchParams.set("page", nextPage);
    window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
  };

  return (
    <ControlPlaneRealtimeProvider>
      <AppLayout page={page} setPage={setPage}>
        {page === "control" ? <ControlPage /> : null}
        {page === "settings" ? <SettingsPage /> : null}
      </AppLayout>
    </ControlPlaneRealtimeProvider>
  );
}

function initialPage(): PageKey {
  const url = new URL(window.location.href);
  const searchPage = parsePageKey(url.searchParams.get("page") ?? "");
  const hashPage = parsePageKey(url.hash.replace(/^#/, ""));
  return searchPage ?? hashPage ?? "control";
}

export default App;
