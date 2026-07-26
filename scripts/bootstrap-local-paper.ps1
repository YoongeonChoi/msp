[CmdletBinding()]
param(
    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $env:LOCALAPPDATA) {
    throw "LOCALAPPDATA가 없어 보호된 로컬 PAPER 상태 경로를 결정할 수 없습니다."
}
$python312 = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
$workerPython = Join-Path $root "apps\worker\.venv\Scripts\python.exe"
$supabaseVersion = "2.109.1"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [string]$WorkingDirectory = $root
    )
    Push-Location $WorkingDirectory
    try {
        & $FilePath @ArgumentList
        if ($LASTEXITCODE -ne 0) {
            throw "$FilePath exited with code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}

function Get-RepositoryMigrationVersions {
    return @(
        Get-ChildItem -LiteralPath (Join-Path $root "supabase\migrations") -Filter "*.sql" |
            Sort-Object Name |
            ForEach-Object { $_.BaseName.Split("_", 2)[0] }
    )
}

function Get-AppliedMigrationVersions {
    $versions = @(
        docker exec supabase_db_kr-auto-trading-lab psql -X -U supabase_admin -d postgres -Atc `
            "select version from supabase_migrations.schema_migrations order by version;" 2>$null
    )
    if ($LASTEXITCODE -ne 0) {
        throw "Local PostgreSQL migration inventory를 읽지 못했습니다."
    }
    return @($versions | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
}

function Assert-AppliedPrefix {
    param(
        [Parameter(Mandatory = $true)][string[]]$Repository,
        [Parameter(Mandatory = $true)][string[]]$Applied
    )

    if ($Applied.Count -gt $Repository.Count) {
        throw "Applied migration inventory가 repository보다 깁니다. reset 또는 repair를 자동 실행하지 않습니다."
    }
    for ($index = 0; $index -lt $Applied.Count; $index += 1) {
        if ($Applied[$index] -ne $Repository[$index]) {
            throw "Applied migration inventory가 repository의 exact prefix가 아닙니다. reset 또는 repair를 자동 실행하지 않습니다."
        }
    }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker CLI가 없습니다. Docker Desktop을 설치한 뒤 다시 실행하세요."
}

if (-not (Test-Path -LiteralPath $python312)) {
    throw "Python 3.12가 없습니다. winget install -e --id Python.Python.3.12 --scope user 를 실행하세요."
}
Invoke-Checked -FilePath $python312 -ArgumentList @(
    (Join-Path $root ".github\scripts\migration_history_guard.py"),
    "--worktree"
)

try {
    docker info *> $null
}
catch {
    $desktop = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    if (-not (Test-Path $desktop)) {
        throw "Docker daemon이 실행 중이 아니고 Docker Desktop도 찾지 못했습니다."
    }
    Start-Process -FilePath $desktop -WindowStyle Hidden
    $ready = $false
    for ($attempt = 0; $attempt -lt 60; $attempt += 1) {
        Start-Sleep -Seconds 2
        try {
            docker info *> $null
            $ready = $true
            break
        }
        catch {
        }
    }
    if (-not $ready) {
        throw "Docker daemon이 제한 시간 안에 준비되지 않았습니다."
    }
}

$dbContainer = docker ps --filter "name=supabase_db_kr-auto-trading-lab" --format "{{.Names}}"
if (-not $dbContainer) {
    Push-Location $root
    try {
        # Supabase start prints local keys. Capture and discard its output so a
        # Worker secret never enters the terminal transcript or process logs.
        $supabaseStartOutput = & npx.cmd --yes "supabase@$supabaseVersion" start --ignore-health-check 2>&1
        $supabaseStartExitCode = $LASTEXITCODE
        $supabaseStartOutput = $null
        if ($supabaseStartExitCode -ne 0 -and -not (docker ps --filter "name=supabase_db_kr-auto-trading-lab" --format "{{.Names}}")) {
            throw "Local Supabase containers did not start."
        }
    }
    finally {
        Pop-Location
    }
}

$repositoryMigrations = Get-RepositoryMigrationVersions
$appliedBefore = Get-AppliedMigrationVersions
Assert-AppliedPrefix -Repository $repositoryMigrations -Applied $appliedBefore
if ($appliedBefore.Count -lt $repositoryMigrations.Count) {
    docker exec supabase_db_kr-auto-trading-lab psql -X -U postgres -d postgres -v ON_ERROR_STOP=1 -c "revoke create on schema extensions from dashboard_user;" | Out-Null
    $preflight = Get-Content -Raw (Join-Path $root "supabase\preflight\pgcrypto_replay_preflight.sql")
    $preflight | docker exec -i supabase_db_kr-auto-trading-lab psql -X -U supabase_admin -d postgres -v ON_ERROR_STOP=1
    if ($LASTEXITCODE -ne 0) {
        throw "PostgreSQL 17 pgcrypto preflight failed."
    }
    Invoke-Checked -FilePath "npx.cmd" -ArgumentList @("--yes", "supabase@$supabaseVersion", "migration", "up", "--local", "--include-all")
}

$appliedAfter = Get-AppliedMigrationVersions
if (($repositoryMigrations -join "|") -ne ($appliedAfter -join "|")) {
    throw "Migration inventory mismatch after additive migration up."
}

# seed.sql is idempotent and fail-closed. Reapply it even on an already-current
# local database so bootstrap actually establishes every documented seed row.
$seed = Get-Content -Raw (Join-Path $root "supabase\seed.sql")
$seed | docker exec -i supabase_db_kr-auto-trading-lab psql -X -U supabase_admin -d postgres -v ON_ERROR_STOP=1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "supabase/seed.sql failed."
}
$seedPostcondition = docker exec supabase_db_kr-auto-trading-lab psql -X -U supabase_admin -d postgres -Atc "select concat((select concat(enabled::text,'|',mode::text,'|',live_order_allowed::text) from public.bot_settings where id='singleton'), '|', (select count(*) from public.strategy_versions where version_name='weighted_factor_v1_seed' and status='paper' and strategy_type='WeightedFactorStrategyV1'), '|', (select count(*) from public.watchlist where symbol='005930' and name='삼성전자' and sector='반도체' and enabled));" 2>$null
if ($LASTEXITCODE -ne 0 -or ([string]$seedPostcondition).Trim() -ne "false|paper|false|1|1") {
    throw "seed.sql postcondition verification failed."
}

if (-not (Test-Path $workerPython)) {
    Invoke-Checked -FilePath $python312 -ArgumentList @("-m", "venv", (Join-Path $root "apps\worker\.venv"))
}
if (-not $SkipDependencyInstall) {
    Invoke-Checked -FilePath $workerPython -ArgumentList @("-m", "pip", "install", "-e", "apps/worker[dev]")
}
Invoke-Checked -FilePath $workerPython -ArgumentList @("-m", "app.tools.write_release_metadata") -WorkingDirectory (Join-Path $root "apps\worker")
Invoke-Checked -FilePath "node.exe" -ArgumentList @((Join-Path $root "scripts\local-paper.mjs"), "provision")
Invoke-Checked -FilePath "node.exe" -ArgumentList @((Join-Path $root "scripts\local-paper.mjs"), "open-account")

Write-Output "PASS PostgreSQL 17 exact migration inventory: $($appliedAfter.Count)/$($repositoryMigrations.Count)"
Write-Output "PASS fail-closed bot settings, strategy, and watchlist seed postconditions"
Write-Output "PASS Python 3.12 Worker environment is ready"
Write-Output "PASS account opening is approved or its journal postcondition already holds"
Write-Output "NEXT (native Desktop): npm run local-paper:desktop"
Write-Output "NEXT (browser Desktop): npm run local-paper:start"
