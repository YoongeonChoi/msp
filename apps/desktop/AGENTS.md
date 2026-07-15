# Desktop Instructions

## Overview

Tauri + React management cockpit. It is a control plane UI, not an execution engine.

## Where To Look

| Task | Location |
| --- | --- |
| Main UI shell | `src/App.tsx` |
| Operations cockpit | `src/pages/OperationsPage.tsx`, `src/components/operations/` |
| Strict V1 contracts | `../../packages/shared/src/operations.ts`, `src/lib/operationsContracts.ts` |
| Operations RPC boundary | `src/lib/operationsData.ts` |
| Safe Realtime signal | `src/lib/controlPlaneRealtime.tsx` |
| Supabase client | `src/lib/supabaseClient.ts` |
| Tauri permissions | `src-tauri/capabilities/default.json` |
| CSP | `src-tauri/tauri.conf.json` |

## Rules

- User-facing text should be Korean.
- No broker secret, Supabase secret key, Toss credential, or OpenAI key in desktop.
- No direct broker order call from UI.
- Do not perform direct CRUD against `public`, `private`, or source-of-truth tables.
- Mutations use strict `api` RPCs and must stop on unknown/missing contract fields.
- Never show a command as complete before Worker ACK and the runtime postcondition.
- Stale, disconnected, expired-session, or offline state blocks mutations.
- Offline emergency stop is not queued or replayed.
- Subscribe only to `api.control_plane_signal`; raw execution/audit payloads are not Realtime data.
- Dangerous controls require confirmation.
- PAPER/CONTRACT TEST and permanent LIVE prohibition must remain visible.
- Use accessible focus states and avoid color-only status.
- Do not add Tauri shell, filesystem, or network capabilities unless a documented need exists.

## Commands

```bash
npm install
npm run desktop:dev
npm run desktop:typecheck
npm run desktop:test
npm run desktop:e2e
npm run desktop:build
```

