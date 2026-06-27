# Data Pipeline

Toss: account, position, quote, candle, order contracts are represented as ports and placeholders until official endpoint details are verified.

KRX: market calendar/listing/statistics are adapter placeholders and mock data in local mode.

OpenDART: financial statement ingestion requires corp code and account-name mapping verification. Canonical fields are defined in `domain/fundamentals/value_objects.py`.

Naver: news search stores title, source, published time, compact summary/classification, and hashes for deduplication. Full article scraping is out of MVP scope.

OpenAI: structured outputs classify news/disclosures and propose monthly strategy candidates. Inputs must be sanitized and data-minimized.

Feature storage uses typed columns for queryable fields and JSONB only for snapshots.

