import assert from "node:assert/strict";

import { validateDesktopReleaseConfig } from "../src/lib/desktopReleaseConfig";

const hostedReleaseConfig = {
  VITE_SUPABASE_URL: "https://project-ref.supabase.co",
  VITE_SUPABASE_PUBLISHABLE_KEY: "sb_publishable_release-test",
};

assert.deepEqual(validateDesktopReleaseConfig(hostedReleaseConfig), {
  ok: true,
});

assert.deepEqual(
  validateDesktopReleaseConfig({
    ...hostedReleaseConfig,
    VITE_SUPABASE_SERVICE_ROLE_KEY: "must-never-be-exposed",
  }),
  { ok: false, reason: "unexpected_public_input" },
);

assert.deepEqual(
  validateDesktopReleaseConfig({
    ...hostedReleaseConfig,
    VITE_SUPABASE_URL: "http://localhost:54321",
  }),
  { ok: false, reason: "loopback_endpoint" },
);

assert.deepEqual(
  validateDesktopReleaseConfig({
    ...hostedReleaseConfig,
    VITE_SUPABASE_PUBLISHABLE_KEY: "sb_secret_not-a-desktop-key",
  }),
  { ok: false, reason: "invalid_hosted_config" },
);

console.log("Desktop release configuration policy passed");
