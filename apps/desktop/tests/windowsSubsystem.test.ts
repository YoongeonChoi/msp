import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const mainSource = readFileSync(
  new URL("../src-tauri/src/main.rs", import.meta.url),
  "utf8",
);
const packagingScriptUrl = new URL(
  "../../../scripts/build-windows-desktop.ps1",
  import.meta.url,
);
const packagingScript = readFileSync(packagingScriptUrl, "utf8");
const packagingScriptPath = fileURLToPath(packagingScriptUrl);

assert.match(
  mainSource,
  /^#!\[cfg_attr\(not\(debug_assertions\), windows_subsystem = "windows"\)\]\r?\n/,
  "Windows release builds must suppress the console window from the crate root",
);

assert.match(
  packagingScript,
  /Release application PE subsystem must be Windows GUI \(2\)/,
  "Windows packaging must reject a console-subsystem release executable",
);

function createPeFixture(
  optionalHeaderMagic: number,
  subsystem: number,
  declaredOptionalHeaderSize?: number,
): Buffer {
  const peOffset = 0x80;
  const optionalHeaderSize = optionalHeaderMagic === 0x020b ? 240 : 224;
  const fixture = Buffer.alloc(peOffset + 24 + optionalHeaderSize);

  fixture.writeUInt16LE(0x5a4d, 0);
  fixture.writeInt32LE(peOffset, 0x3c);
  fixture.writeUInt32LE(0x00004550, peOffset);
  fixture.writeUInt16LE(
    declaredOptionalHeaderSize ?? optionalHeaderSize,
    peOffset + 20,
  );
  fixture.writeUInt16LE(optionalHeaderMagic, peOffset + 24);
  fixture.writeUInt16LE(subsystem, peOffset + 24 + 68);

  return fixture;
}

function inspectPe(path: string) {
  return spawnSync(
    "pwsh",
    [
      "-NoLogo",
      "-NoProfile",
      "-File",
      packagingScriptPath,
      "-InspectPePath",
      path,
    ],
    { encoding: "utf8" },
  );
}

const fixtureDirectory = mkdtempSync(join(tmpdir(), "desktop-pe-subsystem-"));
try {
  const pe32GuiPath = join(fixtureDirectory, "pe32-gui.exe");
  const pe32PlusGuiPath = join(fixtureDirectory, "pe32-plus-gui.exe");
  const pe32ConsolePath = join(fixtureDirectory, "pe32-console.exe");
  const truncatedPath = join(fixtureDirectory, "truncated.exe");
  const undersizedOptionalHeaderPath = join(
    fixtureDirectory,
    "undersized-optional-header.exe",
  );
  const oversizedOptionalHeaderPath = join(
    fixtureDirectory,
    "oversized-optional-header.exe",
  );

  writeFileSync(pe32GuiPath, createPeFixture(0x010b, 2));
  writeFileSync(pe32PlusGuiPath, createPeFixture(0x020b, 2));
  writeFileSync(pe32ConsolePath, createPeFixture(0x010b, 3));
  writeFileSync(truncatedPath, createPeFixture(0x020b, 2).subarray(0, 0x90));
  writeFileSync(undersizedOptionalHeaderPath, createPeFixture(0x020b, 2, 69));
  writeFileSync(
    oversizedOptionalHeaderPath,
    createPeFixture(0x020b, 2, 0xffff),
  );

  for (const guiFixturePath of [pe32GuiPath, pe32PlusGuiPath]) {
    const result = inspectPe(guiFixturePath);
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout.trim(), "2");
  }

  const consoleResult = inspectPe(pe32ConsolePath);
  assert.equal(consoleResult.status, 0, consoleResult.stderr);
  assert.equal(consoleResult.stdout.trim(), "3");

  const truncatedResult = inspectPe(truncatedPath);
  assert.notEqual(truncatedResult.status, 0);
  assert.match(
    truncatedResult.stderr,
    /invalid PE header offset|invalid PE optional header size/,
  );

  for (const invalidSizeFixturePath of [
    undersizedOptionalHeaderPath,
    oversizedOptionalHeaderPath,
  ]) {
    const result = inspectPe(invalidSizeFixturePath);
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /invalid PE optional header size/);
  }
} finally {
  rmSync(fixtureDirectory, { recursive: true, force: true });
}

console.log("Windows release subsystem guard passed");
