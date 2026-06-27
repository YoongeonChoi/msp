import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { supabase } from "./supabaseClient";

const realtimeTables = [
  "bot_settings",
  "worker_heartbeats",
  "api_health",
  "orders",
  "decision_snapshots",
  "engine_events"
];

export function useRealtimeInvalidation(): void {
  const queryClient = useQueryClient();

  useEffect(() => {
    const client = supabase;
    if (!client) {
      return;
    }
    const channel = client.channel("desktop-paper-cockpit");
    for (const table of realtimeTables) {
      channel.on("postgres_changes", { event: "*", schema: "public", table }, () => {
        void queryClient.invalidateQueries();
      });
    }
    void channel.subscribe();
    return () => {
      void client.removeChannel(channel);
    };
  }, [queryClient]);
}
