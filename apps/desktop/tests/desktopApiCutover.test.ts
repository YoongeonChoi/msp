import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const testDir = dirname(fileURLToPath(import.meta.url));
const sourceRoot = resolve(testDir, "../src");
const sourceFiles = collectSourceFiles(sourceRoot);
const allSource = sourceFiles.map((path) => readFileSync(path, "utf8")).join("\n");
const appSource = readFileSync(resolve(sourceRoot, "App.tsx"), "utf8");
const navigationSource = readFileSync(resolve(sourceRoot, "lib/navigation.ts"), "utf8");
const authSource = readFileSync(resolve(sourceRoot, "lib/authData.ts"), "utf8");
const settingsSource = readFileSync(resolve(sourceRoot, "pages/SettingsPage.tsx"), "utf8");
const operationsSource = readFileSync(resolve(sourceRoot, "lib/operationsData.ts"), "utf8");
const snapshotProviderSource = readFileSync(
  resolve(sourceRoot, "lib/operationsSnapshotContext.tsx"),
  "utf8"
);
const sharedSourceRoot = resolve(testDir, "../../../packages/shared/src");
const sharedSource = collectSourceFiles(sharedSourceRoot).map((path) => readFileSync(path, "utf8")).join("\n");

assert.doesNotMatch(
  allSource,
  /\.from\s*\(/,
  "desktop source must contain zero direct public-table query or mutation calls"
);
assert.doesNotMatch(appSource, /useRealtimeInvalidation|DashboardPage|OrdersPage|StrategyLabPage|WatchlistPage/);
assert.match(appSource, /ControlPage/);
assert.match(appSource, /SettingsPage/);
assert.match(navigationSource, /export type PageKey = "control" \| "settings"/);
assert.equal(existsSync(resolve(sourceRoot, "lib/rows.ts")), false, "silent-default row parser must be removed");
assert.equal(existsSync(resolve(sourceRoot, "lib/supabaseData.ts")), false, "legacy public adapter must be removed");
assert.doesNotMatch(authSource, /fetchOperationsSnapshot/);
assert.doesNotMatch(
  settingsSource,
  /mutationFn:\s*signInWithPassword|mutationFn:\s*authApi\.connectDevice/,
  "credential input must never be stored in the React Query mutation cache"
);
assert.match(authSource, /signOut\(\{\s*scope:\s*"local"\s*\}\)/);
assert.match(appSource, /OperationsSnapshotProvider/);
assert.match(appSource, /shouldDiscardPendingDeviceConnection/);
assert.match(appSource, /blockDeviceConnectionCleanup/);
assert.match(appSource, /authRole\.data\?\.signedIn === true && sessionGuarded/);
assert.match(appSource, /invalidateQueries\(\{ queryKey: authRoleQueryKey, exact: true \}\)/);
assert.doesNotMatch(appSource, /invalidateQueries\(\)/);
assert.doesNotMatch(settingsSource, /invalidateQueries\(\)/);
assert.doesNotMatch(appSource, /ControlPlaneRealtimeProvider/);
assert.match(snapshotProviderSource, /refetchInterval:\s*pollIntervalMs/);
assert.match(snapshotProviderSource, /useSyncExternalStore/);
assert.match(
  snapshotProviderSource,
  /effectiveEnabled = enabled && !deviceConnectionGuard\.shouldDiscardAuthenticatedSession/
);
assert.match(snapshotProviderSource, /queryKey:\s*operationsSnapshotQueryKey,\s*exact:\s*true/);
assert.doesNotMatch(sharedSource, /\.default\s*\(/, "strict shared contracts must not repair missing fields");
assert.match(operationsSource, /\.schema\("api"\)\.rpc\(/);
assert.doesNotMatch(operationsSource, /\.schema\("public"\)|\.from\s*\(/);
assert.match(snapshotProviderSource, /schema: "api", table: "control_plane_signal"/);
assert.match(snapshotProviderSource, /event: "UPDATE"/);
assert.match(snapshotProviderSource, /z\.literal\("singleton"\)/);
assert.match(snapshotProviderSource, /z\.literal\("snapshot_invalidated"\)/);
assert.doesNotMatch(
  snapshotProviderSource,
  /schema: "public"|bot_settings|worker_heartbeats|decision_snapshots/
);

console.log("desktop API-only cutover guard passed");

function collectSourceFiles(directory: string): string[] {
  return readdirSync(directory).flatMap((entry) => {
    const path = join(directory, entry);
    if (statSync(path).isDirectory()) {
      return collectSourceFiles(path);
    }
    return /\.(?:ts|tsx)$/.test(entry) ? [path] : [];
  });
}
