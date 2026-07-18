import assert from "node:assert/strict";

import { resolveSupabaseClientConfig } from "../src/lib/supabaseConfig";

const publishableKey = "sb_publishable_0123456789abcdefghijklmnop";

assert.deepEqual(
  resolveSupabaseClientConfig(
    "  https://project-ref.supabase.co  ",
    `  ${publishableKey}  `
  ),
  {
    url: "https://project-ref.supabase.co/",
    publishableKey,
    authStorageKey: "sb-project-ref-auth-token"
  }
);

assert.deepEqual(
  resolveSupabaseClientConfig("http://localhost:54321", publishableKey),
  {
    url: "http://localhost:54321/",
    publishableKey,
    authStorageKey: "sb-localhost-auth-token"
  }
);

assert.notEqual(
  resolveSupabaseClientConfig("http://127.0.0.1:54321", publishableKey),
  null
);
assert.notEqual(
  resolveSupabaseClientConfig("http://[::1]:54321", publishableKey),
  null
);

for (const unsafeUrl of [
  "http://project-ref.supabase.co",
  "https://example.com",
  "http://localhost.evil.test",
  "https://project-ref.supabase.co.evil.test",
  "https://project-ref.supabase.co:8443",
  "https://project-ref.supabase.co/rest/v1",
  "https://project-ref.supabase.co?redirect=https://example.com",
  "https://project-ref.supabase.co/#fragment",
  "https://user:password@project-ref.supabase.co"
]) {
  assert.equal(
    resolveSupabaseClientConfig(unsafeUrl, publishableKey),
    null,
    `unsafe desktop Supabase URL must be rejected: ${unsafeUrl}`
  );
}

for (const unsafeKey of [
  "arbitrary-client-key",
  "sb_publishable_",
  "sb_secret_0123456789abcdefghijklmnop",
  "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.signature"
]) {
  assert.equal(
    resolveSupabaseClientConfig("https://project-ref.supabase.co", unsafeKey),
    null,
    "secret-grade or malformed keys must not be accepted by the desktop"
  );
}

console.log("Supabase desktop configuration security boundary passed");
