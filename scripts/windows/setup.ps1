$ErrorActionPreference = "Stop"

if (-not (Test-Path ".venv")) {
    py -m venv .venv
}

.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e .
.\.venv\Scripts\python -m msp.cli init-db
.\.venv\Scripts\python -m msp.cli seed-cash --amount 10000000 --currency KRW

Write-Host "MSP local environment is ready."
