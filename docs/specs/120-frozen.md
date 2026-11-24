---
feature: 120-frozen
repo: salvatorebottiglieri/memex
branch: factory/120-cli-check-integrit-locatori-e-congelamen
tracker: https://github.com/salvatorebottiglieri/memex/issues/120
---

# 120-frozen — integrità dei locatori e nodi frozen

Retrofit: la feature è stata progettata e implementata fuori dall'harness (software factory,
PR #158, un commit) e questa spec ne è il record phases 0–6, scritta prima della review e del
merge. Il design è `docs/design/120-frozen.md` (fase 5, 813 righe, con `contracts[]`, `## Decision`
e le quattro uscite).

## Phase plan

| Phase | Decision | Reason |
|---|---|---|
| 1 · Design & alignment | run | il problema e i criteri d'accettazione sono quelli di #120, verificati contro il codice invece che assunti |
| 2 · Product review | skip | solo CLI, nessuna UI |
| 3 · System architecture | skip | nessun componente nuovo né confine di sistema: un modulo puro (`integrity.py`), due colonne nullable su `node`, un predicato condiviso; i contratti e gli exit sono nel design di fase 5 |
| 4 · Tickets | run | un solo ticket, #120, consegnato in un commit |
| 5 · Program design | run | `docs/design/120-frozen.md` esiste ed è il contratto della fase 6 |
| 6 · Implement-loop | run | — |

Approved: 2026-11-24, owner in chat — "A"

## Problem Statement

Il vault è un sistema a due store (ADR-0008): il grafo vive in SQLite, il contenuto nei file
markdown. I locatori tra i due lati non hanno un check completo: D3 copre i wikilink
`derived_from` dangling, ma non verifica che ogni `content_path` punti a un file esistente, che il
target di un edge sia il nodo giusto, e che una sorgente morta (404, dominio sparito, fetch
fallito) lasci un segnale. Il risultato osservato prima della feature: nodi con `content_path`
rotto e sorgenti morte restano indistinguibili dai nodi vivi, e la sola risposta disponibile era
cancellare — che è distruttivo e non auditabile.

## Solution

`memex check` (nuovo `src/memex/integrity.py`, puro: tre liste in ingresso, nessuno Store, nessun
SQL) riporta tre classi di finding senza scrivere — file mancante, `derived_from` verso nodo
inesistente, sorgente morta — con exit 0 pulito / 1 con finding. `node.frozen_at` /
`node.frozen_reason` + `memex freeze` / `memex thaw` marcano un nodo come storico: resta nel grafo,
esce da ogni superficie attiva (`Store.ACTIVE_PREDICATE`, `list_nodes(include_frozen=False)` di
default, `search`, `sync`). `frozen` è **ortogonale** a `trust_state`: un flag come
`is_contested`, non un quinto valore ordinale (ADR-0019).

## User Stories

1. Come proprietario del vault, voglio che `memex check` mi elenchi i locatori rotti con un exit
   code, così che un vault degradato sia rilevabile e non solo ripulibile a mano.
2. Come proprietario del vault, voglio congelare un nodo invece di cancellarlo, così che un
   contenuto scomparso resti nel grafo come riferimento storico e fuori dai percorsi attivi.
3. Come agente che interroga il grafo, voglio che i nodi congelati non compaiano di default in
   `list` e `search`, così che il mio stop condition non si poggi su un nodo storico.
4. Come manutentore, voglio che `sync` riporti i nodi congelati, così che il riepilogo non
   nasconda lo stato del vault.

## Premises

| # | Premise | Command | Output | Verdict |
|---|---|---|---|---|
| P1 | La PR, riportata su `main`, è verde | `uv run --frozen pytest -q` | `881 passed in 208.42s` | confirmed |
| P2 | Lint e tipi sono puliti | `uv run --frozen ruff check src tests && uv run --frozen mypy src` | `All checks passed!` · `Success: no issues found in 35 source files` | confirmed |
| P3 | La firma dichiarata dal design è quella del codice | `sed -n '40,45p' src/memex/integrity.py` | `def check_integrity(locations: Iterable[tuple[str, str \| None]], provenance: Iterable[tuple[str, str]], sources: Iterable[tuple[str, bool]]) -> list[Finding]:` | confirmed |
| P4 | Il fallimento di `guards/check_design.py --gate` è un limite del guard, non una divergenza del design | probe su `check_design.tokens()` sulla firma dichiarata | `['locations', '# (node_id, content_path)\n    provenance', …]` → `incompatible — missing provenance, sources` | confirmed |
| P5 | Il comando si chiama `check`, non `integrity` come dice il PR body | `grep -n "def check" src/memex/cli.py` | `202:def check(db_path: Path, vault_path: Path) -> None:` | confirmed |
| P6 | `frozen` è una colonna nuova su `node`, non uno stato di `trust_state` | `grep -n "frozen_at" src/memex/store.py` | `53: frozen_at TEXT` · `139: ACTIVE_PREDICATE = "n.frozen_at IS NULL"` · `271: ALTER TABLE node ADD COLUMN frozen_at TEXT` | confirmed |

## Model

Model skipped — la feature non introduce un'entità con ciclo di vita: `frozen` è un flag booleano
(`frozen_at` / `frozen_reason`) sul nodo esistente, ortogonale a `trust_state` (ADR-0019). Nessuno
stato nuovo, nessuna transizione nuova; l'allineamento semantico è in `docs/ONTOLOGY.md` e in
`src/memex/rules.py` (A1/A2).

## Tickets

- #120 — CLI: check integrità locatori e congelamento nodi (frozen)

## Delivery

- #120 — PR: https://github.com/salvatorebottiglieri/memex/pull/158 · Model: n/a — `frozen` è un flag booleano su `node`, nessun ciclo di vita (ADR-0019) · Mutation: 120-frozen-mutation-r2
