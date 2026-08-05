import { resolveSupabaseClientConfig } from "./supabaseConfig";

export type DesktopReleaseConfigFailure =
  | "unexpected_public_input"
  | "invalid_hosted_config"
  | "loopback_endpoint";

export type DesktopReleaseConfigValidation =
  | { readonly ok: true }
  | { readonly ok: false; readonly reason: DesktopReleaseConfigFailure };

const ALLOWED_PUBLIC_KEYS = new Set([
  "VITE_SUPABASE_URL",
  "VITE_SUPABASE_PUBLISHABLE_KEY",
]);

const LOOPBACK_HOSTNAMES = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);

export function validateDesktopReleaseConfig(
  values: Readonly<Record<string, unknown>>,
): DesktopReleaseConfigValidation {
  const hasUnexpectedPublicInput = Object.keys(values).some(
    (key) => key.startsWith("VITE_") && !ALLOWED_PUBLIC_KEYS.has(key),
  );
  if (hasUnexpectedPublicInput) {
    return { ok: false, reason: "unexpected_public_input" };
  }

  const config = resolveSupabaseClientConfig(
    values.VITE_SUPABASE_URL,
    values.VITE_SUPABASE_PUBLISHABLE_KEY,
  );
  if (config === null) {
    return { ok: false, reason: "invalid_hosted_config" };
  }

  if (LOOPBACK_HOSTNAMES.has(new URL(config.url).hostname)) {
    return { ok: false, reason: "loopback_endpoint" };
  }

  return { ok: true };
}
