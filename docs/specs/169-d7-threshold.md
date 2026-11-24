---
feature: 169-d7-threshold
repo: salvatorebottiglieri/memex
branch: feat-169-d7-threshold
tracker: https://github.com/salvatorebottiglieri/memex/issues/169
---

# 169-d7-threshold — soglia con numeri hard al posto di tutto-o-niente (ponte)

## Phase plan

| Phase | Decision | Reason |
|---|---|---|
| 1 · Design & alignment | run | le premesse sono misurate su 6 derivazioni reali del vault |
| 2 · Product review | skip | nessuna UI: cambia l'aggregazione di un gate |
| 3 · System architecture | skip | nessun componente nuovo: si sposta una decisione dentro `_d7_resolve_grounding` |
| 4 · Tickets | run | un ticket, #169 |
| 5 · Program design | skip | nessuna firma pubblica nuova; la scelta di soglia è in `## Decision` con le misure |
| 6 · Implement-loop | run | — |

Approved: 2026-11-24, owner in chat — "intendevo che accetto il tuo consiglio" (opzione 1 come ponte,
opzione 2 come destinazione)

## Problem Statement

Il gate D7 è per-claim e hard: **una** claim non fondata blocca l'intera derivazione. Su prosa reale
`auto-verified` è quindi irraggiungibile. Misura (6 derivazioni nuove, vault reale, dopo #164 e #167):
**0/6 `auto-verified`**, **20** claim non fondate in totale — **18 di prosa** e **2 con numeri assenti
dal sorgente** (`imo25`, `630`). Un gate a token non distingue una parafrasi fedele da una inventata:
chiedere il 100% non misura la qualità, misura la fortuna lessicale.

## Solution

Soglia al posto di tutto-o-niente, con i numeri che restano hard (la classe di #145):

1. una claim `SUPPORTED` che non si fonda resta *ungrounded* (D7, invariato);
2. la si classifica **numeric** se contiene un token con una cifra che non compare nel contenuto di
   nessun parent, altrimenti **prose**;
3. il nodo resta `draft` se esiste almeno una claim numeric, **oppure** se le claim prose non fondate
   superano `max(1, ceil(TOLERATED_SHARE × supported_claims))` con `TOLERATED_SHARE = 0.10`;
4. altrimenti il nodo passa, e le claim prose non fondate restano **annotate** in `check_failures`
   con il tag non-fatale già esistente (`quality`), quindi visibili e non bloccanti.

L'aggregazione non diventa "nessun failure fatale" (che demoterebbe anche il gate di **qualità** di
V2, fuori scope): blocca **tutto tranne ciò che la soglia ha esplicitamente perdonato**. D7 restituisce
la lista esatta delle entry tollerate; l'aggregazione usata da `services/derive.py` e
`services/synthesize.py` calcola `blocking = [f for f in merged if f not in tolerated]` e il nodo è
fidato solo se `blocking` è vuota. Così V2, i numeri non fondati, le prose oltre soglia e ogni altro
criterio continuano a bloccare come oggi, e `TOLERATED_SHARE = 0` produce una lista vuota, cioè il
comportamento pre-#169 con una sola costante. Il DAG V1 → D7 → V2 e l'ordine non cambiano.

## Decision

**Scelta**: tolleranza proporzionale (`max(1, 10% delle claim`) con **numeri non fondati sempre
fatali**. Costante nominata `TOLERATED_SHARE`, nessun numero sparso.

**Alternative rifiutate**:

- *tolleranza assoluta* (es. "fino a 2 claim"): su una derivazione con 60 claim tollera troppo poco e
  su una con 5 tollera il 40%; la proporzione è la sola grandezza che ha senso su derivazioni di
  dimensioni diverse.
- *soglia sui numeri* (contare anche i numeri): è esattamente ciò che non si deve fare — un numero
  inventato è il difetto che il gate esiste per fermare (#145).
- *nessuna soglia, si aspetta il resolver estrattivo*: rimanderebbe ogni derivazione a `draft` per una
  feature futura, e il vault oggi è usato da `search`/`render` senza trust.

**Conseguenze**: 1-2 claim di prosa sbagliate possono convivere con `auto-verified` — sono annotate,
non nascoste, e restano leggibili in `check_failures`. La regola numerica è volutamente
conservativa e catturerà anche nomi propri (`IMO25` → token `imo25`): il ponte non deve comprare
copertura fidandosi di un numero inventato. Il ponte è **reversibile**: `TOLERATED_SHARE = 0` riporta
al comportamento di #164. Destinazione: il resolver estrattivo (PRD #152), che sceglie lo span invece
di sperare nel lessico.

## Premises

| # | Premise | Command | Output | Verdict |
|---|---|---|---|---|
| P1 | Lo stato attuale non lascia passare nulla | `select trust_state from node where kind='summary' and created_at > <cut>` | 6/6 `draft` | confirmed |
| P2 | Le claim non fondate sono quasi tutte prosa | classificazione per cifra assente sul contenuto dei parent | `18 prose / 2 numeric` su 20 | confirmed |
| P3 | La regola numerica ha falsi positivi anche su nomi propri | token contenenti cifre delle claim bocciate | `imo25` (da `IMO25`), `630` | confirmed |
| P4 | `passed` oggi è "nessun failure" | lettura di `run_validations` | `passed=len(failures) == 0` | confirmed |
| P5 | Il tag di severità non-fatale esiste già | CHANGELOG 0.2.0 + `SEVERITY_FATAL` in `validate.py` | `two-level severity tags (fatal vs quality) in check_failures` | confirmed |

## Model

Model skipped — nessuna entità con ciclo di vita: cambia l'aggregazione booleana di un gate; nessuno
stato nuovo, nessuna transizione.

## User Stories

1. Come agente che legge il vault, voglio che una derivazione ben fondata arrivi `auto-verified`, così
   che la mia stop condition si fermi su di essa invece di scendere al L0.
2. Come proprietario del vault, voglio che una claim con numeri inventati resti sempre bloccante, così
   che il ponte non compri copertura a spese dell'anti-invenzione.
3. Come manutentore, voglio vedere *quante* claim sono state tollerate, così che un ponte troppo
   permissivo sia misurabile invece che invisibile.

## Tickets

- #169 — D7: ponte — soglia con numeri hard invece di tutto-o-niente

## Delivery

- #169 — PR: da aprire · Model: n/a — aggregazione di un gate, nessuna entità · Mutation: n/a — da eseguire in questa sessione
