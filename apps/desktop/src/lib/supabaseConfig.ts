export interface SupabaseClientConfig {
  readonly url: string;
  readonly publishableKey: string;
  readonly authStorageKey: string;
}

const PUBLISHABLE_KEY_PATTERN = /^sb_publishable_[A-Za-z0-9_-]+$/;
const HOSTED_SUPABASE_PATTERN =
  /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.supabase\.co$/;

function isLoopbackHostname(hostname: string): boolean {
  return (
    hostname === "localhost" ||
    hostname === "127.0.0.1" ||
    hostname === "[::1]" ||
    hostname === "::1"
  );
}

/**
 * Treats partial, malformed, or unsafe desktop configuration as unavailable.
 * The desktop may use only a publishable key with a hosted Supabase endpoint
 * or an explicit loopback endpoint for local development.
 */
export function resolveSupabaseClientConfig(
  urlValue: unknown,
  publishableKeyValue: unknown
): SupabaseClientConfig | null {
  if (typeof urlValue !== "string" || typeof publishableKeyValue !== "string") {
    return null;
  }

  const url = urlValue.trim();
  const publishableKey = publishableKeyValue.trim();
  if (url.length === 0 || !PUBLISHABLE_KEY_PATTERN.test(publishableKey)) {
    return null;
  }

  try {
    const parsedUrl = new URL(url);
    const isLoopback = isLoopbackHostname(parsedUrl.hostname);
    const isHostedSupabase = HOSTED_SUPABASE_PATTERN.test(parsedUrl.hostname);
    const hasUnsafeLocation =
      parsedUrl.pathname !== "/" ||
      parsedUrl.search.length > 0 ||
      parsedUrl.hash.length > 0;

    if (
      (!isLoopback && (!isHostedSupabase || parsedUrl.protocol !== "https:")) ||
      (isHostedSupabase && parsedUrl.port.length > 0) ||
      (isLoopback && parsedUrl.protocol !== "https:" && parsedUrl.protocol !== "http:") ||
      hasUnsafeLocation ||
      parsedUrl.username.length > 0 ||
      parsedUrl.password.length > 0
    ) {
      return null;
    }

    const projectRef = parsedUrl.hostname.split(".")[0];
    if (!projectRef) {
      return null;
    }

    return {
      url: parsedUrl.href,
      publishableKey,
      authStorageKey: `sb-${projectRef}-auth-token`
    };
  } catch {
    return null;
  }
}
