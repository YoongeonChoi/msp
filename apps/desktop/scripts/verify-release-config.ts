import { fileURLToPath } from "node:url";
import { loadEnv } from "vite";

import { validateDesktopReleaseConfig } from "../src/lib/desktopReleaseConfig";

const appRoot = fileURLToPath(new URL("../", import.meta.url));
const releaseEnv = loadEnv("production", appRoot, "");
const validation = validateDesktopReleaseConfig(releaseEnv);

if (!validation.ok) {
  const messages = {
    unexpected_public_input:
      "only VITE_SUPABASE_URL and VITE_SUPABASE_PUBLISHABLE_KEY may be exposed " +
      "to the packaged UI.",
    invalid_hosted_config:
      "provide a valid hosted Supabase HTTPS URL and sb_publishable_ key. " +
      "Values were not printed.",
    loopback_endpoint:
      "a packaged installer may not target a loopback Supabase endpoint.",
  } as const;
  console.error(
    `Desktop release configuration rejected: ${messages[validation.reason]}`,
  );
  process.exit(1);
}

console.log(
  "Desktop release configuration is valid: hosted Supabase URL and publishable " +
    "key are set (values hidden).",
);
