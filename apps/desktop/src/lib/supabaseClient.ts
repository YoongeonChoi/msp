import { createClient } from "@supabase/supabase-js";
import { resolveSupabaseClientConfig } from "./supabaseConfig";

const supabaseUrl = import.meta.env?.VITE_SUPABASE_URL;
const publishableKey = import.meta.env?.VITE_SUPABASE_PUBLISHABLE_KEY;
const clientConfig = supabaseUrl && publishableKey
  ? resolveSupabaseClientConfig(supabaseUrl, publishableKey)
  : null;

export const hasSupabaseConfig = clientConfig !== null;

export const supabase = clientConfig
  ? createClient(clientConfig.url, clientConfig.publishableKey, {
        auth: {
          autoRefreshToken: true,
          persistSession: true
        }
      })
    : null;
