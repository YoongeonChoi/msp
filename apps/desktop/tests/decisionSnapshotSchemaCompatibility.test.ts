import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const testDir = dirname(fileURLToPath(import.meta.url));
const supabaseDataPath = resolve(testDir, "../src/lib/supabaseData.ts");
const source = readFileSync(supabaseDataPath, "utf8");

assert.doesNotMatch(
  source,
  /from\("decision_snapshots"\)[\s\S]{0,220}\.(?:gte|order)\("decided_at"/,
  "Desktop decision_snapshots reads must not filter/order by decided_at; hosted control planes may still be on the base created_at schema."
);

console.log("decision snapshot schema compatibility guard passed");
