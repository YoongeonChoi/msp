# ADR-0002 Render Worker vs Web Service

Status: accepted

Use Render Background Worker, not a web service, because the trading engine does not need inbound HTTP traffic. Control is via Supabase tables, auth, RLS, and Realtime.

Deployment must keep `autoDeployTrigger: "off"` and disable live trading before manual deploy.

