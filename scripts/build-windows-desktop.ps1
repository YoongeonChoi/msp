[CmdletBinding()]
param(
    [switch]$PreflightOnly,
    [switch]$RequireSignature
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$tauriManifest = Join-Path $repoRoot "apps\desktop\src-tauri\Cargo.toml"
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
