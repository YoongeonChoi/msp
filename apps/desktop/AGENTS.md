# Desktop Instructions

## Overview

Tauri + React management cockpit. It is a control plane UI, not an execution engine.

## Where To Look

| Task | Location |
| --- | --- |
| Main UI shell | `src/App.tsx` |
| Mock UI data | `src/lib/mockData.ts` |
| Supabase client | `src/lib/supabaseClient.ts` |
| Tauri permissions | `src-tauri/capabilities/default.json` |
| CSP | `src-tauri/tauri.conf.json` |

## Rules

- User-facing text should be Korean.
- No broker secret, Supabase secret key, Toss credential, or OpenAI key in desktop.
- No direct broker order call from UI.
- Dangerous controls require confirmation.
- Live mode and live permission must be visible when present.
- Use accessible focus states and avoid color-only status.
- Do not add Tauri shell, filesystem, or network capabilities unless a documented need exists.

## Commands

```bash
npm install
npm run desktop:dev
npm run desktop:typecheck
npm run desktop:build
```

