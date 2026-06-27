# Render Deployment

Use `render.yaml` worker service:

- `type: worker`
- `runtime: python`
- `region: singapore`
- `plan: starter`
- `rootDir: apps/worker`
- `autoDeployTrigger: "off"`
- `maxShutdownDelaySeconds: 120`

Before deploy:

1. Set `bot_settings.enabled=false`.
2. Set `live_order_allowed=false`.
3. Wait for heartbeat showing paused/disabled.
4. Deploy manually.
5. Verify heartbeat.
6. Run smoke checks.
7. Re-enable paper first.
8. Consider live only after manual confirmation.

No secrets are stored in `render.yaml`; secret env vars use `sync: false`.

