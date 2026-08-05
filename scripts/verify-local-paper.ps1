[CmdletBinding()]
param(
    [switch]$RequireDesktop,
    [switch]$ExerciseCommand,
    [switch]$RequireQualification
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$workerEnvPath = Join-Path $root "apps\worker\.env"
$desktopEnvPath = Join-Path $root "apps\desktop\.env.local-paper.local"
$helperPath = Join-Path $root "scripts\local-paper.mjs"
$dbContainer = "supabase_db_kr-auto-trading-lab"
$authContainer = "supabase_auth_kr-auto-trading-lab"
$desktopUrl = "http://127.0.0.1:1421/"

function Read-DotEnv {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path $Path)) {
        throw "필수 local env 파일이 없습니다: $Path"
    }
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
        if ($values.ContainsKey($name)) {
            throw "중복된 env key가 있습니다: $name ($Path)"
        }
        $values[$name] = $line.Substring($separator + 1)
    }
    return $values
}

function Invoke-PsqlScalar {
    param([Parameter(Mandatory = $true)][string]$Sql)

    $result = docker exec $dbContainer psql -X -U supabase_admin -d postgres -Atc $Sql 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "Local PostgreSQL verification query failed."
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

if (-not (docker ps --filter "name=$dbContainer" --format "{{.Names}}")) {
    throw "Local Supabase database가 실행 중이 아닙니다."
}
if (-not (docker ps --filter "name=$authContainer" --format "{{.Names}}")) {
    throw "Local Supabase Auth가 실행 중이 아닙니다."
}

$workerEnv = Read-DotEnv -Path $workerEnvPath
$desktopEnv = Read-DotEnv -Path $desktopEnvPath
$workerUrl = [Uri]$workerEnv["SUPABASE_URL"]
$desktopUrlValue = [Uri]$desktopEnv["VITE_SUPABASE_URL"]
if (
    $workerUrl.Scheme -ne "http" -or
    $workerUrl.Host -ne "127.0.0.1" -or
    $workerUrl.Port -ne 54321 -or
    $workerUrl.AbsoluteUri -ne $desktopUrlValue.AbsoluteUri
) {
    throw "Worker와 Desktop이 동일한 loopback Supabase를 사용하지 않습니다."
}
if ($workerEnv["SUPABASE_SECRET_KEY"] -notmatch "^sb_secret_[A-Za-z0-9_-]+$") {
    throw "Worker secret 형식이 local Supabase contract와 다릅니다."
}
if ($desktopEnv["VITE_SUPABASE_PUBLISHABLE_KEY"] -notmatch "^sb_publishable_[A-Za-z0-9_-]+$") {
    throw "Desktop publishable key 형식이 local Supabase contract와 다릅니다."
}
if (
    $workerEnv["ENV"] -ne "local" -or
    $workerEnv["MOCK_PROVIDERS"] -ne "true" -or
    $workerEnv["BOT_DEFAULT_MODE"] -ne "paper" -or
    $workerEnv["LIVE_ORDER_EXECUTION_ENABLED"] -ne "false" -or
    $workerEnv["TOSS_ORDER_ENDPOINT_ENABLED"] -ne "false" -or
    $workerEnv["EXECUTION_V2_ENVIRONMENT"] -ne "paper" -or
    $workerEnv["EXECUTION_V2_WORKER_API_ENABLED"] -ne "true" -or
    $workerEnv["EXECUTION_V2_ACCOUNT_ID"] -ne "paper-primary"
) {
    throw "Worker local PAPER safety environment가 일치하지 않습니다."
}
if ($workerEnv["EXECUTION_V2_WORKER_ID"] -notmatch "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$") {
    throw "Worker identity가 canonical UUID v4가 아닙니다."
}
Write-Output "PASS ignored Worker/Desktop env files are bound to loopback PAPER only"

$repositoryMigrations = @(
    Get-ChildItem -LiteralPath (Join-Path $root "supabase\migrations") -Filter "*.sql" |
        Sort-Object Name |
        ForEach-Object { $_.BaseName.Split("_", 2)[0] }
)
$appliedMigrations = @(
    (Invoke-PsqlScalar "select coalesce(string_agg(version, '|' order by version), '') from supabase_migrations.schema_migrations;").Split("|", [StringSplitOptions]::RemoveEmptyEntries)
)
if (($repositoryMigrations -join "|") -ne ($appliedMigrations -join "|")) {
    throw "Migration version inventory가 repository와 exact match가 아닙니다."
}
Write-Output "PASS PostgreSQL 17 exact migration inventory: $($appliedMigrations.Count)/$($repositoryMigrations.Count)"

$safeSeed = Invoke-PsqlScalar "select concat(enabled::text,'|',mode::text,'|',live_order_allowed::text) from public.bot_settings where id='singleton';"
if ($safeSeed -ne "false|paper|false") {
    throw "bot_settings가 fail-closed PAPER seed 상태가 아닙니다."
}
Write-Output "PASS bot_settings remains paper with LIVE permanently disabled"
$seedObjects = Invoke-PsqlScalar "select concat((select count(*) from public.strategy_versions where version_name='weighted_factor_v1_seed' and status='paper' and strategy_type='WeightedFactorStrategyV1'), '|', (select count(*) from public.watchlist where symbol='005930' and name='삼성전자' and sector='반도체' and enabled));"
if ($seedObjects -ne "1|1") {
    throw "strategy/watchlist seed postcondition이 충족되지 않았습니다."
}
Write-Output "PASS strategy and watchlist seed postconditions"

$authEnvironment = docker inspect $authContainer --format "{{range .Config.Env}}{{println .}}{{end}}"
if (
    $authEnvironment -notcontains "GOTRUE_MFA_TOTP_ENROLL_ENABLED=true" -or
    $authEnvironment -notcontains "GOTRUE_MFA_TOTP_VERIFY_ENABLED=true"
) {
    throw "Local Supabase Auth의 TOTP enroll/verify가 활성화되지 않았습니다."
}
$identityCount = [int](Invoke-PsqlScalar "select count(distinct users.id) from auth.users users join auth.mfa_factors factors on factors.user_id=users.id and factors.factor_type='totp' and factors.status='verified' where lower(users.email) in ('operator.local@example.test','risk.local@example.test');")
$roleCount = [int](Invoke-PsqlScalar "select count(*) from private.role_assignments where revoked_at is null and ((role='operator' and user_id=(select id from auth.users where lower(email)='operator.local@example.test')) or (role='risk_approver' and user_id=(select id from auth.users where lower(email)='risk.local@example.test')));")
if ($identityCount -ne 2 -or $roleCount -ne 2) {
    throw "서로 다른 operator/risk_approver의 verified TOTP 또는 역할이 완성되지 않았습니다."
}
Invoke-NodeHelper -Command "verify-identity"

$releaseSha = $workerEnv["LOCAL_PAPER_RELEASE_SHA"]
if ($releaseSha -notmatch "^[0-9a-f]{40}$") {
    throw "Worker local release SHA가 canonical Git SHA가 아닙니다."
}
if ($RequireQualification) {
    $qualified = Invoke-PsqlScalar "select exists(select 1 from private.qualifications qualification join private.qualification_finalizations finalization on finalization.qualification_id=qualification.id where qualification.account_id='paper-primary' and qualification.environment='paper' and qualification.release_sha='$releaseSha' and qualification.status='qualified' and qualification.g1_status='pass' and qualification.g2_status='pass' and qualification.valid_from<=clock_timestamp() and qualification.valid_until>clock_timestamp());"
    if ($qualified -ne "t") {
        throw "현재 Worker release에 대한 maker/checker qualification finalization이 없습니다. Hosted release evidence 없이 이 gate를 우회하지 않습니다."
    }
    Write-Output "PASS current Worker release has finalized PAPER qualification"
}

$lease = Invoke-PsqlScalar "select exists(select 1 from private.worker_leases where account_id='paper-primary' and holder_id='$($workerEnv["EXECUTION_V2_WORKER_ID"])' and expires_at>clock_timestamp());"
if ($lease -ne "t") {
    throw "설정된 Worker identity의 active lease가 없습니다. npm run local-paper:worker를 먼저 실행하세요."
}
Write-Output "PASS configured Worker identity holds the active paper-primary lease"

if ($ExerciseCommand) {
    Invoke-NodeHelper -Command "open-account"
    Invoke-NodeHelper -Command "verify-opening"
}

$opening = Invoke-PsqlScalar "select concat_ws('|', account.state, coalesce((select count(*)::text from private.accounting_transactions transaction where transaction.id=account.opening_journal_entry_id), '0'), coalesce((select count(*)::text from private.accounting_postings posting where posting.journal_entry_id=account.opening_journal_entry_id), '0'), coalesce(balance.settled_cash_krw::bigint::text, '')) from private.trading_accounts account left join private.cash_balance_projection balance using (account_id) where account.account_id='paper-primary';"
if ($opening -ne "open|1|2|10000000") {
    throw "paper-primary opening journal postcondition이 충족되지 않았습니다."
}
Write-Output "PASS account opening, balanced journal, and cash projection postcondition"

if ($ExerciseCommand) {
    Invoke-NodeHelper -Command "exercise-command"
}

if ($RequireDesktop) {
    try {
        $response = Invoke-WebRequest -Uri $desktopUrl -TimeoutSec 5 -UseBasicParsing
    }
    catch {
        throw "Local PAPER Desktop가 127.0.0.1:1421에서 응답하지 않습니다."
    }
    if ($response.StatusCode -ne 200 -or $response.Content -notmatch '<div id="root">') {
        throw "1421 응답이 Desktop React 진입점과 일치하지 않습니다."
    }
    Write-Output "PASS Desktop is serving the local PAPER flavor at $desktopUrl"
}

Write-Output "FINAL=PASS local PAPER runtime verification"
