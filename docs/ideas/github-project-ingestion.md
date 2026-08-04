# GitHub Project Ingestion — Idea

## Cosa

Estendere memex per ingerire progetti GitHub come fonte, allo stesso modo di articoli/YouTube/PDF.
Un progetto GitHub diventa un nodo L0 (o un set di nodi) nel grafo, collegabile via synthesis
a idee prese da paper, articoli, video.

## Come si inserisce nello schema esistente

| Oggi (link) | GitHub project |
|---|---|
| URL → HttpFetcher / RoutingFetcher | `github.com/user/repo` → `GitHubProjectFetcher` |
| Estrai titolo + HTML body / YouTube transcript / PDF text | Estrai README + docs + struttura repo + metadata |
| L0 = raw_source (immutabile) | L0 = README + metadata del progetto |
| derive → notes | derive → "cosa fa, architettura, tech stack" |
| synthesize cross-source | synthesis paper X + progetto Y → "ho implementato l'idea X in Y" |

## Valore

- Collegare idee astratte (da paper, articoli) a implementazioni reali
- Il grafo diventa tracciabile: "questa idea l'ho letta qui → l'ho usata in questo progetto"
- Synthesis cross-source: paper su RAG + progetto che implementa RAG → synthesis unica
- Un progetto può essere "figlio" di più edifici (paper diversi implementati nello stesso repo)

## Domande aperte

1. **Cosa diventa L0?** README? Tree delle directory? File chiave (pyproject.toml, main.rs)?
2. **Forma di ingest?** `memex extract https://github.com/user/repo` come URL normale, o comando dedicato?
3. **Refresh?** I progetti evolvono (nuovi commit, README aggiornato). Serve re-ingest periodico?
4. **Connessione automatica?** All'ingest, cercare synthesis esistenti su argomenti simili e proporre edge `related`?
5. **Scope?** Solo repo pubblici? Anche privati (via token)? Solo README o anche issues, wiki?
6. **Edge cases?** Repo senza README, mono-repo con più progetti, fork, repo archiviati.

## Dipende da

- [#5 Retrieval model](/home/sbottiglieri/memex/issues/5) — graph-aware retrieval per navigare i collegamenti
- [#6 Associative edges beyond CLI](/home/sbottiglieri/memex/issues/6) — per collegamenti automatici
