# ADR-0006 Modular Monolith First

Status: accepted

Keep one worker process for MVP. Use internal events and `outbox_events` as extension points. Do not add Redis, Kubernetes, sharding, or microservices until the single worker has measurable pressure.

