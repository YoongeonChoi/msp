import { useState } from "react";
import { AppLayout } from "./components/Layout";
import { useRealtimeInvalidation } from "./lib/useRealtimeInvalidation";
import { parsePageKey } from "./lib/navigation";
import type { PageKey } from "./lib/navigation";
import { ControlPage } from "./pages/ControlPage";
import { DashboardPage } from "./pages/DashboardPage";
import { FundamentalsPage } from "./pages/FundamentalsPage";
import { LogsPage } from "./pages/LogsPage";
import { NewsPage } from "./pages/NewsPage";
import { OrdersPage } from "./pages/OrdersPage";
import { PortfolioPage } from "./pages/PortfolioPage";
import { SettingsPage } from "./pages/SettingsPage";
import { SignalsPage } from "./pages/SignalsPage";
import { StrategyLabPage } from "./pages/StrategyLabPage";
import { WatchlistPage } from "./pages/WatchlistPage";

function App() {
  const [page, setPageState] = useState<PageKey>(() => initialPage());
  useRealtimeInvalidation();

  const setPage = (nextPage: PageKey) => {
    setPageState(nextPage);
    const url = new URL(window.location.href);
    url.searchParams.set("page", nextPage);
    window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
  };

  return (
    <AppLayout page={page} setPage={setPage}>
      {page === "dashboard" ? <DashboardPage /> : null}
      {page === "control" ? <ControlPage /> : null}
      {page === "watchlist" ? <WatchlistPage /> : null}
      {page === "portfolio" ? <PortfolioPage /> : null}
      {page === "orders" ? <OrdersPage /> : null}
      {page === "signals" ? <SignalsPage /> : null}
      {page === "fundamentals" ? <FundamentalsPage /> : null}
      {page === "news" ? <NewsPage /> : null}
      {page === "strategy" ? <StrategyLabPage /> : null}
      {page === "settings" ? <SettingsPage /> : null}
      {page === "logs" ? <LogsPage /> : null}
    </AppLayout>
  );
}

function initialPage(): PageKey {
  const url = new URL(window.location.href);
  const searchPage = parsePageKey(url.searchParams.get("page") ?? "");
  const hashPage = parsePageKey(url.hash.replace(/^#/, ""));
  return searchPage ?? hashPage ?? "dashboard";
}

export default App;
