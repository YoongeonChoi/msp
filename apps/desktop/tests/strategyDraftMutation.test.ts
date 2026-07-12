import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const testDir = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(resolve(testDir, "../src/lib/supabaseData.ts"), "utf8");
const start = source.indexOf("export async function updateDraftStrategyJson");
const end = source.indexOf("\nexport async function", start + 1);

assert.ok(start >= 0 && end > start, "updateDraftStrategyJson source must be present");
const mutationSource = source.slice(start, end);

assert.match(mutationSource, /\.eq\("status", "draft"\)/);
assert.doesNotMatch(mutationSource, /\.in\("status"/);
assert.match(mutationSource, /\.select\("id"\)/);
assert.match(mutationSource, /length !== 1/);

console.log("strategy draft mutation guard passed");
