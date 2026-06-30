# Capture via a source-agnostic inbox; Telegram going forward, WhatsApp dropped

An **inbox abstraction** (`url + timestamp + optional note`) decouples *capture* from *ingest*, so the capture source is swappable without touching the core. Ongoing capture moves to **Telegram Saved Messages**; existing WhatsApp links are brought in by a one-time export backfill.

## Considered Options

- **WhatsApp Web automation** (whatsapp-web.js / Baileys) — rejected: violates ToS and risks a ban on my personal number.
- **WhatsApp manual export forever** — rejected as the ongoing channel: permanently manual.
- **Telegram Saved Messages** (chosen) — same "forward a link to myself" habit, but a real API.

## Consequences

The precious habit is "forward a link to myself," not "WhatsApp" — Telegram replicates the gesture and adds automation. The inbox abstraction means a future source (email, browser share-sheet) is a new adapter, not a core change.
