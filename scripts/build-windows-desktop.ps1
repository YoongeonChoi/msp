[CmdletBinding()]
param(
    [switch]$PreflightOnly,
    [switch]$RequireSignature,
    [string]$InspectPePath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$tauriManifest = Join-Path $repoRoot "apps\desktop\src-tauri\Cargo.toml"
$applicationExecutable = Join-Path $repoRoot "apps\desktop\src-tauri\target\release\kr_auto_trading_lab_desktop.exe"
$bundleDirectory = Join-Path $repoRoot "apps\desktop\src-tauri\target\release\bundle\nsis"

function Assert-CommandAvailable {
    param([Parameter(Mandatory = $true)][string]$Name)

    if ($null -eq (Get-Command -Name $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found on PATH."
    }
}

function Invoke-CheckedStep {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    Write-Host "==> $Label"
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE."
    }
}

function Get-WindowsPeSubsystem {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::Read
    )
    $reader = [System.IO.BinaryReader]::new($stream)
    try {
        if ($stream.Length -lt 64 -or $reader.ReadUInt16() -ne 0x5A4D) {
            throw "Application executable is not a valid PE file."
        }

        $stream.Position = 0x3C
        $peOffset = $reader.ReadInt32()
        if ($peOffset -lt 0 -or $peOffset + 24 -gt $stream.Length) {
            throw "Application executable has an invalid PE header offset."
        }

        $stream.Position = $peOffset
        if ($reader.ReadUInt32() -ne 0x00004550) {
            throw "Application executable has an invalid PE signature."
        }

        $optionalHeaderStart = $peOffset + 24
        $stream.Position = $peOffset + 20
        $optionalHeaderSize = $reader.ReadUInt16()
        if ($optionalHeaderSize -lt 70 -or $optionalHeaderStart + $optionalHeaderSize -gt $stream.Length) {
            throw "Application executable has an invalid PE optional header size."
        }

        $stream.Position = $optionalHeaderStart
        $optionalHeaderMagic = $reader.ReadUInt16()
        if ($optionalHeaderMagic -notin @(0x010B, 0x020B)) {
            throw "Application executable has an unsupported PE optional header."
        }

        $stream.Position = $optionalHeaderStart + 68
        return $reader.ReadUInt16()
    }
    finally {
        $reader.Dispose()
    }
}

if ($InspectPePath) {
    $resolvedInspectPath = (Resolve-Path -LiteralPath $InspectPePath).Path
    Write-Output (Get-WindowsPeSubsystem -Path $resolvedInspectPath)
    return
}

if (-not $IsWindows) {
    throw "Windows packaging must run on Windows."
}

foreach ($tool in @("node", "npm", "cargo", "rustc")) {
    Assert-CommandAvailable -Name $tool
}

$nodeVersion = (& node --version).Trim()
if ($LASTEXITCODE -ne 0 -or $nodeVersion -notmatch '^v(?<major>[0-9]+)\.') {
    throw "Unable to determine the Node.js version."
}
if ([int]$Matches.major -lt 22) {
    throw "Node.js 22 or newer is required; found $nodeVersion."
}

Push-Location $repoRoot
try {
    Invoke-CheckedStep `
        -Label "Validate public desktop release configuration" `
        -Executable "npm" `
        -Arguments @("--workspace", "apps/desktop", "run", "verify:release-config")

    if ($PreflightOnly) {
        Write-Host "Windows desktop packaging preflight passed."
        return
    }

    Invoke-CheckedStep -Label "Desktop lint" -Executable "npm" -Arguments @("run", "desktop:lint")
    Invoke-CheckedStep -Label "Desktop typecheck" -Executable "npm" -Arguments @("run", "desktop:typecheck")
    Invoke-CheckedStep -Label "Desktop contract tests" -Executable "npm" -Arguments @("run", "desktop:test")
    Invoke-CheckedStep -Label "Desktop browser E2E" -Executable "npm" -Arguments @("run", "desktop:e2e")
    Invoke-CheckedStep `
        -Label "Tauri Cargo check" `
        -Executable "cargo" `
        -Arguments @("check", "--locked", "--manifest-path", $tauriManifest)
    Invoke-CheckedStep `
        -Label "Tauri Cargo tests" `
        -Executable "cargo" `
        -Arguments @("test", "--locked", "--manifest-path", $tauriManifest)

    $buildStartedUtc = [DateTime]::UtcNow
    Invoke-CheckedStep `
        -Label "Build Windows NSIS installer" `
        -Executable "npm" `
        -Arguments @("--workspace", "apps/desktop", "run", "bundle:windows")

    if (-not (Test-Path -LiteralPath $applicationExecutable -PathType Leaf)) {
        throw "Expected release application executable was not created."
    }
    $applicationSubsystem = Get-WindowsPeSubsystem -Path $applicationExecutable
    if ($applicationSubsystem -ne 2) {
        throw "Release application PE subsystem must be Windows GUI (2); found $applicationSubsystem."
    }

    $installers = @(
        Get-ChildItem -LiteralPath $bundleDirectory -Filter "*-setup.exe" -File |
            Where-Object { $_.LastWriteTimeUtc -ge $buildStartedUtc.AddSeconds(-5) }
    )
    if ($installers.Count -ne 1) {
        throw "Expected exactly one freshly built NSIS setup.exe, found $($installers.Count)."
    }

    $installer = $installers[0]
    $checksum = Get-FileHash -LiteralPath $installer.FullName -Algorithm SHA256
    $checksumPath = "$($installer.FullName).sha256"
    $checksumLine = "$($checksum.Hash.ToLowerInvariant())  $($installer.Name)`n"
    [System.IO.File]::WriteAllText(
        $checksumPath,
        $checksumLine,
        [System.Text.UTF8Encoding]::new($false)
    )

    $signature = Get-AuthenticodeSignature -LiteralPath $installer.FullName
    if ($RequireSignature -and $signature.Status -ne "Valid") {
        throw "Installer signature is '$($signature.Status)'; a valid signature was required."
    }

    Write-Host "Windows installer: $($installer.FullName)"
    Write-Host "Application PE subsystem: Windows GUI ($applicationSubsystem)"
    Write-Host "SHA-256: $($checksum.Hash)"
    Write-Host "Checksum file: $checksumPath"
    Write-Host "Authenticode: $($signature.Status)"
    if ($signature.Status -ne "Valid") {
        Write-Warning "The installer is not trusted-signed and may trigger Windows SmartScreen."
    }
}
finally {
    Pop-Location
}
