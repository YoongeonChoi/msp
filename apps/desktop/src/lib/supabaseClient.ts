import { createClient } from "@supabase/supabase-js";
import { resolveSupabaseClientConfig } from "./supabaseConfig";

const supabaseUrl = import.meta.env?.VITE_SUPABASE_URL;
const publishableKey = import.meta.env?.VITE_SUPABASE_PUBLISHABLE_KEY;
export const supabaseRequestTimeoutMs = 15_000;
const clientConfig = supabaseUrl && publishableKey
  ? resolveSupabaseClientConfig(supabaseUrl, publishableKey)
  : null;
const timeoutFetch = typeof globalThis.fetch === "function"
  ? createTimeoutFetch(globalThis.fetch.bind(globalThis), supabaseRequestTimeoutMs)
  : null;

export const supabaseAuthStorageKey = clientConfig?.authStorageKey ?? null;

export const hasSupabaseConfig = clientConfig !== null;

export const supabase = clientConfig
  ? createClient(clientConfig.url, clientConfig.publishableKey, {
        auth: {
          autoRefreshToken: true,
          persistSession: true
        },
        ...(timeoutFetch === null ? {} : { global: { fetch: timeoutFetch } })
      })
    : null;

export function createTimeoutFetch(
  fetchImplementation: typeof fetch,
  timeoutMs: number
): typeof fetch {
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
    throw new Error("timeoutMs must be a positive finite number");
  }

  return (async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const timeoutController = new AbortController();
    const requestSignal = init?.signal ?? requestSignalFromInput(input);
    let rejectAbort!: (reason: unknown) => void;
    const abortPromise = new Promise<never>((_resolve, reject) => {
      rejectAbort = reject;
    });
    const abortRequest = (reason: DOMException) => {
      timeoutController.abort();
      rejectAbort(reason);
    };
    const forwardAbort = () => abortRequest(new DOMException("Supabase request aborted", "AbortError"));
    if (requestSignal?.aborted) {
      forwardAbort();
    } else {
      requestSignal?.addEventListener("abort", forwardAbort, { once: true });
    }
    const timeoutHandle = globalThis.setTimeout(
      () => abortRequest(new DOMException("Supabase request timed out", "TimeoutError")),
      timeoutMs
    );

    try {
      const response = await Promise.race([
        fetchImplementation(input, {
          ...init,
          signal: timeoutController.signal
        }),
        abortPromise
      ]);
      if (response.body === null) {
        return response;
      }

      const body = await Promise.race([response.arrayBuffer(), abortPromise]);
      return recreateBufferedResponse(response, body);
    } finally {
      globalThis.clearTimeout(timeoutHandle);
      requestSignal?.removeEventListener("abort", forwardAbort);
    }
  }) as typeof fetch;
}

function requestSignalFromInput(input: RequestInfo | URL): AbortSignal | null {
  return typeof Request !== "undefined" && input instanceof Request ? input.signal : null;
}

function recreateBufferedResponse(response: Response, body: ArrayBuffer): Response {
  const bufferedResponse = new Response(body, {
    headers: response.headers,
    status: response.status,
    statusText: response.statusText
  });
  Object.defineProperties(bufferedResponse, {
    redirected: { configurable: true, value: response.redirected },
    type: { configurable: true, value: response.type },
    url: { configurable: true, value: response.url }
  });
  return bufferedResponse;
}

export interface RemovableStorage {
  readonly removeItem: (key: string) => void;
}

export function clearSupabaseAuthStorage(
  storage: RemovableStorage,
  storageKey: string
): void {
  storage.removeItem(storageKey);
  storage.removeItem(`${storageKey}-code-verifier`);
  storage.removeItem(`${storageKey}-user`);
}

export function clearPersistedSupabaseSession(): void {
  if (supabaseAuthStorageKey === null || typeof globalThis.localStorage === "undefined") {
    return;
  }
  clearSupabaseAuthStorage(globalThis.localStorage, supabaseAuthStorageKey);
}
