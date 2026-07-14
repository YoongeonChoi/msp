import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  queryKeyPrefixesForRealtimeTable,
  realtimeTables
} from "./realtimeInvalidation";
import { supabase } from "./supabaseClient";

type ViteRealtimeEnv = {
  readonly VITE_SUPABASE_REALTIME_DISABLED?: string;
};

const env = (import.meta as ImportMeta & { readonly env?: ViteRealtimeEnv }).env ?? {};
const realtimeDisabled = env.VITE_SUPABASE_REALTIME_DISABLED === "true";

export function useRealtimeInvalidation(): void {
  const queryClient = useQueryClient();

  useEffect(() => {
    const client = supabase;
    if (!client || realtimeDisabled) {
      return;
    }
    const channel = client.channel("desktop-paper-cockpit");
    for (const table of realtimeTables) {
      channel.on("postgres_changes", { event: "*", schema: "public", table }, () => {
        for (const queryKey of queryKeyPrefixesForRealtimeTable(table)) {
          void queryClient.invalidateQueries({ queryKey });
        }
      });
    }
    void channel.subscribe();
    return () => {
      void client.removeChannel(channel);
    };
  }, [queryClient]);
}
