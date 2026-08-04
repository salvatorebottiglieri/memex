# Handoff — 2026-08-04

Stato del progetto per la prossima sessione. Main = `cb2cd31`, branch pulito,
suite verde: **472 pytest + 177 smoke**.

## Cosa è successo in questa sessione

1. **Triage backlog** — chiuse come `wontfix` le issue del vecchio fetcher
   (#84/#83/#82/#79, duplicati di #80/#81/#78, architettura eliminata nel
   refactor resolver del 24/07), chiusa la mappa #73, `ready-for-agent` su #85.
2. **Rimosso `develop/vault/` dal repo** (36 file tracciati, incluso un
   `memex.db`), aggiunto `develop/` al `.gitignore` (`38db045`).
3. **Due implement-loop completati** (TDD + review a 2 assi + fix loop):
   - #85 → PR #101: `derive --all` illimitato di default (`--limit N` tappezza,
     `--limit 0`/negativo = illimitato).
   - #95 → PR #102: store model URL-node + extracted-node (kind invariants,
     `fetcher_type`, confidence, migrazione schema failure-atomic).
   - #96 → PR #104: `memex extract <url>` (fetcher http/pdf/wikipedia,
     `--force` per ri-estrarre in place, advisory per non-ingestable,
     `source.failed=1` su fetch error).
   - #97 → PR #105: `register` crea coppia URL-node + extracted-node.
   - #98 → PR #106: `list`/`show`/`render`/`stats` per i nuovi kind.
4. **Integrazione su main fatta da me** (le PR non erano mergiabili via UI:
   conflitto additivo store.py tra #96 e #98). Risolto tenendo entrambi i
   metodi. **Errore mio da non ripetere**: il primo commit di #96 ometteva
   `store.py` (gli helper erano nel worktree ma non nel branch) — verificare
   `git diff --stat` del branch prima di pushare.
5. **Test allineati al comportamento combinato** (`cb2cd31`): 8 test +
   5 check smoke che codificavano il pre-#98 (URL-node visibili in
   `list`/`render`).
6. **Smoke test reso ermetico** (`1a8adc9`): unsets `MEMEX_DB`/`MEMEX_VAULT`
   a inizio modulo — il check `status --vault` (derivazione db dal vault)
   falliva con le env var ambientali. **Eseguire lo smoke senza `env -u`**:
   ora è sicuro.
7. **README ripulito** (`c40e6e4`): rimossi comandi fantasma (`extract
   --inbox`, `list --pending`, `retry`, `capture`, `MEMEX_FETCHER_MODULE`),
   aggiunti `register`/`relate`/`ontology`.

## Tracker — aperte (tutte `ready-for-agent`)

| # | Ticket | Dipendenze |
|---|---|---|
| 99 | YouTube transcript extractor (cache in `vault/.cache/`, confidence=low) | #96 — **mergiata, sbloccato** |
| 100 | Retire `raw_source` (contract: derive_all/renderer/checks/ontology/CONTEXT.md/ARCHITECTURE.md) | #96/#97/#98 — **mergiate, sbloccato** |
| 103 | D5 depth checks: notes da extracted (depth 2) mai auto-verified | nessuno — **frontiera** |

**Ordine suggerito**: #103 è piccolo e sblocca il trust gate per il nuovo
modello; #99 dipende dal package `fetchers/` di #96; #100 è il wide-refactor
finale (contract) e va per ultimo.

## Stato del modello (post-#95/#96/#97/#98)

- **URL-node** (`kind='url'`): zero contenuto, tier/trust/confidence NULL,
  depth=0, mai contestabile. La `source` row vive su di lui.
- **Extracted-node** (`kind='extracted'`, `tier='extracted'`, depth=1,
  `content_path` → `vault/extracted/<id>.md` per extract, o il file utente per
  register). Nessuna source row; confidence da `EXTRACTED_CONFIDENCE`
  (`rules.py`: http=medium, youtube=low, pdf=high, wikipedia=high); edge
  provenance → URL-node.
- **Notes/synthesis**: invariati (notes=medium 1 parent, synthesis=min).
- `raw_source` ancora supportato (expand phase) ma **nessun comando lo crea
  più** — solo DB legacy. #100 lo elimina.
- **`list` esclude `kind='url'` di default** (a livello SQL in
  `store.list_nodes`); `--kind url` li mostra. Attenzione ai consumer di
  `list_nodes()` (renderer, derive_all, backfill) che ora non vedono gli URL.
- **Helper store da usare** (non SQL raw nel CLI): `find_url_parent(node_id)`
  (#98), `find_extracted_child(url_node_id)` (#96), `mark_source_failed`,
  `update_source_after_fetch`, `update_extracted_fetcher` (#96).
- **Contratto checks→trust** (pattern da `services/derive.py`): `run_checks`
  → `auto-verified` se passa, `draft` + `check_failures` altrimenti.

## Gotcha noti

- **D5** (`tier=notes ⇒ depth==1`) è obsoleto: notes da extracted (depth 1)
  → depth 2 → sempre draft. È #103. I test che pinnano `draft` come esito
  (test_render, smoke derive-pass) gireranno quando #103 atterra.
- **`derive --all` filtra `kind != 'raw_source'`** — non deriva né url né
  extracted. #100 deve puntarlo a `kind='extracted'`.
- **ADR-0013** (RoutingFetcher) è ancora nell'indice ma il suo codice era
  stato rimosso; #96 lo ha riesumato come `src/memex/fetchers/` con le
  resolution rules come classificatore. L'ADR andrebbe aggiornato (o
  sostituito) — nessun ticket lo copre ancora.
- **CONTEXT.md** glossario ancora raw_source-centrico ("Raw source (L0)") —
  coperto da #100. ARCHITECTURE.md status idem.
- **Smoke** = 177 check; documento di contratto d'ambiente in testa a
  `tests/smoke_test.py`.

## Workflow di sessione

- Flusso e skill: `~/agentic-workflow/WORKFLOW.md` (+ BASELINE.md, TARGET.md).
- Implement-loop: `skill://implement-loop` — coordinatore che delega a
  subagent TDD, review standards+spec con diff verbatim, fix loop fino a zero
  findings, docs, PR.
- **Lezione della sessione**: in caso di ticket paralleli che toccano superfici
  correlate (es. comportamento di `list`), fare la **verifica di integrazione**
  (merge locale + suite completa + smoke) PRIMA di dichiarare chiuso il loop —
  il verde per-branch non garantisce il verde combinato.
