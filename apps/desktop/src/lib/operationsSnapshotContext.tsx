import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState
} from "react";
import {
  useQuery,
  useQueryClient,
  type UseQueryResult
} from "@tanstack/react-query";
import { z } from "zod";

import type { OperationsSnapshot } from "./operationsContracts";
import {
  operationsDataApi,
  operationsSnapshotQueryKey,
  type OperationsDataApi
} from "./operationsData";
import type { ClientRealtimeHealth } from "./controlPlaneRealtime";
import { supabase } from "./supabaseClient";
import { useOnlineStatus } from "./useOnlineStatus";

const DEFAULT_POLL_INTERVAL_MS = 15_000;

const disconnectedRealtimeHealth: ClientRealtimeHealth = {
  connected: false,
  connectedAt: null,
  lastSignalAt: null
};

const controlPlaneSignalSchema = z
  .object({
    id: z.literal("singleton"),
    signal_version: z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER),
    signaled_at: z.string().datetime({ offset: true }),
    signal_kind: z.literal("snapshot_invalidated")
  })
  .strict();

export interface OperationsSnapshotContextValue {
  readonly dataApi: OperationsDataApi;
  readonly query: UseQueryResult<OperationsSnapshot, Error>;
  readonly snapshot: OperationsSnapshot | undefined;
  readonly error: Error | null;
  readonly isLoading: boolean;
  readonly isFetching: boolean;
  readonly isOnline: boolean;
  readonly realtime: ClientRealtimeHealth | null;
  readonly updatedAt: number;
  readonly refetchSnapshot: UseQueryResult<OperationsSnapshot, Error>["refetch"];
  readonly invalidateSnapshot: () => Promise<void>;
}

export interface OperationsSnapshotProviderProps {
  readonly children: ReactNode;
  readonly dataApi?: OperationsDataApi;
  readonly onlineOverride?: boolean;
  /**
   * Test-only health injection. Omit this prop in production so the provider
   * owns the single control-plane Realtime subscription.
   */
  readonly realtimeOverride?: ClientRealtimeHealth | null;
  readonly pollIntervalMs?: number | false;
}

const OperationsSnapshotContext = createContext<OperationsSnapshotContextValue | null>(null);

/**
 * Owns the desktop operations read model lifecycle: one query observer, one
 * 15-second polling definition, and one Realtime invalidation subscription.
 * Consumers must share this provider instead of declaring their own snapshot
 * queries, so headers, pages, and access controls evaluate the same snapshot.
 */
export function OperationsSnapshotProvider({
  children,
  dataApi = operationsDataApi,
  onlineOverride,
  realtimeOverride,
  pollIntervalMs = DEFAULT_POLL_INTERVAL_MS
}: OperationsSnapshotProviderProps) {
  const queryClient = useQueryClient();
  const isOnline = useOnlineStatus(onlineOverride);
  const [observedRealtime, setObservedRealtime] = useState<ClientRealtimeHealth>(
    disconnectedRealtimeHealth
  );
  const lastSignalVersion = useRef<number | null>(null);

  const query = useQuery({
    queryKey: operationsSnapshotQueryKey,
    queryFn: dataApi.fetchSnapshot,
    retry: false,
    refetchInterval: pollIntervalMs,
    refetchIntervalInBackground: false
  });

  useEffect(() => {
    if (realtimeOverride !== undefined) {
      return;
    }

    const client = supabase;
    if (client === null || import.meta.env?.VITE_SUPABASE_REALTIME_DISABLED === "true") {
      setObservedRealtime(disconnectedRealtimeHealth);
      lastSignalVersion.current = null;
      return;
    }

    const channel = client
      .channel("desktop-control-plane-signal-v1")
      .on(
        "postgres_changes",
        { event: "UPDATE", schema: "api", table: "control_plane_signal" },
        (payload) => {
          const parsed = controlPlaneSignalSchema.safeParse(payload.new);
          if (
            !parsed.success ||
            (lastSignalVersion.current !== null &&
              parsed.data.signal_version <= lastSignalVersion.current)
          ) {
            setObservedRealtime(disconnectedRealtimeHealth);
            return;
          }

          lastSignalVersion.current = parsed.data.signal_version;
          setObservedRealtime((current) => ({
            connected: true,
            connectedAt: current.connectedAt ?? new Date().toISOString(),
            lastSignalAt: parsed.data.signaled_at
          }));
          void queryClient.invalidateQueries({
            queryKey: operationsSnapshotQueryKey,
            exact: true
          });
        }
      )
      .subscribe((status) => {
        if (status === "SUBSCRIBED") {
          setObservedRealtime((current) => ({
            ...current,
            connected: true,
            connectedAt: new Date().toISOString()
          }));
          return;
        }

        setObservedRealtime(disconnectedRealtimeHealth);
        lastSignalVersion.current = null;
      });

    return () => {
      lastSignalVersion.current = null;
      void client.removeChannel(channel);
    };
  }, [queryClient, realtimeOverride]);

  const invalidateSnapshot = useCallback(
    () =>
      queryClient.invalidateQueries({
        queryKey: operationsSnapshotQueryKey,
        exact: true
      }),
    [queryClient]
  );

  const realtime = realtimeOverride === undefined ? observedRealtime : realtimeOverride;
  const value = useMemo<OperationsSnapshotContextValue>(
    () => ({
      dataApi,
      query,
      snapshot: query.data,
      error: query.error,
      isLoading: query.isLoading,
      isFetching: query.isFetching,
      isOnline,
      realtime,
      updatedAt: query.dataUpdatedAt,
      refetchSnapshot: query.refetch,
      invalidateSnapshot
    }),
    [dataApi, invalidateSnapshot, isOnline, query, realtime]
  );

  return (
    <OperationsSnapshotContext.Provider value={value}>
      {children}
    </OperationsSnapshotContext.Provider>
  );
}

export function useOperationsSnapshot(): OperationsSnapshotContextValue {
  const value = useContext(OperationsSnapshotContext);
  if (value === null) {
    throw new Error("useOperationsSnapshot must be used within OperationsSnapshotProvider");
  }
  return value;
}

export function useOptionalOperationsSnapshot(): OperationsSnapshotContextValue | null {
  return useContext(OperationsSnapshotContext);
}
