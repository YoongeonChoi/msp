# ADR-0005 AI Never Executes Trades

Status: accepted

OpenAI is a research assistant only. It may classify news, summarize disclosures, produce reports, and propose strategy candidates.

OpenAI output cannot call `ExecutionService`, broker ports, or database writes that create orders.

