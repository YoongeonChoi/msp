import assert from "node:assert/strict";

import {
  clearSupabaseAuthStorage,
  createTimeoutFetch
} from "../src/lib/supabaseClient";
import { resolveSupabaseClientConfig } from "../src/lib/supabaseConfig";

const config = resolveSupabaseClientConfig(
  "  https://project-ref.supabase.co  ",
  "  sb_publishable_test-value  "
);
assert.deepEqual(config, {
  url: "https://project-ref.supabase.co/",
  publishableKey: "sb_publishable_test-value",
  authStorageKey: "sb-project-ref-auth-token"
});

assert.equal(resolveSupabaseClientConfig(undefined, "key"), null);
assert.equal(resolveSupabaseClientConfig("https://project-ref.supabase.co", undefined), null);
assert.equal(resolveSupabaseClientConfig("", "key"), null);
assert.equal(resolveSupabaseClientConfig("not a url", "key"), null);
assert.equal(resolveSupabaseClientConfig("javascript:alert(1)", "key"), null);
assert.equal(resolveSupabaseClientConfig("https://user:password@example.com", "key"), null);

assert.deepEqual(
  resolveSupabaseClientConfig("http://localhost:54321", "sb_publishable_local-test-key"),
  {
    url: "http://localhost:54321/",
    publishableKey: "sb_publishable_local-test-key",
    authStorageKey: "sb-localhost-auth-token"
  }
);

const removedKeys: string[] = [];
clearSupabaseAuthStorage(
  { removeItem: (key) => removedKeys.push(key) },
  "sb-project-ref-auth-token"
);
assert.deepEqual(removedKeys, [
  "sb-project-ref-auth-token",
  "sb-project-ref-auth-token-code-verifier",
  "sb-project-ref-auth-token-user"
]);

let timeoutAbortObserved = false;
const hangingFetch = (async (
  _input: RequestInfo | URL,
  init?: RequestInit
): Promise<Response> => new Promise((_resolve, reject) => {
  assert.ok(init?.signal);
  init.signal.addEventListener("abort", () => {
    timeoutAbortObserved = true;
    reject(new Error("synthetic timeout abort"));
  }, { once: true });
})) as typeof fetch;
const timeoutFetch = createTimeoutFetch(hangingFetch, 5);
await assert.rejects(
  timeoutFetch("https://project-ref.supabase.co/auth/v1/token"),
  (error: unknown) => error instanceof DOMException && error.name === "TimeoutError"
);
assert.equal(timeoutAbortObserved, true, "a stuck auth request must be aborted at its deadline");

let bodyTimeoutAbortObserved = false;
const hangingBodyFetch = (async (
  _input: RequestInfo | URL,
  init?: RequestInit
): Promise<Response> => {
  assert.ok(init?.signal);
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode("{"));
      init.signal?.addEventListener("abort", () => {
        bodyTimeoutAbortObserved = true;
        controller.error(new Error("synthetic response body abort"));
      }, { once: true });
    }
  });
  return new Response(body, {
    headers: { "content-type": "application/json" },
    status: 200
  });
}) as typeof fetch;
await assert.rejects(
  createTimeoutFetch(hangingBodyFetch, 5)("https://project-ref.supabase.co/auth/v1/token"),
  (error: unknown) => error instanceof DOMException && error.name === "TimeoutError"
);
assert.equal(
  bodyTimeoutAbortObserved,
  true,
  "the request deadline must remain active until the response body is complete"
);

const bufferedResponse = await createTimeoutFetch((async (): Promise<Response> => (
  new Response(JSON.stringify({ connected: true }), {
    headers: { "content-type": "application/json", "x-request-id": "test-request" },
    status: 201,
    statusText: "Created"
  })
)) as typeof fetch, 1_000)("https://project-ref.supabase.co/rest/v1/rpc");
assert.equal(bufferedResponse.status, 201);
assert.equal(bufferedResponse.statusText, "Created");
assert.equal(bufferedResponse.headers.get("x-request-id"), "test-request");
assert.deepEqual(await bufferedResponse.json(), { connected: true });

const upstreamAbortController = new AbortController();
let upstreamAbortObserved = false;
const upstreamFetch = createTimeoutFetch((async (
  _input: RequestInfo | URL,
  init?: RequestInit
): Promise<Response> => new Promise((_resolve, reject) => {
  assert.ok(init?.signal);
  init.signal.addEventListener("abort", () => {
    upstreamAbortObserved = true;
    reject(new Error("synthetic upstream abort"));
  }, { once: true });
})) as typeof fetch, 1_000);
const upstreamRequest = upstreamFetch("https://project-ref.supabase.co/rest/v1/rpc", {
  signal: upstreamAbortController.signal
});
upstreamAbortController.abort();
await assert.rejects(
  upstreamRequest,
  (error: unknown) => error instanceof DOMException && error.name === "AbortError"
);
assert.equal(upstreamAbortObserved, true, "an existing caller abort signal must remain effective");

console.log("Supabase client configuration guard passed");
