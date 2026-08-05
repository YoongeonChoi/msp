[CmdletBinding()]
param(
    [switch]$StopSupabase
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$stateDir = Join-Path $env:LOCALAPPDATA "kr-auto-trading-lab"
$workerPython = Join-Path $root "apps\worker\.venv\Scripts\python.exe"
$workerPidPath = Join-Path $stateDir "worker.pid"
$desktopPidPath = Join-Path $stateDir "desktop.pid"
$supabaseVersion = "2.109.1"

function Get-DescendantProcessIds {
    param([Parameter(Mandatory = $true)][int]$ParentId)

    $result = [System.Collections.Generic.List[int]]::new()
    foreach ($child in Get-CimInstance Win32_Process -Filter "ParentProcessId = $ParentId" -ErrorAction SilentlyContinue) {
        foreach ($descendant in Get-DescendantProcessIds -ParentId $child.ProcessId) {
            $result.Add($descendant)
        }
        $result.Add([int]$child.ProcessId)
    }
    return $result
}

function Stop-RecordedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][ValidateSet("worker", "desktop")][string]$Kind
    )

    if (-not (Test-Path $Path)) {
        return $false
    }
    $rawPid = (Get-Content -Raw -LiteralPath $Path).Trim()
    if ($rawPid -notmatch "^[0-9]+$") {
        throw "손상된 managed PID 파일입니다: $Path"
    }
    $recordedPid = [int]$rawPid
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $recordedPid" -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        Remove-Item -LiteralPath $Path
        return $false
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
        throw "PID 파일이 관리 대상이 아닌 프로세스를 가리킵니다. 프로세스를 종료하지 않았습니다: $Path"
    }

    foreach ($childId in Get-DescendantProcessIds -ParentId $recordedPid) {
        Stop-Process -Id $childId -Force -ErrorAction SilentlyContinue
    }
    Stop-Process -Id $recordedPid -Force -ErrorAction Stop
    Remove-Item -LiteralPath $Path
    return $true
}

$desktopStopped = Stop-RecordedProcess -Path $desktopPidPath -Kind "desktop"
$workerStopped = Stop-RecordedProcess -Path $workerPidPath -Kind "worker"
if ($desktopStopped) {
    Write-Output "PASS stopped managed desktop process"
}
if ($workerStopped) {
    Write-Output "PASS stopped managed worker process"
}

if ($workerStopped -and (docker ps --filter "name=supabase_db_kr-auto-trading-lab" --format "{{.Names}}")) {
    $leaseExpired = $false
    for ($attempt = 0; $attempt -lt 40; $attempt += 1) {
        $lease = docker exec supabase_db_kr-auto-trading-lab psql -X -U supabase_admin -d postgres -Atc "select exists(select 1 from private.worker_leases where account_id='paper-primary' and expires_at>clock_timestamp());" 2>$null
        if ($lease -eq "f") {
            $leaseExpired = $true
            break
        }
        Start-Sleep -Seconds 1
    }
    if (-not $leaseExpired) {
        throw "Worker 프로세스는 중지됐지만 lease 만료를 확인하지 못했습니다."
    }
    Write-Output "PASS paper-primary worker lease is no longer active"
}

if ($StopSupabase) {
    Push-Location $root
    try {
        & npx.cmd --yes "supabase@$supabaseVersion" stop
        if ($LASTEXITCODE -ne 0) {
            throw "Supabase stop exited with code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
    Write-Output "PASS Local Supabase stopped without resetting its database"
}
else {
    Write-Output "Local Supabase remains running. Use -StopSupabase to stop containers without resetting data."
}
Write-Output "Runtime logs remain at $stateDir"
