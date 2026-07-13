import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const testDir = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(resolve(testDir, "../src/lib/supabaseData.ts"), "utf8");

for (const functionName of ["upsertWatchlistItem", "reviewAiUpgradeCandidate", "updateBotSettings"]) {
  const start = source.indexOf(`export async function ${functionName}`);
  const end = source.indexOf("\nexport async function", start + 1);
  assert.ok(start >= 0, `${functionName} source must be present`);
  const mutationSource = source.slice(start, end < 0 ? source.length : end);
  assert.match(mutationSource, /\.select\("id"\)/, `${functionName} must return the affected row`);
  assert.match(mutationSource, /length !== 1/, `${functionName} must reject a silent zero-row mutation`);
}

console.log("data mutation guards passed");
