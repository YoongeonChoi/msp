$ErrorActionPreference = "Stop"

$port = 8000
while (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
    $port++
}

$python = Join-Path (Get-Location) ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Virtual environment not found. Run .\scripts\windows\setup.ps1 first."
}

$api = Start-Process -FilePath $python `
    -ArgumentList @("-m", "uvicorn", "msp.api.main:app", "--host", "127.0.0.1", "--port", "$port") `
    -WorkingDirectory (Get-Location) `
    -WindowStyle Hidden `
    -PassThru

$execution = Start-Process -FilePath $python `
    -ArgumentList @("-m", "msp.workers.execution") `
    -WorkingDirectory (Get-Location) `
    -WindowStyle Hidden `
    -PassThru

$reconcile = Start-Process -FilePath $python `
    -ArgumentList @("-m", "msp.workers.reconcile") `
    -WorkingDirectory (Get-Location) `
    -WindowStyle Hidden `
    -PassThru

Start-Sleep -Seconds 3
$ready = Invoke-RestMethod -Uri "http://127.0.0.1:$port/health/ready" -Method Get

[pscustomobject]@{
    url = "http://127.0.0.1:$port"
    api_pid = $api.Id
    execution_worker_pid = $execution.Id
    reconcile_worker_pid = $reconcile.Id
    mode = $ready.mode
    state_version = $ready.state_version
} | ConvertTo-Json
