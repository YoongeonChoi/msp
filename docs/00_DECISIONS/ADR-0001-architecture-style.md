# ADR-0001 Architecture Style

Status: accepted

Use a modular monolith deployed as one Render Background Worker. Inside the worker, use hexagonal architecture and DDD-lite bounded contexts.

Reason:

- Render Starter has tight CPU/RAM limits.
- One trading engine is simpler and cheaper than many services.
- Ports/adapters isolate broker, market, fundamentals, news, AI, and persistence APIs.
- Boundaries allow later extraction into services without changing the domain language.

