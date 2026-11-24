---
feature: 164-d7-gate
repo: salvatorebottiglieri/memex
branch: feat-164-d7-gate
tracker: https://github.com/salvatorebottiglieri/memex/issues/164
---

# 164-d7-gate — il grounding gate guarda la claim, non l'hint

## Phase plan

| Phase | Decision | Reason |
|---|---|---|
| 1 · Design & alignment | run | il problema è misurato su una derivazione reale, non ipotizzato |
| 2 · Product review | skip | solo comportamento interno del gate, nessuna UI |
| 3 · System architecture | skip | nessun componente nuovo né confine di sistema: un'espressione dentro `_d7_resolve_grounding` |
| 4 · Tickets | run | un ticket, #164 |
| 5 · Program design | skip | nessuna firma nuova né modulo nuovo: la scelta di progetto è registrata in `## Decision` qui sotto, con le misure che l'hanno decisa |
| 6 · Implement-loop | run | — |

Approved: 2026-11-24, owner in chat — "(a)" (opzione (a): decidere l'opzione di #164 e implementarla, poi derivare i 44)

## Problem Statement

La validation family (ADR-0018) lascia passare zero derivazioni su contenuto reale. Su una
derivazione vera del vault (`everything-i-know-about-good-api-design-sean-goedecke`,
`/mnt/c/Users/s.bottiglieri/Documents/Obsidian Vault`), 40 claim `SUPPORTED` su 40 finiscono con
`D7: SUPPORTED claim not grounded in the cited source content` e il nodo resta `draft`: il
trust-state gate (ADR-0004) non si apre mai, quindi il consumer primario (l'agente, ADR-0001) non ha
niente da leggere.

Causa: `src/memex/validators/validate.py::_d7_resolve_grounding` risolve lo span *dall'hint*
(`candidate = hint_text.strip() or claim` → `resolve_evidence(candidate, content)`) e poi applica
`grounding_gate` alla **claim**. Una claim lunga non può stare dentro lo span di una frase: coverage
0.30 su una coppia reale, contro la soglia 0.60.

## Solution

`candidate` non decide più cosa viene gated. Ordine:

1. se c'è un `evidence_hint` e lo span risolto **grounda la claim**, usa quello span (il più stretto:
   provenienza migliore);
2. altrimenti risolvi lo span dalla **claim** e applica il gate alla claim;
3. altrimenti fatal, come oggi.

Il gate resta fail-closed sulla claim, quindi la proprietà anti-invenzione di #145 resta: una claim
con numeri o dettagli assenti dal sorgente non passa.

## Decision

**Scelta**: `grounding_gate(claim, span)` sempre; `evidence_hint` è un *locator preferito*, non il
testo da verificare (opzione B della issue, con l'hint in prima battuta).

**Alternative rifiutate**, con la misura che le rifiuta (vault reale, 40 claim della derivazione
sopra; script di misura in `/tmp`, riproducibile con `resolve_evidence` + `grounding_gate`):

| Policy | Claim reali che passano | Una frode passa? |
|---|---|---|
| A — gate sull'hint (opzione 1 della issue) | **40/40** | **sì**: claim inventata + hint vero ("WE DO NOT BREAK USERSPACE") → `gate(hint)` = True, e il gate non guarda mai la claim |
| B — gate sulla claim (scelta) | **38/40** (coverage mediana 0.815, max 0.97) | **no**: la stessa frode dà coverage 0.38 → False |
| C — gate sulla claim con span dall'hint (stato attuale) | **0/40** | no (ma il gate non si apre mai) |

Una soglia a token invece che percentuale (opzione 3) è stata rifiutata perché non discrimina: una
claim fabbricata che condivide vocabolo generico col sorgente supererebbe un conteggio assoluto.

**Conseguenze**: un `evidence_hint` che non grounda la claim è solo un locator rifiutato, e non
lascia segnale (nessun `_warn`): la trasparenza su un giudice che inventa locatori si perde, ed è
registrata come follow-up in #166 insieme al messaggio fatale e allo span del fallback non
asseriti. 2 claim su 40 restano `draft` (riassunti multi-paragrafo, coverage < 0.6): è il
comportamento fail-closed voluto, non un difetto. Le derivazioni ri-esistenti non cambiano stato da
sole: serve `memex derive --force` (o il re-ingest) per ri-giudicarle.

## User Stories

1. Come agente che legge il vault, voglio che una derivazione ben fondata arrivi `auto-verified`, così
   che il mio stop condition (ADR-0004) possa fermarsi lì invece di scendere a ogni L0.
2. Come proprietario del vault, voglio che una claim che inventa numeri o dettagli resti `draft`, così
   che il grafo non prenda per buona una fabbricazione.
3. Come manutentore, voglio un test che usi una claim lunga *reale* e un test con una frode, così che
   la scelta del testo da gated non possa regredire in silenzio.

## Premises

| # | Premise | Command | Output | Verdict |
|---|---|---|---|---|
| P1 | Il difetto è nel gate, non nel giudice | `resolve_evidence(hint, src)` sugli hint reali | `WE DO NOT BREAK USERSPACE → conf 1.0`; `Once you publish an API… → conf 1.0` | confirmed |
| P2 | Lo stato attuale boccia tutte le claim reali | gate su 40 claim della derivazione `08adac5f` | `C hint-span & gate(claim) 0/40` | confirmed |
| P3 | Il gate sulla claim discrimina una frode | claim inventata + hint vero | A: `True` (frode passa); B: `coverage 0.38 → False` | confirmed |
| P4 | Il gate sulla claim non boccia il contenuto reale | 40 claim reali, `candidate = claim` | `38/40`, coverage mediana 0.815 | confirmed |
| P5 | I test attuali non coprono il caso | `grep -c "evidence_hint" tests/` | la fixture usa claim corte che stanno nello span dell'hint | confirmed |

## Model

Model skipped — nessuna entità con ciclo di vita: la modifica sceglie il testo su cui gira un gate
esistente, in una funzione pura; nessuno stato nuovo, nessuna transizione nuova.

## Tickets

- #164 — D7: il grounding gate confronta la claim con lo span risolto dall'`evidence_hint`

## Delivery

- #164 — PR: https://github.com/salvatorebottiglieri/memex/pull/165 · Model: n/a — nessuna entità con ciclo di vita, modifica a un gate esistente · Mutation: 164-d7-gate-mutation-r4
