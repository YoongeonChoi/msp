# GitHub Instructions

## Overview

CI and repository automation protect safety-critical trading paths.

## Rules

- Keep workflow default permissions minimal.
- Do not add auto-deploy to live trading.
- Do not weaken migration/RLS checks to pass CI.
- Do not remove CodeQL, Dependabot, dependency review, or secret scanning without security rationale.
- Protected paths require PR checklist evidence.

## Protected Paths

- `apps/worker/app/application/services/risk_service.py`
- `apps/worker/app/application/services/execution_service.py`
- `apps/worker/app/adapters/broker/`
- `supabase/migrations/`
- `render.yaml`
- `.github/workflows/`

