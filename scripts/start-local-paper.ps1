[CmdletBinding()]
param(
    [switch]$NoDesktop,
    [switch]$Tauri
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $env:LOCALAPPDATA) {
    throw "LOCALAPPDATA가 없어 보호된 로컬 PAPER 상태 경로를 결정할 수 없습니다."
}
$stateDir = Join-Path $env:LOCALAPPDATA "kr-auto-trading-lab"
$workerPython = Join-Path $root "apps\worker\.venv\Scripts\python.exe"
$workerEnvPath = Join-Path $root "apps\worker\.env"
$desktopEnvPath = Join-Path $root "apps\desktop\.env.local-paper.local"
$tauriConfigPath = Join-Path $root "apps\desktop\src-tauri\tauri.local-paper.conf.json"
$helperPath = Join-Path $root "scripts\local-paper.mjs"
$workerPidPath = Join-Path $stateDir "worker.pid"
$desktopPidPath = Join-Path $stateDir "desktop.pid"
$desktopUrl = "http://127.0.0.1:1421/"

if ($NoDesktop -and $Tauri) {
    throw "-NoDesktop과 -Tauri는 함께 사용할 수 없습니다."
}
if ($PSVersionTable.PSVersion.Major -lt 7) {
    throw "로컬 PAPER launcher는 PowerShell 7(pwsh)이 필요합니다."
}
if (-not (Test-Path $workerPython) -or -not (Test-Path $workerEnvPath) -or -not (Test-Path $desktopEnvPath)) {
    throw "로컬 PAPER bootstrap이 완료되지 않았습니다. npm run local-paper:bootstrap을 먼저 실행하세요."
}
if ($Tauri -and -not (Test-Path $tauriConfigPath)) {
    throw "Tauri local PAPER 설정 파일이 없습니다."
}
if (-not (docker ps --filter "name=supabase_db_kr-auto-trading-lab" --format "{{.Names}}")) {
    throw "Local Supabase가 실행 중이 아닙니다. npm run local-paper:bootstrap을 다시 실행하세요."
}

function Read-DotEnv {
    param([Parameter(Mandatory = $true)][string]$Path)

    $values = @{}
    foreach ($rawLine in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#")) {
            continue
        }
        $separator = $line.IndexOf("=")
        if ($separator -lt 1) {
            throw "잘못된 env 항목이 있습니다: $Path"
        }
        $name = $line.Substring(0, $separator).Trim()
        $value = $line.Substring($separator + 1)
        if ($values.ContainsKey($name)) {
            throw "중복된 env key가 있습니다: $name ($Path)"
        }
        $values[$name] = $value
    }
    return $values
}

function Assert-LocalRuntimeEnvironment {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Worker,
        [Parameter(Mandatory = $true)][hashtable]$Desktop
    )

    $workerUrl = [Uri]$Worker["SUPABASE_URL"]
    $desktopUrlValue = [Uri]$Desktop["VITE_SUPABASE_URL"]
    if (
        $workerUrl.Scheme -ne "http" -or
        $workerUrl.Host -ne "127.0.0.1" -or
        $workerUrl.Port -ne 54321 -or
        $desktopUrlValue.AbsoluteUri -ne $workerUrl.AbsoluteUri
    ) {
        throw "로컬 PAPER Supabase URL은 http://127.0.0.1:54321 이어야 합니다."
    }
    if ($Worker["SUPABASE_SECRET_KEY"] -notmatch "^sb_secret_[A-Za-z0-9_-]+$") {
        throw "Worker local secret 형식이 올바르지 않습니다."
    }
    if ($Desktop["VITE_SUPABASE_PUBLISHABLE_KEY"] -notmatch "^sb_publishable_[A-Za-z0-9_-]+$") {
        throw "Desktop local publishable key 형식이 올바르지 않습니다."
    }
    $required = @{
        "ENV" = "local"
        "MOCK_PROVIDERS" = "true"
        "BOT_DEFAULT_MODE" = "paper"
        "LIVE_ORDER_EXECUTION_ENABLED" = "false"
        "TOSS_ORDER_ENDPOINT_ENABLED" = "false"
        "TOSS_ORDER_CAPABLE_CREDENTIALS" = "false"
        "EXECUTION_V2_ENABLED" = "true"
        "EXECUTION_V2_ENVIRONMENT" = "paper"
        "EXECUTION_V2_WORKER_API_ENABLED" = "true"
        "EXECUTION_V2_PAPER_RESUME_INPUT_ENABLED" = "false"
        "EXECUTION_V2_PAPER_SOURCE_INPUT_ENABLED" = "false"
        "EXECUTION_V2_ACCOUNT_ID" = "paper-primary"
    }
    foreach ($entry in $required.GetEnumerator()) {
        if ($Worker[$entry.Key] -ne $entry.Value) {
            throw "Worker env safety contract mismatch: $($entry.Key)"
        }
    }
    if ($Worker["EXECUTION_V2_WORKER_ID"] -notmatch "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$") {
        throw "EXECUTION_V2_WORKER_ID는 canonical UUID v4여야 합니다."
    }
}

function Get-RecordedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][ValidateSet("worker", "desktop")][string]$Kind
    )

    if (-not (Test-Path $Path)) {
        return $null
    }
    $rawPid = (Get-Content -Raw -LiteralPath $Path).Trim()
    if ($rawPid -notmatch "^[0-9]+$") {
        throw "손상된 managed PID 파일입니다: $Path"
    }
    $recordedPid = [int]$rawPid
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $recordedPid" -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        Remove-Item -LiteralPath $Path
        return $null
    }
    $commandLine = [string]$process.CommandLine
    $matchesKind = if ($Kind -eq "worker") {
        $commandLine -match "app\.main" -and
        [IO.Path]::GetFullPath([string]$process.ExecutablePath) -eq [IO.Path]::GetFullPath($workerPython)
    }
    else {
        $commandLine -match "apps/desktop|apps\\desktop" -and $commandLine -match "1421"
    }
    if (-not $matchesKind) {
        throw "PID 파일이 관리 대상이 아닌 프로세스를 가리킵니다. 안전을 위해 중단합니다: $Path"
    }
    return $process
}

function Assert-PortAvailable {
    param([Parameter(Mandatory = $true)][int]$Port)

    $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    if ($listener) {
        throw "127.0.0.1:$Port 포트를 다른 프로세스가 사용 중입니다. 해당 프로세스를 명시적으로 종료한 뒤 다시 실행하세요."
    }
}

function Invoke-PsqlScalar {
    param([Parameter(Mandatory = $true)][string]$Sql)

    $result = docker exec supabase_db_kr-auto-trading-lab psql -X -U supabase_admin -d postgres -Atc $Sql 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "Local PostgreSQL launcher query failed."
    }
    return ([string]$result).Trim()
}

function Invoke-NodeHelper {
    param([Parameter(Mandatory = $true)][string]$Command)

    Push-Location $root
    try {
        & node.exe $helperPath $Command
        if ($LASTEXITCODE -ne 0) {
            throw "local-paper helper failed: $Command"
        }
    }
    finally {
        Pop-Location
    }
}

function Get-MatchingWorkerProcesses {
    $expectedExecutable = [IO.Path]::GetFullPath($workerPython)
    return @(
        Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object {
                $_.ExecutablePath -and
                [IO.Path]::GetFullPath([string]$_.ExecutablePath) -eq $expectedExecutable -and
                [string]$_.CommandLine -match "(^|\s)-m\s+app\.main(\s|$)"
            }
    )
}

function Invoke-WithLauncherMutex {
    param([Parameter(Mandatory = $true)][scriptblock]$Action)

    $mutex = [System.Threading.Mutex]::new($false, "Local\KrAutoTradingLab.LocalPaperLauncher")
    $acquired = $false
    try {
        try {
            $acquired = $mutex.WaitOne(5000)
        }
        catch [System.Threading.AbandonedMutexException] {
            $acquired = $true
        }
        if (-not $acquired) {
            throw "다른 local PAPER launcher가 실행 중입니다. 완료 후 다시 시도하세요."
        }
        & $Action
    }
    finally {
        if ($acquired) {
            $mutex.ReleaseMutex()
        }
        $mutex.Dispose()
    }
}

function Wait-WorkerLease {
    param([Parameter(Mandatory = $true)][string]$HolderId)

    for ($attempt = 0; $attempt -lt 45; $attempt += 1) {
        Start-Sleep -Seconds 1
        $lease = Invoke-PsqlScalar "select exists(select 1 from private.worker_leases where account_id='paper-primary' and holder_id='$HolderId' and expires_at>clock_timestamp());"
        if ($lease -eq "t") {
            return
        }
    }
    throw "Worker가 active lease를 획득하지 못했습니다. $stateDir\worker.stderr.log를 확인하세요."
}

Invoke-WithLauncherMutex {
    $workerEnvironment = Read-DotEnv -Path $workerEnvPath
    $desktopEnvironment = Read-DotEnv -Path $desktopEnvPath
    Assert-LocalRuntimeEnvironment -Worker $workerEnvironment -Desktop $desktopEnvironment
    New-Item -ItemType Directory -Force -Path $stateDir | Out-Null

    $workerProcess = Get-RecordedProcess -Path $workerPidPath -Kind "worker"
    $matchingWorkers = @(Get-MatchingWorkerProcesses)
    if ($null -eq $workerProcess -and $matchingWorkers.Count -gt 0) {
        throw "PID 파일 밖에서 같은 Worker venv의 app.main 프로세스가 실행 중입니다. 중복 실행을 피하기 위해 중단합니다."
    }
    if ($null -ne $workerProcess -and @($matchingWorkers | Where-Object { $_.ProcessId -ne $workerProcess.ProcessId }).Count -gt 0) {
        throw "관리 Worker 외에 같은 venv의 app.main 프로세스가 더 있습니다. 중복 Worker를 명시적으로 정리하세요."
    }
    $configuredWorkerId = $workerEnvironment["EXECUTION_V2_WORKER_ID"]
    $activeLeaseHolder = Invoke-PsqlScalar "select coalesce((select holder_id::text from private.worker_leases where account_id='paper-primary' and expires_at>clock_timestamp()), '');"
    if ($null -eq $workerProcess -and $activeLeaseHolder) {
        throw "PID 파일은 없지만 paper-primary active lease가 남아 있습니다. lease 만료 또는 기존 Worker 확인 전 새 프로세스를 시작하지 않습니다."
    }
    if ($null -ne $workerProcess -and $activeLeaseHolder -and $activeLeaseHolder -ne $configuredWorkerId) {
        throw "paper-primary active lease가 설정된 Worker identity와 다릅니다."
    }
    if ($null -eq $workerProcess) {
        $worker = Start-Process -FilePath $workerPython `
            -ArgumentList @("-m", "app.main") `
            -WorkingDirectory (Join-Path $root "apps\worker") `
            -RedirectStandardOutput (Join-Path $stateDir "worker.stdout.log") `
            -RedirectStandardError (Join-Path $stateDir "worker.stderr.log") `
            -WindowStyle Hidden `
            -Environment $workerEnvironment `
            -PassThru
        Set-Content -LiteralPath $workerPidPath -Value $worker.Id -NoNewline
    }
    Wait-WorkerLease -HolderId $configuredWorkerId
    Invoke-NodeHelper -Command "verify-opening"

    Write-Output "PASS Local Supabase is running"
    Write-Output "PASS Worker process has an active paper-primary lease"
    Write-Output "PASS Worker applied account opening and the journal postcondition holds"

    if ($NoDesktop) {
        Write-Output "Logs: $stateDir"
        return
    }

    $env:VITE_SUPABASE_URL = $desktopEnvironment["VITE_SUPABASE_URL"]
    $env:VITE_SUPABASE_PUBLISHABLE_KEY = $desktopEnvironment["VITE_SUPABASE_PUBLISHABLE_KEY"]

    if ($Tauri) {
        Assert-PortAvailable -Port 1421
        Write-Output "START Tauri Local PAPER Desktop (close the window or press Ctrl+C to stop the UI)"
        Push-Location $root
        try {
            & npm.cmd --workspace apps/desktop run tauri -- dev --config src-tauri/tauri.local-paper.conf.json
            if ($LASTEXITCODE -ne 0) {
                throw "Tauri Desktop exited with code $LASTEXITCODE"
            }
        }
        finally {
            Pop-Location
        }
        return
    }

    $desktopProcess = Get-RecordedProcess -Path $desktopPidPath -Kind "desktop"
    if ($null -eq $desktopProcess) {
        Assert-PortAvailable -Port 1421
        $desktop = Start-Process -FilePath "npm.cmd" `
            -ArgumentList @("--workspace", "apps/desktop", "run", "dev", "--", "--mode", "local-paper", "--host", "127.0.0.1", "--port", "1421", "--strictPort") `
            -WorkingDirectory $root `
            -RedirectStandardOutput (Join-Path $stateDir "desktop.stdout.log") `
            -RedirectStandardError (Join-Path $stateDir "desktop.stderr.log") `
            -WindowStyle Hidden `
            -PassThru
        Set-Content -LiteralPath $desktopPidPath -Value $desktop.Id -NoNewline
    }

    $desktopReady = $false
    for ($attempt = 0; $attempt -lt 60; $attempt += 1) {
        Start-Sleep -Milliseconds 500
        try {
            $response = Invoke-WebRequest -Uri $desktopUrl -TimeoutSec 2 -UseBasicParsing
            if ($response.StatusCode -eq 200 -and $response.Content -match '<div id="root">') {
                $desktopReady = $true
                break
            }
        }
        catch {
        }
    }
    if (-not $desktopReady) {
        throw "Desktop dev server가 준비되지 않았습니다. $stateDir\desktop.stderr.log를 확인하세요."
    }

    Write-Output "PASS Desktop URL: $desktopUrl"
    Write-Output "Logs: $stateDir"
}
