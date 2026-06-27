export type ProviderHealth = "healthy" | "degraded" | "down";
export type PageKey =
  | "Dashboard"
  | "Control"
  | "Watchlist"
  | "Portfolio"
  | "Orders"
  | "Signals"
  | "Fundamentals"
  | "News"
  | "Strategy Lab"
  | "Settings"
  | "Logs";

export const navItems: { key: PageKey; label: string }[] = [
  { key: "Dashboard", label: "Dashboard" },
  { key: "Control", label: "Control" },
  { key: "Watchlist", label: "Watchlist" },
  { key: "Portfolio", label: "Portfolio" },
  { key: "Orders", label: "Orders" },
  { key: "Signals", label: "Signals" },
  { key: "Fundamentals", label: "Fundamentals" },
  { key: "News", label: "News" },
  { key: "Strategy Lab", label: "Strategy Lab" },
  { key: "Settings", label: "Settings" },
  { key: "Logs", label: "Logs" }
];

export const providerHealth = [
  { provider: "Toss", status: "healthy" as ProviderHealth, latencyMs: 120 },
  { provider: "Supabase", status: "healthy" as ProviderHealth, latencyMs: 80 },
  { provider: "KRX", status: "healthy" as ProviderHealth, latencyMs: 150 },
  { provider: "OpenDART", status: "degraded" as ProviderHealth, latencyMs: 420 },
  { provider: "Naver", status: "healthy" as ProviderHealth, latencyMs: 190 },
  { provider: "OpenAI", status: "healthy" as ProviderHealth, latencyMs: 260 }
];

export const decisions = [
  { symbol: "005930", action: "buy", score: 0.72, risk: "paper only", time: "09:31:20" },
  { symbol: "000660", action: "hold", score: 0.54, risk: "관망", time: "09:31:18" },
  { symbol: "035420", action: "blocked", score: 0.69, risk: "critical news", time: "09:30:52" }
];

export const orders = [
  { symbol: "005930", side: "buy", status: "paper", amount: "100,000원", reason: "paper mode" },
  {
    symbol: "035420",
    side: "buy",
    status: "blocked",
    amount: "100,000원",
    reason: "critical_negative_news_risk"
  }
];

export const positions = [
  { symbol: "005930", name: "삼성전자", sector: "반도체", pnl: 1.8, exposure: 8.5 },
  { symbol: "000660", name: "SK하이닉스", sector: "반도체", pnl: -0.7, exposure: 5.1 }
];

export const chartData = [
  { day: "월", pnl: 0.1 },
  { day: "화", pnl: 0.4 },
  { day: "수", pnl: -0.2 },
  { day: "목", pnl: 0.6 },
  { day: "금", pnl: 0.3 }
];

