---
feature: 171-revalidate
repo: salvatorebottiglieri/memex
branch: feat-171-revalidate
tracker: https://github.com/salvatorebottiglieri/memex/issues/171
---

# 171-revalidate — ri-giudicare un nodo esistente senza riscriverne la prosa

## Phase plan

| Phase | Decision | Reason |
|---|---|---|
| 1 · Design & alignment | run | le premesse sono misurate sul vault reale e sul codice |
| 2 · Product review | skip | nessuna UI: un comando CLI che aggiorna campi già esistenti |
| 3 · System architecture | skip | nessun componente nuovo né confine di sistema: un servizio che riusa `run_validations` e i check esistenti |
| 4 · Tickets | run | un ticket, #171 |
| 5 · Program design | skip | nessuna firma pubblica nuova oltre al comando; la scelta ("solo i campi, mai la prosa") è in `## Decision` |
| 6 · Implement-loop | run | — |

Approved: 2026-11-24/25, owner in chat — "ok" (ordine proposto: `revalidate` → misura su campione →
`--force`) e "prima di farlo … voglio prima integrarlo nel sistema e poi partiamo cn la 147".

## Problem Statement

Il gate di validazione gira **solo alla creazione** di un nodo. Non esiste modo di ri-giudicare una
derivazione già in trust, e questo lascia scoperte le 48 derivazioni del 2026-07-29, tutte
`auto-verified` — un audit esterno (report non presente su questa macchina) ci trovò numeri e
attribuzioni inventati. `memex derive` su un L0 già derivato risponde `already_derived`
(`services/derive.py:97-101`), quindi manca anche la via distruttiva, e #147 non può partire.

## Solution

`memex revalidate [<node-id> | --all] [--limit N]`:

1. carica il contenuto del nodo dal suo `content_path` e i parent dalla provenienza, con gli stessi
   input che usa `derive` (nessuna euristica nuova);
2. esegue i check deterministici e il DAG di validazione **sul contenuto esistente**;
3. aggiorna `trust_state`, `check_failures`, `evidence` — **la prosa non si tocca**: il file markdown
   resta byte-identico, e questo è il test che lo prova;
4. stampa un JSON per nodo e un riepilogo; **exit 1** se almeno un nodo passa da `auto-verified` a
   `draft`, 0 altrimenti (usabile in uno script di migrazione);
5. idempotente: due esecuzioni di fila danno lo stesso esito.

È il **passo 1** del percorso di #147: misura lo scope senza riscrivere niente. La ri-derivazione vera
(`derive --force`) è un ticket separato, da aprire solo dopo aver visto questi numeri.

## Decision

**Scelta**: `revalidate` tocca solo le colonne di stato (`trust_state`, `check_failures`, `evidence`) e
non il file markdown. La prosa resta quella approvata a luglio: se è sbagliata, lo dice lo stato, non
una riscrittura silenziosa.

**Alternative rifiutate**:
- *ri-derivare direttamente*: distrugge il testo esistente prima di sapere quanti nodi sono davvero
  colpiti, e costa una derivazione LLM per nodo invece di un giudizio;
- *un `--force` su `derive` come unico strumento*: identico costo, stesso effetto distruttivo, e non
  permette di misurare;
- *riconvalidare solo su richiesta esplicita del singolo id*: renderebbe la misura dei 48 un ciclo di 48
  comandi a mano.

**Conseguenze**: dopo `revalidate` un nodo può passare a `draft` e restare tale finché non lo si
ri-deriva (contenuto vecchio, stato basso è l'esito onesto: il gate non sa riscrivere). Il costo è una
chiamata al giudice per nodo, non una derivazione.

## Premises

| # | Premise | Command | Output | Verdict |
|---|---|---|---|---|
| P1 | Non esiste un comando di riconvalida | `memex --help` | nessun `revalidate` | confirmed |
| P2 | `derive` su un L0 già derivato si ferma | lettura di `services/derive.py` | `status="already_derived"` a :97-101 | confirmed |
| P3 | Le 48 derivazioni di luglio sono tutte fidate e mai ri-giudicate | `select trust_state from node where kind='summary'` | 48/48 `auto-verified`, `check_failures = []` | confirmed |
| P4 | I parent di quei nodi hanno contenuto leggibile (D7 può girare) | controllo di un campione su disco | parent `raw_source` con `content_path` esistente | confirmed |
| P5 | Il DAG di validazione è chiamabile su un nodo esistente | `run_validations(judge, con, node_id, content_path)` | firma già usata da `cli.py:951/989` | confirmed |

## Model

Model skipped — nessuna entità nuova né ciclo di vita nuovo: `trust_state` ha già le sue transizioni
(ADR-0004, ADR-0018); il comando le ri-esegue su un nodo esistente.

## User Stories

1. Come proprietario del vault, voglio sapere quali derivazioni esistenti non passerebbero il gate di
   oggi, così che la migrazione di #147 parta da numeri invece che da un report esterno assente.
2. Come agente che legge il vault, voglio che un nodo fidato lo sia per il gate attuale, non per quello
   di quattro mesi fa.
3. Come manutentore, voglio che la misura non riscriva niente: se poi serve ri-derivare, quella è una
   decisione separata e consapevole.

## Tickets

- #171 — memex revalidate: ri-giudicare un nodo esistente senza riscriverne la prosa

## Delivery

- #171 — PR: https://github.com/salvatorebottiglieri/memex/pull/172 · Model: n/a — nessuna entità nuova, transizioni già esistenti · Mutation: 171-revalidate-mutation
