# Architecture

The system is one modular monolith worker plus a desktop control cockpit.

Bounded contexts:

- Trading
- MarketData
- Fundamentals
- NewsIntel
- Portfolio
- Risk
- Execution
- StrategyResearch
- Operations
- IdentityAccess

Dependency rule:

- `domain` imports no provider, HTTP, Supabase, or OpenAI code.
- `application` depends on domain and ports.
- `adapters` implement provider/persistence ports.
- `infrastructure` owns cross-cutting technical mechanisms.
- Desktop has no business/risk/execution authority.

Control plane is Supabase. Execution plane is the worker.

