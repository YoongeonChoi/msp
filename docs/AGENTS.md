# Docs Instructions

## Overview

Docs are part of the safety system. They must not claim verified provider behavior unless source-backed.

## Rules

- Reference official docs in `API_CONNECTIONS.md`.
- Unknown API details go to `API_GAPS.md` with exact verification steps.
- Do not invent Toss/KRX/OpenDART/Naver/OpenAI endpoints or rate limits.
- Keep live trading warnings explicit.
- Update runbooks when changing deploy, rollback, execution, or risk behavior.
- User-facing explanations should be Korean when written for the app user; architecture docs may keep technical identifiers in English.

## Required Docs

`ARCHITECTURE.md`, `ENGINE.md`, `RISK_POLICY.md`, `EXECUTION_POLICY.md`, `SECURITY.md`, `THREAT_MODEL.md`, `API_CONNECTIONS.md`, `API_GAPS.md`, `RUNBOOK.md`, `ROLLBACK.md`, and `COST_LIMITS.md` must stay current with code.

