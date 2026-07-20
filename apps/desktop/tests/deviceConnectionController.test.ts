import assert from "node:assert/strict";

import {
  AuthDataError,
  authenticateCurrentDeviceWithClient,
  createDeviceConnectionController,
  signOutCurrentDeviceWithClient
} from "../src/lib/authData";
import { clearSupabaseAuthStorage } from "../src/lib/supabaseClient";

const firstAuthentication = deferred<string | null>();
let currentAccessToken: string | null = "cancelled-token";
let authenticationCalls = 0;
let disconnectCalls = 0;
const controller = createDeviceConnectionController({
  authenticate: async () => {
    authenticationCalls += 1;
    return firstAuthentication.promise;
  },
  getCurrentAccessToken: async () => currentAccessToken,
  disconnectCurrentDevice: async () => {
    disconnectCalls += 1;
    currentAccessToken = null;
  }
});

const cancelledConnection = controller.connect({
  email: "operator@example.test",
  password: "test-only-password"
});
await assert.rejects(
  controller.connect({ email: "operator@example.test", password: "duplicate" }),
  (error: unknown) => error instanceof AuthDataError && /정리하는 중/.test(error.message)
);
assert.equal(authenticationCalls, 1, "concurrent credential submissions must be serialized");

controller.discardPending();
assert.equal(
  controller.shouldDiscardAuthenticatedSession(),
  true,
  "auth events from a cancelled in-flight request must remain fail-closed"
);
firstAuthentication.resolve("cancelled-token");
assert.equal(await cancelledConnection, "discarded");
await Promise.resolve();
assert.equal(controller.shouldDiscardAuthenticatedSession(), false);
assert.equal(disconnectCalls, 1, "a cancelled late success must remove its persisted local session");

const failedCleanupAuthentication = deferred<string | null>();
let cleanupMustFail = true;
let failedCleanupAuthenticationCalls = 0;
const failedCleanupController = createDeviceConnectionController({
  authenticate: async () => {
    failedCleanupAuthenticationCalls += 1;
    return failedCleanupAuthentication.promise;
  },
  getCurrentAccessToken: async () => "failed-cleanup-token",
  disconnectCurrentDevice: async () => {
    if (cleanupMustFail) {
      throw new Error("synthetic local cleanup failure");
    }
  }
});
const failedCleanupConnection = failedCleanupController.connect({
  email: "operator@example.test",
  password: "test-only-password"
});
failedCleanupController.discardPending();
failedCleanupAuthentication.resolve("failed-cleanup-token");
await assert.rejects(failedCleanupConnection, /synthetic local cleanup failure/);
assert.equal(
  failedCleanupController.shouldDiscardAuthenticatedSession(),
  true,
  "a cleanup failure must keep authenticated events fail-closed"
);
assert.match(failedCleanupController.getCleanupError()?.message ?? "", /synthetic local cleanup failure/);
await assert.rejects(
  failedCleanupController.connect({ email: "operator@example.test", password: "blocked" }),
  /정리하는 중/
);
assert.equal(failedCleanupAuthenticationCalls, 1, "cleanup failure must block another credential request");
cleanupMustFail = false;
await failedCleanupController.retryCleanup();
assert.equal(failedCleanupController.getCleanupError(), null);
assert.equal(failedCleanupController.shouldDiscardAuthenticatedSession(), false);

const acceptedController = createDeviceConnectionController({
  authenticate: async () => "accepted-token",
  getCurrentAccessToken: async () => "accepted-token",
  disconnectCurrentDevice: async () => {
    disconnectCalls += 1;
  }
});
assert.equal(
  await acceptedController.connect({ email: "operator@example.test", password: "test-only-password" }),
  "connected"
);
assert.equal(disconnectCalls, 1, "an accepted connection must remain attached");
await acceptedController.disconnect();
assert.equal(disconnectCalls, 2);

const racingAuthentication = deferred<string | null>();
let racingAccessToken: string | null = "existing-token";
let racingDisconnectCalls = 0;
const racingController = createDeviceConnectionController({
  authenticate: async () => {
    const token = await racingAuthentication.promise;
    racingAccessToken = token;
    return token;
  },
  getCurrentAccessToken: async () => racingAccessToken,
  disconnectCurrentDevice: async () => {
    racingDisconnectCalls += 1;
    racingAccessToken = null;
  }
});
const racingGuardTransitions: boolean[] = [];
const unsubscribeRacingGuard = racingController.subscribeGuard(() => {
  racingGuardTransitions.push(
    racingController.getGuardSnapshot().shouldDiscardAuthenticatedSession
  );
});
const racingConnection = racingController.connect({
  email: "operator@example.test",
  password: "test-only-password"
});
const racingDisconnect = racingController.disconnect();
assert.equal(
  racingController.shouldDiscardAuthenticatedSession(),
  true,
  "disconnect must guard auth events until an in-flight connection has drained"
);
racingAuthentication.resolve("late-token");
assert.equal(await racingConnection, "discarded");
await racingDisconnect;
assert.equal(racingAccessToken, null, "a late successful connection must not survive disconnect");
assert.equal(racingDisconnectCalls, 2, "disconnect must remove both the prior and late session");
assert.equal(racingController.shouldDiscardAuthenticatedSession(), false);
assert.deepEqual(
  racingGuardTransitions,
  [true, false],
  "React subscribers must observe both the fail-closed and released guard states"
);
unsubscribeRacingGuard();

let blockedCleanupCalls = 0;
const blockedCleanupController = createDeviceConnectionController({
  authenticate: async () => "blocked-token",
  getCurrentAccessToken: async () => "blocked-token",
  disconnectCurrentDevice: async () => {
    blockedCleanupCalls += 1;
  }
});
blockedCleanupController.blockCleanupFailure(new Error("synthetic storage removal failure"));
assert.equal(blockedCleanupController.shouldDiscardAuthenticatedSession(), true);
await assert.rejects(
  blockedCleanupController.connect({ email: "operator@example.test", password: "blocked" }),
  /정리하는 중/
);
await blockedCleanupController.retryCleanup();
assert.equal(blockedCleanupCalls, 1);
assert.equal(blockedCleanupController.getCleanupError(), null);
assert.equal(blockedCleanupController.shouldDiscardAuthenticatedSession(), false);

let signOutScope: string | null = null;
await signOutCurrentDeviceWithClient({
  auth: {
    signOut: async (options) => {
      signOutScope = options.scope;
      return { error: null };
    }
  }
});
assert.equal(signOutScope, "local", "device disconnect must not terminate sessions on other devices");

let persistedSession = true;
let fallbackSignOutCalls = 0;
let autoRefreshStops = 0;
await signOutCurrentDeviceWithClient(
  {
    auth: {
      stopAutoRefresh: () => {
        autoRefreshStops += 1;
      },
      signOut: async () => {
        fallbackSignOutCalls += 1;
        return { error: persistedSession ? new Error("synthetic revoke failure") : null };
      }
    }
  },
  () => {
    persistedSession = false;
  }
);
assert.equal(persistedSession, false, "a revoke failure must still remove the persisted local session");
assert.equal(fallbackSignOutCalls, 2, "the SDK local notification path must run after storage removal");
assert.equal(autoRefreshStops, 1);

const removedStorageKeys: string[] = [];
clearSupabaseAuthStorage(
  { removeItem: (key) => removedStorageKeys.push(key) },
  "test-auth-key"
);
assert.deepEqual(removedStorageKeys, [
  "test-auth-key",
  "test-auth-key-code-verifier",
  "test-auth-key-user"
]);

let autoRefreshStarts = 0;
assert.equal(
  await authenticateCurrentDeviceWithClient(
    {
      auth: {
        signInWithPassword: async () => ({
          data: { session: { access_token: "reconnected-token" } },
          error: null
        }),
        startAutoRefresh: () => {
          autoRefreshStarts += 1;
        }
      }
    },
    { email: "operator@example.test", password: "test-only-password" }
  ),
  "reconnected-token"
);
assert.equal(autoRefreshStarts, 1, "same-process reconnection must restart token auto-refresh");

await assert.rejects(
  signOutCurrentDeviceWithClient({
    auth: {
      signOut: async () => ({ error: new Error("synthetic sign-out failure") })
    }
  }),
  (error: unknown) => error instanceof AuthDataError && /연결을 해제하지 못했습니다/.test(error.message)
);

console.log("device connection controller safety passed");

function deferred<T>(): {
  readonly promise: Promise<T>;
  readonly resolve: (value: T) => void;
} {
  let resolvePromise: ((value: T) => void) | undefined;
  const promise = new Promise<T>((resolve) => {
    resolvePromise = resolve;
  });
  return {
    promise,
    resolve: (value) => {
      assert.ok(resolvePromise);
      resolvePromise(value);
    }
  };
}
