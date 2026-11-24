---
feature: 167-wording
repo: salvatorebottiglieri/memex
branch: feat-167-wording
tracker: https://github.com/salvatorebottiglieri/memex/issues/167
---

# 167-wording — il derivatore usa le parole della fonte, e un solo prompt condiviso

## Phase plan

| Phase | Decision | Reason |
|---|---|---|
| 1 · Design & alignment | run | le premesse sono misurate sui tre prompt esistenti e su due claim reali bocciate |
| 2 · Product review | skip | nessuna UI: cambia il testo di un prompt |
| 3 · System architecture | skip | nessun componente nuovo né confine di sistema: un modulo di costanti e tre import |
| 4 · Tickets | run | un ticket, #167 |
| 5 · Program design | skip | nessuna firma pubblica nuova; la scelta di progetto (core condiviso = variante ricca) è in `## Decision` |
| 6 · Implement-loop | run | — |

Approved: 2026-11-24, owner in chat — "ok" (dopo la proposta: regola di wording + estrazione del prompt duplicato in un posto solo)

## Problem Statement

Il gate D7 fonda la claim sui token del sorgente, ma il derivatore parafrasa liberamente: senza
resolver estrattivo, la parafrasi è indistinguibile da una fabbricazione finché cambiano le parole.
Misura su una derivazione reale del vault (`model-based-testing-with-xstate-test`, 2026-11-24): 2
claim su 40 bocciate.

- «In a real app the public API or UI already being tested is used instead.» contro il sorgente «In
  your app, use the public API or UI you already test.» → coverage **0.50** (soglia 0.60): *tested≠test*,
  *used≠use*, *real app≠your app*. Falso negativo del lessico.
- «A checkout flow illustrates the gap.» contro «Take a checkout flow.» → **0.50**: "illustrates the
  gap" è commento del derivatore. Qui il gate ha ragione.

Il prompt di derivazione (regola 2) chiede che ogni frase del body «restates a fact FROM a source» ma
non dice **con quali parole**. E il prompt esiste in tre copie divergenti.

## Solution

1. **Regola di wording** nel prompt: ogni frase del body riusa le parole della fonte (citazione quasi
   letterale); tutto ciò che è riscritto con parole proprie va in `## Synthesis`. Coerente con la
   regola già presente (body = fatti della fonte, synthesis = inferenze).
2. **Un solo posto**: le regole comuni dei tre `_DERIVE_SYSTEM_PROMPT` (`anthropic.py`,
   `claude_code.py`, `pi.py`) diventano una costante condivisa; ogni deriver compone core + le sue
   regole specifiche (la variante di regola 2 e la regola di consegna del risultato).

Finché non esiste un resolver estrattivo, la citazione quasi letterale è il solo modo per rendere
affidabile un gate deterministico a token.

## Decision

**Scelta**: il core condiviso usa la **variante ricca** della regola 2 (quella del deriver `pi`, con
le regole "start DIRECTLY", "never a preamble", "never a meta-claim about a source"), e non la
versione breve di `anthropic`/`claude_code`.

**Perché**: è un sovrainsieme — vieta preamboli e meta-claim, cioè proprio la classe di frase che il
gate rifiuta ("A checkout flow illustrates the gap"). Tenerla solo in `pi` significa che due deriver
su tre continuano a produrre quel tipo di frase.

**Alternative rifiutate**: (a) lasciare tre copie e aggiungere la regola in tre punti — è lo stato
attuale che ha già divergito (regola 2 in due versioni, regola 5 diversa, regola 6 solo in `pi`);
(b) core minimo = intersezione stretta delle tre — lascerebbe fuori le regole anti-preamble, cioè
l'unico contenuto che cambia il comportamento.

**Conseguenze**: `anthropic` e `claude_code` ereditano le regole più strette (cambio di comportamento
voluto, approvato dall'owner); le regole specifiche di consegna del risultato (JSON puro per due,
`submit_derivation` per `pi`) restano per-deriver e non vengono unificate.

**Amendamento 2026-11-24 (in corso di implementazione, deciso dall'owner in chat)**: anche la frase
sui link della regola 4 diverge — `anthropic`/`claude_code` dicono `[[filename|alias]]` e "exact link
targets", `pi` dice `[[link_key|alias]]` e "exact link keys, e.g. [[P1|alias]]". Poiché
`services/synthesize.py` costruisce `# Sources` come `- [[P1|alias]]` per ogni agente inline e risolve
`P1 → filename` **dopo** (`_resolve_link_keys`), la variante corretta è quella di `pi`: il core porta
quella formulazione, in forma generica ("l'esatta link key che il blocco `# Sources` elenca per quel
parent, es. `[[P1|alias]]`"). È quindi il **terzo** cambio di comportamento intenzionale, oltre
all'upgrade della regola 2 e alla regola di wording. Premesse P2/P3 incomplete: la divergenza della
regola 4 non era stata misurata prima di scriverle.

## User Stories

1. Come agente che legge il vault, voglio derivazioni `auto-verified`, così che la mia stop condition
   (ADR-0004) si fermi sulla derivazione invece di scendere al L0.
2. Come proprietario del vault, voglio che una derivazione usi le parole della fonte, così che il gate
   deterministico possa verificarla senza un modello estrattivo.
3. Come manutentore, voglio una sola definizione delle regole comuni, così che non divergano fra i
   deriver.

## Premises

| # | Premise | Command | Output | Verdict |
|---|---|---|---|---|
| P1 | I tre prompt sono tre testi diversi, non copie | eval dei tre `_DERIVE_SYSTEM_PROMPT` | `anthropic 1196 · claude_code 1266 · pi 1951` caratteri | confirmed |
| P2 | `anthropic` e `claude_code` differiscono solo nella coda della regola 5 | `difflib.unified_diff` sulle righe | una sola riga `@@ -6 +6 @@` (`Return ONLY the JSON object…`) | confirmed |
| P3 | `pi` ha la regola 2 più ricca e una regola 6 in più | stesso diff | regola 2 con "Start DIRECTLY"/"NEVER a preamble"/"NEVER a meta-claim", più `6. Submit the result by calling the submit_derivation tool` | confirmed |
| P4 | La regola di wording è assente da tutti e tre | grep di `own words`, `verbatim`, `quote` nel testo dei tre prompt | `assente` per tutti e tre | confirmed |
| P5 | La classe di frase che il gate boccia è quella che la regola 2 di `pi` vieta | confronto delle 2 claim bocciate con il testo della regola | "A checkout flow illustrates the gap." = meta-commento sull'esempio, vietato da "NEVER a meta-claim ABOUT a source" | confirmed |

## Model

Model skipped — nessuna entità con ciclo di vita: cambia il testo di un prompt e si estrae una
costante; nessuno stato nuovo, nessuna transizione.

## Tickets

- #167 — Derivazione: istruire il modello a usare le parole della fonte (e un solo prompt condiviso)

## Delivery

- #167 — PR: da aprire · Model: n/a — testo di prompt, nessuna entità · Mutation: n/a — da eseguire in questa sessione
