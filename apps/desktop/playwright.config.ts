import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:1431",
    trace: "retain-on-failure"
  },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 1431",
    env: {
      VITE_SUPABASE_URL: "https://e2e.supabase.test",
      VITE_SUPABASE_PUBLISHABLE_KEY: "e2e-publishable-key",
      VITE_SUPABASE_REALTIME_DISABLED: "true"
    },
    reuseExistingServer: false,
    timeout: 120_000,
    url: "http://127.0.0.1:1431"
  }
});
