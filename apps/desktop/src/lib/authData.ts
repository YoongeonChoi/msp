import {
  clearPersistedSupabaseSession,
  hasSupabaseConfig,
  supabase
} from "./supabaseClient";

export class AuthDataError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AuthDataError";
  }
}

export interface AuthRoleState {
  readonly signedIn: boolean;
  readonly email: string | null;
  readonly role: string | null;
  readonly roles: readonly string[];
  readonly warning: string | null;
}

export interface AuthCredentials {
  readonly email: string;
  readonly password: string;
}

export type DeviceConnectionResult = "connected" | "discarded";

export interface DeviceConnectionGuardSnapshot {
  readonly shouldDiscardAuthenticatedSession: boolean;
  readonly cleanupError: Error | null;
}

export interface DeviceConnectionGuardStore {
  readonly subscribe: (listener: () => void) => () => void;
  readonly getSnapshot: () => DeviceConnectionGuardSnapshot;
}

export interface DeviceConnectionBackend {
  readonly authenticate: (input: AuthCredentials) => Promise<string | null>;
  readonly getCurrentAccessToken: () => Promise<string | null>;
  readonly disconnectCurrentDevice: () => Promise<void>;
}

export interface DeviceConnectionController {
  readonly connect: (input: AuthCredentials) => Promise<DeviceConnectionResult>;
  readonly discardPending: () => void;
  readonly blockCleanupFailure: (error: unknown) => void;
  readonly subscribeGuard: (listener: () => void) => () => void;
  readonly getGuardSnapshot: () => DeviceConnectionGuardSnapshot;
  readonly shouldDiscardAuthenticatedSession: () => boolean;
  readonly getCleanupError: () => Error | null;
  readonly retryCleanup: () => Promise<void>;
  readonly disconnect: () => Promise<void>;
}

export interface LocalSignOutClient {
  readonly auth: {
    readonly signOut: (options: { readonly scope: "local" }) => Promise<{ readonly error: unknown }>;
    readonly stopAutoRefresh?: () => void;
  };
}

export interface PasswordSignInClient {
  readonly auth: {
    readonly signInWithPassword: (input: AuthCredentials) => Promise<{
      readonly data: { readonly session: { readonly access_token: string } | null };
      readonly error: unknown;
    }>;
    readonly startAutoRefresh?: () => void;
  };
}

export function isSupabaseReady(): boolean {
  return hasSupabaseConfig && supabase !== null;
}

export async function fetchAuthRole(): Promise<AuthRoleState> {
  const client = requireClient();
  const sessionResult = await client.auth.getSession();
  if (sessionResult.error) {
    throw new AuthDataError("저장된 운영 세션을 확인하지 못했습니다.");
  }
  if (sessionResult.data.session === null) {
    return signedOutRoleState();
  }
  const userResult = await client.auth.getUser();
  if (userResult.error) {
    throw new AuthDataError("저장된 운영 세션을 확인하지 못했습니다.");
  }
  if (!userResult.data.user) {
    return signedOutRoleState();
  }

  const user = userResult.data.user;
  return {
    signedIn: true,
    email: user.email ?? null,
    role: null,
    roles: [],
    warning: null
  };
}

function signedOutRoleState(): AuthRoleState {
  return {
    signedIn: false,
    email: null,
    role: null,
    roles: [],
    warning: "이 기기를 운영 계정에 연결해야 합니다."
  };
}

export function createDeviceConnectionController(
  backend: DeviceConnectionBackend
): DeviceConnectionController {
  let generation = 0;
  let activeConnection: Promise<DeviceConnectionResult> | null = null;
  let activeCleanup: Promise<void> | null = null;
  let discardPending = false;
  let cleanupError: Error | null = null;
  const guardListeners = new Set<() => void>();
  let guardSnapshot: DeviceConnectionGuardSnapshot = {
    shouldDiscardAuthenticatedSession: false,
    cleanupError: null
  };

  const publishGuardState = () => {
    const shouldDiscardAuthenticatedSession = discardPending || cleanupError !== null;
    if (
      guardSnapshot.shouldDiscardAuthenticatedSession === shouldDiscardAuthenticatedSession &&
      guardSnapshot.cleanupError === cleanupError
    ) {
      return;
    }
    guardSnapshot = { shouldDiscardAuthenticatedSession, cleanupError };
    guardListeners.forEach((listener) => listener());
  };

  const updateDiscardState = () => {
    discardPending = activeConnection !== null || activeCleanup !== null || cleanupError !== null;
    publishGuardState();
  };

  const connect = (input: AuthCredentials): Promise<DeviceConnectionResult> => {
    if (activeConnection !== null || activeCleanup !== null || cleanupError !== null) {
      return Promise.reject(new AuthDataError("이전 기기 연결 요청을 안전하게 정리하는 중입니다."));
    }

    discardPending = false;
    const requestGeneration = ++generation;
    const operation = (async () => {
      const returnedAccessToken = await backend.authenticate(input);
      if (requestGeneration === generation) {
        return "connected";
      }

      try {
        await discardReturnedSession(backend, returnedAccessToken);
      } catch (error) {
        cleanupError = normalizeControllerError(error);
        discardPending = true;
        publishGuardState();
        throw cleanupError;
      }
      return "discarded";
    })();
    activeConnection = operation;
    void operation.finally(() => {
      if (activeConnection === operation) {
        activeConnection = null;
        updateDiscardState();
      }
    }).catch(() => undefined);
    return operation;
  };

  const runCleanup = (): Promise<void> => {
    generation += 1;
    discardPending = true;
    publishGuardState();
    if (activeCleanup !== null) {
      return activeCleanup;
    }

    const connectionToDrain = activeConnection;
    cleanupError = null;
    publishGuardState();
    const operation = (async () => {
      let disconnectError: Error | null = null;
      try {
        await backend.disconnectCurrentDevice();
      } catch (error) {
        disconnectError = normalizeControllerError(error);
      }

      if (connectionToDrain !== null) {
        try {
          await connectionToDrain;
        } catch {
          // The connection path records only cleanup failures. Authentication
          // failures do not make a completed local disconnect unsafe.
        }
      }

      if (cleanupError !== null) {
        throw cleanupError;
      }
      if (disconnectError !== null) {
        cleanupError = disconnectError;
        throw disconnectError;
      }
    })();
    activeCleanup = operation;
    void operation.finally(() => {
      if (activeCleanup === operation) {
        activeCleanup = null;
        updateDiscardState();
      }
    }).catch(() => undefined);
    return operation;
  };

  return {
    connect,
    discardPending: () => {
      generation += 1;
      updateDiscardState();
    },
    blockCleanupFailure: (error) => {
      generation += 1;
      cleanupError = normalizeControllerError(error);
      discardPending = true;
      publishGuardState();
    },
    subscribeGuard: (listener) => {
      guardListeners.add(listener);
      return () => guardListeners.delete(listener);
    },
    getGuardSnapshot: () => guardSnapshot,
    shouldDiscardAuthenticatedSession: () => guardSnapshot.shouldDiscardAuthenticatedSession,
    getCleanupError: () => cleanupError,
    retryCleanup: runCleanup,
    disconnect: runCleanup
  };
}

function normalizeControllerError(error: unknown): Error {
  return error instanceof Error
    ? error
    : new AuthDataError("이 기기에 저장된 운영 세션을 제거하지 못했습니다.");
}

async function discardReturnedSession(
  backend: DeviceConnectionBackend,
  returnedAccessToken: string | null
): Promise<void> {
  let shouldDisconnect = returnedAccessToken === null;
  try {
    const currentAccessToken = await backend.getCurrentAccessToken();
    shouldDisconnect = shouldDisconnect || currentAccessToken === returnedAccessToken;
  } catch {
    // If the current session cannot be compared, failing closed is safer than
    // leaving a cancelled persistent session attached to this device.
    shouldDisconnect = true;
  }

  if (shouldDisconnect) {
    await backend.disconnectCurrentDevice();
  }
}

export async function signOutCurrentDeviceWithClient(
  client: LocalSignOutClient,
  clearPersistedSession: () => void = clearPersistedSupabaseSession
): Promise<void> {
  try {
    client.auth.stopAutoRefresh?.();
  } catch {
    // Local storage removal below remains the authoritative disconnect path.
  }

  const initialError = await localSignOutError(client);
  clearPersistedSessionOrThrow(clearPersistedSession);
  if (initialError === null) {
    return;
  }

  // Supabase can return before removing its storage when the server revoke
  // call fails. Once storage is empty, a second local sign-out reaches the
  // SDK's local removal/notification path without reusing the old token.
  const cleanupError = await localSignOutError(client);
  clearPersistedSessionOrThrow(clearPersistedSession);
  if (cleanupError !== null) {
    throw new AuthDataError("이 기기의 운영 계정 연결을 해제하지 못했습니다.");
  }
}

async function localSignOutError(client: LocalSignOutClient): Promise<unknown | null> {
  try {
    const result = await client.auth.signOut({ scope: "local" });
    return result.error ?? null;
  } catch (error) {
    return error;
  }
}

function clearPersistedSessionOrThrow(clearPersistedSession: () => void): void {
  try {
    clearPersistedSession();
  } catch {
    throw new AuthDataError("이 기기에 저장된 운영 세션을 제거하지 못했습니다.");
  }
}

export async function authenticateCurrentDeviceWithClient(
  client: PasswordSignInClient,
  input: AuthCredentials
): Promise<string | null> {
  const result = await client.auth.signInWithPassword(input);
  if (result.error) {
    throw new AuthDataError("이 기기를 운영 계정에 연결하지 못했습니다.");
  }
  client.auth.startAutoRefresh?.();
  return result.data.session?.access_token ?? null;
}

const deviceConnectionController = createDeviceConnectionController({
  authenticate: async (input) => {
    const client = requireClient();
    return authenticateCurrentDeviceWithClient(client, input);
  },
  getCurrentAccessToken: async () => {
    const client = requireClient();
    const result = await client.auth.getSession();
    if (result.error) {
      throw new AuthDataError("저장된 운영 세션을 확인하지 못했습니다.");
    }
    return result.data.session?.access_token ?? null;
  },
  disconnectCurrentDevice: async () => signOutCurrentDeviceWithClient(requireClient())
});

export async function signInWithPassword(input: AuthCredentials): Promise<DeviceConnectionResult> {
  return deviceConnectionController.connect(input);
}

export function discardPendingDeviceConnection(): void {
  deviceConnectionController.discardPending();
}

export function blockDeviceConnectionCleanup(error: unknown): void {
  deviceConnectionController.blockCleanupFailure(error);
}

export function subscribeDeviceConnectionGuard(listener: () => void): () => void {
  return deviceConnectionController.subscribeGuard(listener);
}

export function getDeviceConnectionGuardSnapshot(): DeviceConnectionGuardSnapshot {
  return deviceConnectionController.getGuardSnapshot();
}

export const deviceConnectionGuardStore: DeviceConnectionGuardStore = {
  subscribe: subscribeDeviceConnectionGuard,
  getSnapshot: getDeviceConnectionGuardSnapshot
};

export function shouldDiscardPendingDeviceConnection(): boolean {
  return deviceConnectionController.shouldDiscardAuthenticatedSession();
}

export function getDeviceConnectionCleanupError(): Error | null {
  return deviceConnectionController.getCleanupError();
}

export async function retryDeviceConnectionCleanup(): Promise<void> {
  return deviceConnectionController.retryCleanup();
}

export async function signOut(): Promise<void> {
  return deviceConnectionController.disconnect();
}

function requireClient() {
  if (!isSupabaseReady() || supabase === null) {
    throw new AuthDataError("인증 연결 정보가 설정되지 않았습니다.");
  }
  return supabase;
}
