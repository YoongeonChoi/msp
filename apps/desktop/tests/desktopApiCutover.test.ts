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
const operationsSource = readFileSync(resolve(sourceRoot, "lib/operationsData.ts"), "utf8");
const realtimeSource = readFileSync(resolve(sourceRoot, "lib/controlPlaneRealtime.tsx"), "utf8");
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
assert.match(authSource, /fetchOperationsSnapshot/);
assert.doesNotMatch(sharedSource, /\.default\s*\(/, "strict shared contracts must not repair missing fields");
assert.match(operationsSource, /\.schema\("api"\)\.rpc\(/);
assert.doesNotMatch(operationsSource, /\.schema\("public"\)|\.from\s*\(/);
assert.match(realtimeSource, /schema: "api", table: "control_plane_signal"/);
assert.match(realtimeSource, /event: "UPDATE"/);
assert.match(realtimeSource, /z\.literal\("singleton"\)/);
assert.match(realtimeSource, /z\.literal\("snapshot_invalidated"\)/);
assert.doesNotMatch(realtimeSource, /schema: "public"|bot_settings|worker_heartbeats|decision_snapshots/);

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
