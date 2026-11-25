---
feature: 175-derive-force
repo: salvatorebottiglieri/memex
branch: feat-175-force
tracker: https://github.com/salvatorebottiglieri/memex/issues/175
---

# 175-derive-force — rigenerare una derivazione esistente in place

## Phase plan

| Phase | Decision | Reason |
|---|---|---|
| 1 · Design & alignment | run | premesse misurate sul vault (36/48 bocciati) e sul codice (`already_derived`) |
| 2 · Product review | skip | nessuna UI: un flag su un comando esistente |
| 3 · System architecture | skip | nessun componente nuovo: `DeriverService` ha già il percorso di scrittura, manca solo l'ingresso |
| 4 · Tickets | run | un ticket, #175 |
| 5 · Program design | skip | nessuna firma pubblica nuova oltre al flag; le scelte (stesso nodo, scrittura atomica, nessun backup) sono in `## Decision` |
| 6 · Implement-loop | run | — |

Approved: 2026-11-25, owner in chat — "lascerei lo stesso giudice perchè vorrei che il grosso lo facciano i controlli deterministici. partiamo così" — e, sui 36 markdown da sovrascrivere: "possiamo anche perderli non c'è nulla di valore lì dentro" 

## Problem Statement

`derive` su un L0 già derivato risponde `already_derived` e si ferma (`services/derive.py:97-101`):
non esiste la via per rifare una derivazione. Misura: **36 delle 48** derivazioni del 2026-07-29 non
passano il gate attuale (`revalidate`, #171) con **125 difetti fatali**, 100 dei quali numeri o
attribuzioni assenti dal sorgente; più 2 derivazioni di ieri fatte prima della regola di wording,
bocciate per costruzione. #147 (migrazione) non è eseguibile senza questo comando: `revalidate` misura,
non corregge.

## Solution

`memex derive --force <l0-id>` / `--all --force [--limit N]`:

1. **stesso nodo, stesso `content_path`**: le edge di provenienza e le sintesi che lo citano restano
   valide (un nodo nuovo romperebbe i riferimenti);
2. rigenera la prosa e ri-esegue check deterministici + DAG di validazione, come una derivazione nuova;
3. scrive il markdown **atomicamente** (temp + rename): un crash non lascia un file a metà;
4. `--force` è obbligatorio per sovrascrivere; senza flag il comportamento resta `already_derived`.

## Decision

**Scelte, con le alternative rifiutate:**

- **Stesso nodo** invece di un nodo nuovo: un id nuovo invaliderebbe `derived_from` e ogni sintesi che
  punta a quella derivazione, cioè la migrazione romperebbe il grafo esattamente dove dovrebbe
  migliorarlo.
- **Nessun backup automatico**: il vault non è un repository e ogni derivazione avrebbe un `.bak`
  permanente da gestire; la protezione è il flag esplicito. Scartato anche "archiviare in `.cache/`":
  sarebbe un secondo posto dove guardare, senza una politica di scadenza.
- **Giudice invariato** (`MEMEX_JUDGE` non impostato): l'owner ha deciso che la verifica la portano i
  **controlli deterministici**, non un giudice più costoso. Questa feature non tocca il giudice né il
  resolver.
- **Scrittura atomica**: temp + `os.replace` nel vault, perché una riscrittura interrotta non deve
  lasciare una derivazione troncata — l'unico difetto che il flag esplicito non copre.

**Conseguenze**: dopo `--force` la prosa vecchia è irrecuperabile (accettato dall'owner: "possiamo
anche perderli"). Il costo è una derivazione LLM per nodo, il gate è quello di sempre.

## Premises

| # | Premise | Command | Output | Verdict |
|---|---|---|---|---|
| P1 | `derive` si rifiuta su un nodo già derivato | lettura di `services/derive.py` | `status="already_derived"` a :97-101 | confirmed |
| P2 | Le derivazioni bocciate esistono e sono numerose | `revalidate` sui 48 nodi di luglio | 36/48 `draft`, 125 fatali | confirmed |
| P3 | Il percorso di scrittura esiste già ed è riusabile | lettura di `DeriverService._do_derive` | scrive il markdown e chiama il gate | confirmed |
| P4 | Le sintesi citano le derivazioni per id (quindi un id nuovo romperebbe) | lettura di `derived_from` + `synthesize` | edge `from=synthesis, to=derivation` | confirmed |

## Model

Model skipped — nessuna entità nuova né ciclo di vita nuovo: `trust_state` ha già le sue transizioni;
il comando le ri-esegue su un nodo esistente.

## User Stories

1. Come proprietario del vault, voglio rifare una derivazione sbagliata senza perdere il posto che
   occupa nel grafo, così che la migrazione di #147 sia possibile.
2. Come agente che legge il vault, voglio che una derivazione fiduciaria sia stata scritta con la
   pipeline di oggi, non con quella di quattro mesi fa.
3. Come manutentore, voglio che una riscrittura interrotta non lasci un file a metà, perché il vault non
   ha una cronologia da cui recuperarlo.

## Tickets

- #175 — memex derive --force: rigenerare una derivazione esistente in place

## Delivery

- #175 — PR: https://github.com/salvatorebottiglieri/memex/pull/176 · Model: n/a — nessuna entità nuova, transizioni già esistenti · Mutation: 175-derive-force-mutation
