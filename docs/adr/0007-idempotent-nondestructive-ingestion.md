# Idempotent, non-destructive ingestion via canonical key + per-source cursor

Two overlapping mechanisms. **Canonical key** (normalized URL with tracking params stripped and shorteners resolved, or a platform id like `youtube://<id>`) recorded in a **ledger** = correctness: never double-ingest, and the pending set is always derivable. **Per-source cursor** (Telegram last message id; WhatsApp export file marked done) = efficiency: only scan what's new.

Ingestion is **non-destructive**: Telegram is never mutated (the bot is read-only in v1), ingestion state lives entirely in our external ledger.

## Consequences

Re-exports and the same link appearing across sources collapse to a single node. "Not yet ingested" = items past the cursor / canonical keys with no L0 node. Because nothing is deleted from Telegram, the raw stays archived there too. An optional ✅-reaction confirmation is deferred (needs write scope).
