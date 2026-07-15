import { createContext, type ReactNode, useContext, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { z } from "zod";

import { operationsSnapshotQueryKey } from "./operationsData";
import { supabase } from "./supabaseClient";

export interface ClientRealtimeHealth {
  readonly connected: boolean;
  readonly connectedAt: string | null;
  readonly lastSignalAt: string | null;
}

const RealtimeHealthContext = createContext<ClientRealtimeHealth | null>(null);
const disconnected: ClientRealtimeHealth = {
  connected: false,
  connectedAt: null,
  lastSignalAt: null
};
const signalSchema = z
  .object({
    id: z.literal("singleton"),
    signal_version: z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER),
    signaled_at: z.string().datetime({ offset: true }),
    signal_kind: z.literal("snapshot_invalidated")
  })
  .strict();

export function ControlPlaneRealtimeProvider({ children }: { readonly children: ReactNode }) {
  const queryClient = useQueryClient();
  const [health, setHealth] = useState<ClientRealtimeHealth>(disconnected);
  const lastVersion = useRef<number | null>(null);

  useEffect(() => {
    const client = supabase;
    if (client === null || import.meta.env?.VITE_SUPABASE_REALTIME_DISABLED === "true") {
      setHealth(disconnected);
      return;
    }
    const channel = client
      .channel("desktop-control-plane-signal-v1")
      .on(
        "postgres_changes",
        { event: "UPDATE", schema: "api", table: "control_plane_signal" },
        (payload) => {
          const parsed = signalSchema.safeParse(payload.new);
          if (!parsed.success || (lastVersion.current !== null && parsed.data.signal_version <= lastVersion.current)) {
            setHealth(disconnected);
            return;
          }
          lastVersion.current = parsed.data.signal_version;
          setHealth((current) => ({
            connected: true,
            connectedAt: current.connectedAt ?? new Date().toISOString(),
            lastSignalAt: parsed.data.signaled_at
          }));
          void queryClient.invalidateQueries({ queryKey: operationsSnapshotQueryKey });
        }
      )
      .subscribe((status) => {
        if (status === "SUBSCRIBED") {
          setHealth((current) => ({
            ...current,
            connected: true,
            connectedAt: new Date().toISOString()
          }));
          return;
        }
        setHealth(disconnected);
        lastVersion.current = null;
      });
    return () => {
      setHealth(disconnected);
      lastVersion.current = null;
      void client.removeChannel(channel);
    };
  }, [queryClient]);

  return <RealtimeHealthContext.Provider value={health}>{children}</RealtimeHealthContext.Provider>;
}

export function useControlPlaneRealtimeHealth(): ClientRealtimeHealth | null {
  return useContext(RealtimeHealthContext);
}
