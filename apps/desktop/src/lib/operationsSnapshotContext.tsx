import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore
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
import {
  deviceConnectionGuardStore,
  type DeviceConnectionGuardStore
} from "./authData";

const DEFAULT_POLL_INTERVAL_MS = 15_000;
const deviceConnectionDiscardError = new Error("Cancelled device connection cleanup is incomplete");

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
  readonly guardStore?: DeviceConnectionGuardStore;
  /**
   * Scopes the production snapshot cache to the authenticated Supabase user.
   * Omit only in focused fixtures that do not own an auth session.
   */
  readonly principalId?: string | null;
  /**
   * Starts authenticated snapshot polling and Realtime only after the current
   * device session has been confirmed. Disabled providers expose no cached
   * snapshot or transport error from a previous session.
   */
  readonly enabled?: boolean;
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
  guardStore = deviceConnectionGuardStore,
  principalId,
  enabled = true,
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
  const deviceConnectionGuard = useSyncExternalStore(
    guardStore.subscribe,
    guardStore.getSnapshot,
    guardStore.getSnapshot
  );
  const snapshotQueryKey = useMemo(
    () =>
      principalId === undefined
        ? operationsSnapshotQueryKey
        : ([...operationsSnapshotQueryKey, "principal", principalId] as const),
    [principalId]
  );
  const effectiveEnabled =
    enabled &&
    principalId !== null &&
    !deviceConnectionGuard.shouldDiscardAuthenticatedSession;

  const query = useQuery({
    queryKey: snapshotQueryKey,
    queryFn: dataApi.fetchSnapshot,
    enabled: effectiveEnabled,
    retry: false,
    refetchInterval: pollIntervalMs,
    refetchIntervalInBackground: false
  });

  useEffect(() => {
    if (!effectiveEnabled) {
      setObservedRealtime(disconnectedRealtimeHealth);
      lastSignalVersion.current = null;
      return;
    }

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
            queryKey: snapshotQueryKey,
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
  }, [effectiveEnabled, queryClient, realtimeOverride, snapshotQueryKey]);

  const invalidateSnapshot = useCallback(
    () =>
      queryClient.invalidateQueries({
        queryKey: snapshotQueryKey,
        exact: true
      }),
    [queryClient, snapshotQueryKey]
  );

  const effectiveError = enabled
    ? deviceConnectionGuard.shouldDiscardAuthenticatedSession
      ? deviceConnectionGuard.cleanupError ?? deviceConnectionDiscardError
      : query.error
    : null;
  const effectiveRealtime = effectiveEnabled
    ? realtimeOverride === undefined
      ? observedRealtime
      : realtimeOverride
    : disconnectedRealtimeHealth;
  const value = useMemo<OperationsSnapshotContextValue>(
    () => ({
      dataApi,
      query,
      snapshot: effectiveEnabled && effectiveError === null ? query.data : undefined,
      error: effectiveError,
      isLoading: effectiveEnabled && query.isLoading,
      isFetching: effectiveEnabled && query.isFetching,
      isOnline,
      realtime: effectiveRealtime,
      updatedAt: effectiveEnabled ? query.dataUpdatedAt : 0,
      refetchSnapshot: query.refetch,
      invalidateSnapshot
    }),
    [dataApi, effectiveEnabled, effectiveError, effectiveRealtime, invalidateSnapshot, isOnline, query]
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
