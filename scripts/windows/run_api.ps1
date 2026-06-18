$ErrorActionPreference = "Stop"
.\.venv\Scripts\python -m uvicorn msp.api.main:app --reload --host 127.0.0.1 --port 8000
