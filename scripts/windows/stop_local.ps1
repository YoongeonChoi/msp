$ErrorActionPreference = "Stop"

$patterns = @(
    "uvicorn msp.api.main:app",
    "msp.workers.data",
    "msp.workers.research",
    "msp.workers.portfolio",
    "msp.workers.execution",
    "msp.workers.reconcile"
)

$python = (Join-Path (Get-Location) ".venv\Scripts\python.exe")

$matched = Get-CimInstance Win32_Process |
    Where-Object {
        $cmd = $_.CommandLine
        if (-not $cmd) { return $false }
        if ($_.ExecutablePath -ne $python) { return $false }
        foreach ($pattern in $patterns) {
            if ($cmd -like "*$pattern*") { return $true }
        }
        return $false
    }

foreach ($proc in $matched) {
    Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
}

[pscustomobject]@{
    stopped = @($matched | ForEach-Object { $_.ProcessId })
} | ConvertTo-Json
