# memex #120 — integrità dei locatori e congelamento dei nodi (`frozen`)

Sessione di design (fase 5) sul ticket reale #120 di salvatorebottiglieri/memex. Leggi il codice, non il ticket: quattro premesse del ticket sono state verificate (una è falsa, due sono più deboli di come sono scritte). Le decisioni in cima sono il vero bersaglio della revisione: pinna lì se sei in disaccordo, o su un path/una firma se il punto è più in basso. Nel grafo la freccia va dal CHIAMANTE al CHIAMATO (usa): a livello funzione l'etichetta è ciò che passa (argomenti → ritorno), a livello classe/modulo è l'interfaccia consumata con il numero di richiami; `⟲ N` è la coesione (richiami interni). In rosso i call site che non hanno un luogo di gestione per almeno un esito della firma.

> **Forma del file.** La parte che una macchina legge — flusso, contratti, firme,
> stato, call stack, decisioni — sta **tutta** nel blocco qui sotto, in un posto
> solo: la prosa fuori dal blocco e' cio' che nessuna macchina legge, e quindi
> non puo' divergere da niente. Ogni contratto dichiara **tutti e quattro** gli
> esiti (ritorno, assenza, eccezioni, effetti) e ogni esito tranne gli effetti
> dice **dove** e' gestito; un contratto che chiama fuori dal repo dichiara il
> suo **residuo** (cosa succede alle eccezioni della libreria non enumerate).
>
> **I controlli.** Il checker del design (B-023) vive per ora nel repo del
> workflow, non ancora promosso in fase 5. Dal root di questo repo:
>
> ```
> python3 ~/agentic-workflow/experiments/2026-10-interactive-program-design/check-design.py \
>     docs/design/120-frozen.md .
> ```
>
> `exit 1` = qualcosa non regge (coerenza, firme, stato, esiti, frontiera).

<!-- parte macchina: tutto cio' che un controllo o una vista legge -->

```json design
{
  "call_stack": {
    "name": "memex check — integrità dei locatori (read-only, exit 0/1)",
    "note": "il requisito 1 del ticket: oggi non esiste nessun comando con questa portata",
    "children": [
      {
        "name": "cli._integrity_inputs(store) -> (locations, provenance, sources)",
        "note": "il CLI legge e passa DATI: il check non riceve capacità (niente Store, niente SQL) — B-006/R5, least power nelle firme",
        "children": [
          {
            "name": "store.list_content_locations()",
            "note": "nuova lettura: (node_id, content_path)",
            "children": []
          },
          {
            "name": "store.list_edges(type='provenance', relation='derived_from')",
            "note": "esistente e riusata: filtra OPZIONALMENTE (node_id=None), quindi legge gli archi del vault; il CLI proietta le righe a coppie (from_node, to_node)",
            "children": []
          },
          {
            "name": "store.list_sources()",
            "note": "nuova lettura: (node_id, failed)",
            "children": []
          }
        ]
      },
      {
        "name": "integrity.check_integrity(locations, provenance, sources) -> Finding[]",
        "note": "puro: nessuna connessione, nessun SQL, nessun path d'ingresso — testabile con tre liste",
        "children": [
          {
            "name": "find_missing_content(locations)",
            "note": "content_path non nullo e file assente su disco (requisito 1a)",
            "children": []
          },
          {
            "name": "find_dangling_provenance(provenance, locations)",
            "note": "edge derived_from verso un id che non esiste fra i nodi (requisito 1b)",
            "children": []
          },
          {
            "name": "find_dead_source(sources)",
            "note": "segnala `source.failed` e url morti già noti; nessuna sonda HTTP (fuori scope)",
            "children": []
          }
        ]
      },
      {
        "name": "cli.freeze(node_id, --reason) / cli.thaw(node_id)",
        "note": "nuovi comandi di scrittura: il check segnala, l'umano congela (adjudication, ADR-0012)",
        "children": [
          {
            "name": "store.mark_frozen(node_id, reason)",
            "note": "frozen_at + frozen_reason",
            "children": []
          },
          {
            "name": "store.clear_frozen(node_id)",
            "note": "scongelare è esplicito, mai automatico",
            "children": []
          }
        ]
      },
      {
        "name": "cli.list_nodes(--include-frozen)",
        "note": "comando esistente: aggiunto il flag, default = escluso (requisito 3)",
        "children": [
          {
            "name": "store.list_nodes(include_frozen=False)",
            "note": "il filtro entra in ACTIVE_PREDICATE, non in una clause locale",
            "children": []
          }
        ]
      },
      {
        "name": "cli.search(query)",
        "note": "esistente, e NON è una query SQL: è una scansione che legge i file delle derivazioni — il filtro va nel loop",
        "children": []
      },
      {
        "name": "cli.sync()",
        "note": "esistente: il riepilogo riporta i congelati (requisito 4)",
        "children": [
          {
            "name": "store.get_stats() -> {... , \"frozen\": n}",
            "note": "stesso canale dei contestati, nessuna superficie nuova",
            "children": []
          }
        ]
      },
      {
        "name": "Premesse (fase 1) — verificate nel codice, non nella descrizione del ticket",
        "note": "quattro affermazioni del ticket #120, controllate su src/",
        "children": [
          {
            "name": "il ticket: \"D3 copre i wikilink derived_from dangling\"",
            "note": "FALSA. D3 è il check dei marcatori di sintesi (file vs colonna synthesis_statements). I dangling li coprono D2 (_d2_dangling_ref_check: il target di provenance esiste) e D6 (_d6_link_validity_check: ogni wikilink risolve a un parent di provenance) — e solo per le derivazioni, non per il vault.",
            "children": []
          },
          {
            "name": "il ticket: \"serve un nuovo stato\"",
            "note": "PARZIALMENTE FALSA. Esiste già `is_contested` + `contested_at` come flag ortogonale a trust_state: c'è un precedente da seguire, non un enum da estendere.",
            "children": []
          },
          {
            "name": "il ticket: \"escluso da list default e search\" è una cosa sola",
            "note": "FALSA. `list` filtra in SQL (Store.list_nodes), `search` è una scansione sui file delle derivazioni: due meccanismi, due punti di deriva.",
            "children": []
          },
          {
            "name": "esiste già una regola di retrieval documentata",
            "note": "VERA, e il ticket non la nomina: Rule A1/A2 nell'ontologia (rules.py) dichiarano chi l'agente può fermare e chi lo obbliga a scendere — e si dichiarano non implementate. `frozen` va aggiunto lì, non reinventato in un comando.",
            "children": []
          },
          {
            "name": "Rilievo della revisione: mancava l'arco integrity.py → store.py",
            "note": "La firma `check_integrity(store: Store, ...)` dichiarava che il check usa lo store, ma quella dipendenza non era disegnata. Risolto alla radice con la firma pura (decisione 5) invece che disegnando l'arco: un potere in più nella firma era anche un potere in più del checker.",
            "children": []
          },
          {
            "name": "Rilievo della revisione col CODICE: `list_edges` — verificata, non un errore",
            "note": "Il documento dichiarava `list_edges(relation=..., type=...)` come lettura vault-wide. Una prima lettura delle firme reali (nomi, senza i default) l'ha giudicata per-nodo e quindi sbagliata: era una premessa non verificata, e la correzione proposta (`list_provenance_edges`) era inutile. La firma vera è `list_edges(self, *, node_id=None, type=None, relation=None)` — tutti opzionali, docstring 'List edges, optionally filtered': la dichiarazione originale era giusta e resta, con il CLI che proietta le righe a coppie. Morale: il confronto col codice becca il caso B solo se è completo su ciò che afferma di verificare.",
            "children": []
          }
        ]
      }
    ]
  },
  "decisions": [
    {
      "title": "Dove vive `frozen`",
      "chosen": "colonna flag `frozen_at` + `frozen_reason`, ortogonale a trust_state (come `is_contested`)",
      "rejected": "un quinto valore di `trust_state`",
      "why": "trust_state è un ordinale di fiducia dichiarato nell'ontologia (`draft > auto-verified > human-approved > stale`) e usato come filtro in tutta la CLI. `frozen` non è meno fidato: è inattivo. Metterlo nell'ordinale rompe le query esistenti e l'ontologia; il precedente del repo dice flag."
    },
    {
      "title": "Dove si applica l'esclusione (requisito 3)",
      "chosen": "un solo predicato condiviso (`ACTIVE_PREDICATE`) usato da `list`, dal loop di `search` e dalle Rule A1/A2 dell'ontologia",
      "rejected": "una condizione per comando",
      "why": "list e search escludono con meccanismi diversi (SQL vs scansione file). Il predicato oggi esiste solo come prosa nelle Rule A1/A2: o diventa una definizione sola, o la prossima regola di stato lo dimenticherà in uno dei due punti."
    },
    {
      "title": "Chi marca un nodo `frozen`",
      "chosen": "comando esplicito `memex freeze <id> --reason`; `memex check` è read-only e si limita a segnalare",
      "rejected": "marcatura automatica dentro `check`",
      "why": "il ticket stesso dice \"la decisione resta umana\" e ADR-0012 mette l'adjudication nel flusso di review. Un check che scrive non è ri-eseguibile e non è confrontabile fra due esecuzioni."
    },
    {
      "title": "Il check vault-wide: modulo nuovo o `checks.py` esteso",
      "chosen": "`src/memex/integrity.py` nuovo, puro come checks.py (niente rete, niente LLM)",
      "rejected": "estendere `checks.py` con regole D7, D8",
      "why": "checks.py è il gate per-derivazione draft→auto-verified: un nodo per volta, dentro il flusso di derive. Il check dei locatori è a scala vault e legge il filesystem. Regole diverse, costo diverso, momento diverso: se stanno insieme, il gate della derivazione paga un controllo che non lo riguarda."
    },
    {
      "title": "La firma del check: dati o capacità",
      "chosen": "firma pura — `check_integrity(locations, provenance, sources)`, nessuno Store: chi legge dal DB è il CLI",
      "rejected": "`check_integrity(store: Store, vault_path)`",
      "why": "Il grafo ha mostrato il buco: la firma dichiarava che il check usa lo store, ma l'arco integrity.py → store.py non c'era, e il disegno sembrava piatto invece che a stack. La causa non era l'arco mancante: un check che ha bisogno di due colonne non deve ricevere connessione, SQL e transazioni — è la regola di B-006/R5 (`least power` nelle firme), e il suo test unit passa da un database a tre liste."
    }
  ],
  "data_flow": {
    "nodes": [
      {
        "id": "cli.check",
        "module": "src/memex/cli.py",
        "owner": null,
        "kind": "add"
      },
      {
        "id": "integrity.check_integrity",
        "module": "src/memex/integrity.py",
        "owner": null,
        "kind": "add"
      },
      {
        "id": "integrity.find_missing_content",
        "module": "src/memex/integrity.py",
        "owner": null,
        "kind": "add"
      },
      {
        "id": "integrity.find_dangling_provenance",
        "module": "src/memex/integrity.py",
        "owner": null,
        "kind": "add"
      },
      {
        "id": "integrity.find_dead_source",
        "module": "src/memex/integrity.py",
        "owner": null,
        "kind": "add"
      },
      {
        "id": "cli.freeze",
        "module": "src/memex/cli.py",
        "owner": null,
        "kind": "add"
      },
      {
        "id": "cli.thaw",
        "module": "src/memex/cli.py",
        "owner": null,
        "kind": "add"
      },
      {
        "id": "store.mark_frozen",
        "module": "src/memex/store.py",
        "owner": "Store",
        "kind": "add"
      },
      {
        "id": "store.clear_frozen",
        "module": "src/memex/store.py",
        "owner": "Store",
        "kind": "add"
      },
      {
        "id": "cli.list_nodes",
        "module": "src/memex/cli.py",
        "owner": null,
        "kind": "mod"
      },
      {
        "id": "store.list_nodes",
        "module": "src/memex/store.py",
        "owner": "Store",
        "kind": "mod"
      },
      {
        "id": "cli.search",
        "module": "src/memex/cli.py",
        "owner": null,
        "kind": "mod"
      },
      {
        "id": "store.get_node",
        "module": "src/memex/store.py",
        "owner": "Store",
        "kind": "mod"
      },
      {
        "id": "cli.sync",
        "module": "src/memex/cli.py",
        "owner": null,
        "kind": "mod"
      },
      {
        "id": "store.get_stats",
        "module": "src/memex/store.py",
        "owner": "Store",
        "kind": "mod"
      },
      {
        "id": "cli.integrity_inputs",
        "module": "src/memex/cli.py",
        "owner": null,
        "kind": "add"
      },
      {
        "id": "store.list_content_locations",
        "module": "src/memex/store.py",
        "owner": "Store",
        "kind": "add"
      },
      {
        "id": "store.list_sources",
        "module": "src/memex/store.py",
        "owner": "Store",
        "kind": "add"
      },
      {
        "id": "store.list_edges",
        "module": "src/memex/store.py",
        "owner": "Store",
        "kind": "keep"
      }
    ],
    "edges": [
      {
        "from": "integrity.check_integrity",
        "to": "integrity.find_missing_content",
        "data": "store → list[Finding]"
      },
      {
        "from": "integrity.check_integrity",
        "to": "integrity.find_dangling_provenance",
        "data": "store → list[Finding]"
      },
      {
        "from": "integrity.check_integrity",
        "to": "integrity.find_dead_source",
        "data": "store → list[Finding]"
      },
      {
        "from": "cli.check",
        "to": "cli.integrity_inputs",
        "data": "store → (locations, provenance, sources)"
      },
      {
        "from": "cli.integrity_inputs",
        "to": "store.list_content_locations",
        "data": "→ list[(id, content_path)]"
      },
      {
        "from": "cli.integrity_inputs",
        "to": "store.list_edges",
        "data": "type, relation → list[dict]"
      },
      {
        "from": "cli.integrity_inputs",
        "to": "store.list_sources",
        "data": "→ list[(id, failed)]"
      },
      {
        "from": "cli.check",
        "to": "integrity.check_integrity",
        "data": "locations, provenance, sources → list[Finding]"
      },
      {
        "from": "cli.freeze",
        "to": "store.mark_frozen",
        "data": "node_id, reason → None (0 righe? nessuno lo dice)"
      },
      {
        "from": "cli.thaw",
        "to": "store.clear_frozen",
        "data": "node_id → int (righe toccate)"
      },
      {
        "from": "cli.thaw",
        "to": "store.get_node",
        "data": "node_id → dict | None (pre-lettura)"
      },
      {
        "from": "cli.list_nodes",
        "to": "store.list_nodes",
        "data": "include_frozen → list[dict]"
      },
      {
        "from": "cli.search",
        "to": "store.get_node",
        "data": "deriv_id → dict | None"
      },
      {
        "from": "cli.sync",
        "to": "store.get_stats",
        "data": "→ dict"
      }
    ]
  },
  "contracts": [
    {
      "site": "cli.check",
      "callee": "integrity.check_integrity",
      "exits": [
        {
          "kind": "returns",
          "type": "list[Finding]",
          "handled_at": "cli.py:check — JSON, exit 1 se non vuota"
        },
        {
          "kind": "absent",
          "type": "lista vuota",
          "handled_at": "cli.py:check — exit 0"
        },
        {
          "kind": "raises",
          "type": "nessuna dichiarata: il check non tocca più il DB",
          "handled_at": "propagato — il buco OSError/permissioni resta a carico di integrity.find_missing_content"
        },
        {
          "kind": "effect",
          "type": "nessuno (read-only)",
          "handled_at": null
        }
      ]
    },
    {
      "site": "cli.check",
      "callee": "cli.integrity_inputs",
      "exits": [
        {
          "kind": "returns",
          "type": "(locations, provenance, sources)",
          "handled_at": "cli.py:check — passate al check, nessuna interpretazione"
        },
        {
          "kind": "absent",
          "type": "n/a — una tupla, non un valore che può mancare",
          "handled_at": "non applicabile: una tupla non ha un valore che può mancare"
        },
        {
          "kind": "raises",
          "type": "sqlite3.OperationalError (dalle tre letture)",
          "handled_at": "cli.py:91 _require_db"
        },
        {
          "kind": "effect",
          "type": "nessuno (read-only)",
          "handled_at": null
        }
      ]
    },
    {
      "site": "cli.integrity_inputs",
      "callees": [
        "store.list_content_locations",
        "store.list_edges",
        "store.list_sources"
      ],
      "exits": [
        {
          "kind": "returns",
          "type": "le tre liste",
          "handled_at": "cli.py:integrity_inputs — passate al check, nessuna interpretazione"
        },
        {
          "kind": "absent",
          "type": "vault vuoto (liste vuote)",
          "handled_at": "integrita' ok, nessun finding: il check su liste vuote non produce nulla"
        },
        {
          "kind": "raises",
          "type": "sqlite3.OperationalError",
          "handled_at": "cli.py:91 _require_db — il DB mancante è già intercettato prima"
        },
        {
          "kind": "effect",
          "type": "nessuno (read-only)",
          "handled_at": null
        }
      ]
    },
    {
      "site": "integrity.check_integrity",
      "callee": "integrity.find_missing_content",
      "exits": [
        {
          "kind": "returns",
          "type": "list[Finding]",
          "handled_at": "integrity.py:check_integrity — estesa in findings"
        },
        {
          "kind": "absent",
          "type": "lista vuota",
          "handled_at": "integrity.py:check_integrity — nulla da estendere"
        },
        {
          "kind": "raises",
          "type": "nessuna: dati in ingresso, nessun DB",
          "handled_at": "n/a — pura (l'OSError del filesystem resta dichiarato su Path.exists, sotto)"
        },
        {
          "kind": "effect",
          "type": "nessuno",
          "handled_at": null
        }
      ]
    },
    {
      "site": "integrity.check_integrity",
      "callee": "integrity.find_dangling_provenance",
      "exits": [
        {
          "kind": "returns",
          "type": "list[Finding]",
          "handled_at": "integrity.py:check_integrity — estesa in findings"
        },
        {
          "kind": "absent",
          "type": "lista vuota",
          "handled_at": "integrity.py:check_integrity — nulla da estendere"
        },
        {
          "kind": "raises",
          "type": "nessuna: dati in ingresso, nessun DB",
          "handled_at": "n/a — pura"
        },
        {
          "kind": "effect",
          "type": "nessuno",
          "handled_at": null
        }
      ]
    },
    {
      "site": "integrity.check_integrity",
      "callee": "integrity.find_dead_source",
      "exits": [
        {
          "kind": "returns",
          "type": "list[Finding]",
          "handled_at": "integrity.py:check_integrity — estesa in findings"
        },
        {
          "kind": "absent",
          "type": "lista vuota",
          "handled_at": "integrity.py:check_integrity — nulla da estendere"
        },
        {
          "kind": "raises",
          "type": "nessuna: dati in ingresso, nessun DB",
          "handled_at": "n/a — pura"
        },
        {
          "kind": "effect",
          "type": "nessuno",
          "handled_at": null
        }
      ]
    },
    {
      "site": "integrity.find_missing_content",
      "call": "Path.exists() / is_file() sul content_path",
      "residual": "propaga: OSError/PermissionError sono enumerate e diventano Finding per nodo; ogni altra eccezione (bug, MemoryError) risale a cli.check e diventa errore del comando, non un Finding — il check non le cattura",
      "exits": [
        {
          "kind": "returns",
          "type": "True",
          "handled_at": "integrity.py — nessun finding per quel nodo"
        },
        {
          "kind": "absent",
          "type": "percorso esistente ma non è un file (directory, symlink rotto)",
          "handled_at": "integrity.py:find_missing_content — Finding con code content_not_a_file"
        },
        {
          "kind": "raises",
          "type": "OSError / PermissionError",
          "handled_at": "integrity.py:find_missing_content — catturata per nodo: Finding con code content_unreadable; la funzione non solleva per il filesystem"
        },
        {
          "kind": "effect",
          "type": "nessuno",
          "handled_at": null
        }
      ]
    },
    {
      "site": "cli.freeze",
      "callee": "store.mark_frozen",
      "exits": [
        {
          "kind": "returns",
          "type": "int — righe toccate",
          "handled_at": "cli.py:freeze — letto: 1 = congelato, 0 = vedi sotto"
        },
        {
          "kind": "absent",
          "type": "0 righe toccate — id inesistente",
          "handled_at": "cli.py:freeze — {\"error\": \"not_found\"}, exit non-zero (convenzione di memex derive)"
        },
        {
          "kind": "raises",
          "type": "sqlite3.OperationalError",
          "handled_at": "cli.py:91 _require_db"
        },
        {
          "kind": "effect",
          "type": "scrive node.frozen_at / frozen_reason",
          "handled_at": null
        }
      ]
    },
    {
      "site": "cli.thaw",
      "callee": "store.clear_frozen",
      "exits": [
        {
          "kind": "returns",
          "type": "int — righe toccate",
          "handled_at": "cli.py:thaw — letto: 1 = scongelato, 0 = già non congelato"
        },
        {
          "kind": "absent",
          "type": "0 righe toccate su nodo esistente — non era congelato",
          "handled_at": "cli.py:thaw — idempotente: esito 0 con messaggio, nessun errore"
        },
        {
          "kind": "raises",
          "type": "sqlite3.OperationalError",
          "handled_at": "cli.py:91 _require_db"
        },
        {
          "kind": "effect",
          "type": "azzera node.frozen_at / frozen_reason",
          "handled_at": null
        }
      ]
    },
    {
      "site": "cli.thaw",
      "callee": "store.get_node",
      "exits": [
        {
          "kind": "returns",
          "type": "dict",
          "handled_at": "cli.py:thaw — letto per distinguere 'id inesistente' da 'non era congelato' (con la sola riga toccata i due casi sono indistinguibili)"
        },
        {
          "kind": "absent",
          "type": "None — id inesistente",
          "handled_at": "cli.py:thaw — {\"error\": \"not_found\"}, exit non-zero"
        },
        {
          "kind": "raises",
          "type": "sqlite3.OperationalError",
          "handled_at": "cli.py:91 _require_db"
        },
        {
          "kind": "effect",
          "type": "nessuno (read-only)",
          "handled_at": null
        }
      ]
    },
    {
      "site": "cli.search",
      "callee": "store.get_node",
      "exits": [
        {
          "kind": "returns",
          "type": "dict",
          "handled_at": "cli.py:search — usato per content_path"
        },
        {
          "kind": "absent",
          "type": "None (id inesistente, es. dopo delete)",
          "handled_at": "cli.py:search — `if deriv is None: continue`"
        },
        {
          "kind": "raises",
          "type": "sqlite3.OperationalError",
          "handled_at": "cli.py:91 _require_db"
        },
        {
          "kind": "effect",
          "type": "nessuno",
          "handled_at": null
        }
      ]
    },
    {
      "site": "cli.list_nodes",
      "callee": "store.list_nodes",
      "exits": [
        {
          "kind": "returns",
          "type": "list[dict]",
          "handled_at": "cli.py:list_nodes — JSON in uscita"
        },
        {
          "kind": "absent",
          "type": "lista vuota",
          "handled_at": "cli.py:list_nodes — stampa []"
        },
        {
          "kind": "raises",
          "type": "sqlite3.OperationalError",
          "handled_at": "cli.py:91 _require_db"
        },
        {
          "kind": "effect",
          "type": "nessuno",
          "handled_at": null
        }
      ]
    },
    {
      "site": "cli.sync",
      "callee": "store.get_stats",
      "exits": [
        {
          "kind": "returns",
          "type": "dict",
          "handled_at": "cli.py:sync — legge i conteggi"
        },
        {
          "kind": "absent",
          "type": "chiave 'frozen' assente (stats di una versione precedente)",
          "handled_at": "cli.py:sync — `.get('frozen', 0)`"
        },
        {
          "kind": "raises",
          "type": "sqlite3.OperationalError",
          "handled_at": "cli.py:91 _require_db"
        },
        {
          "kind": "effect",
          "type": "nessuno",
          "handled_at": null
        }
      ]
    }
  ],
  "file_tree": [
    {
      "kind": "add",
      "path": "src/memex/integrity.py",
      "note": "check vault-wide, read-only, puro come checks.py"
    },
    {
      "kind": "add",
      "path": "tests/test_integrity.py",
      "note": "unit: file L0 mancante, provenance dangling, nodo sano (nessun falso positivo)"
    },
    {
      "kind": "add",
      "path": "tests/test_cli_frozen.py",
      "note": "subprocess: freeze → list/search escludono → thaw → riappaiono"
    },
    {
      "kind": "add",
      "path": "docs/adr/0019-frozen-nodes-orthogonal-to-trust-state.md",
      "note": "la decisione 1 è di modello: un ADR, non solo una voce di backlog"
    },
    {
      "kind": "mod",
      "path": "src/memex/store.py",
      "note": "migrazione colonne, mark_frozen/clear_frozen, include_frozen in list_nodes, ACTIVE_PREDICATE, get_stats, più due letture per il check (list_content_locations, list_sources)"
    },
    {
      "kind": "mod",
      "path": "src/memex/cli.py",
      "note": "check / freeze / thaw; _integrity_inputs (le tre letture in un punto solo); --include-frozen su list; filtro nel loop di search; conteggio in sync"
    },
    {
      "kind": "mod",
      "path": "src/memex/rules.py",
      "note": "ontologia: Rule A1/A2 nominano frozen; riga nuova per frozen_at/frozen_reason"
    },
    {
      "kind": "mod",
      "path": "CONTEXT.md",
      "note": "il termine `frozen` nel glossario (accanto a contested)"
    },
    {
      "kind": "keep",
      "path": "src/memex/checks.py",
      "note": "INVARIATO: D1–D6 restano il gate della derivazione, non diventano un check di vault"
    }
  ],
  "signatures": [
    {
      "module": "src/memex/integrity.py (nuovo)",
      "language": "python",
      "code": "@dataclass(frozen=True)\nclass Finding:\n    node_id: str\n    code: Literal[\"missing_content\", \"content_not_a_file\", \"content_unreadable\", \"dangling_provenance\", \"dead_source\"]\n    detail: str\n\ndef check_integrity(\n    locations: Iterable[tuple[str, str | None]],   # (node_id, content_path)\n    provenance: Iterable[tuple[str, str]],         # (from_node, to_node)\n    sources: Iterable[tuple[str, bool]],           # (node_id, failed)\n) -> list[Finding]:\n    \"\"\"Tutti i locatori rotti, su dati passati: niente Store, niente SQL.\"\"\"\n\ndef find_missing_content(locations) -> list[Finding]: ...  # totale: classifica i guasti del filesystem, non li solleva\ndef find_dangling_provenance(provenance, locations) -> list[Finding]: ...\ndef find_dead_source(sources) -> list[Finding]: ...",
      "note": "puro come checks.py (niente rete, niente LLM) e in piu' senza capacita': il test unit e' tre liste, non un database"
    },
    {
      "module": "src/memex/cli.py — il lettore",
      "language": "python",
      "code": "def _integrity_inputs(store: Store) -> tuple[\n    list[tuple[str, str | None]],\n    list[tuple[str, str]],\n    list[tuple[str, bool]],\n]:\n    \"\"\"Le tre letture che il check richiede, in un punto solo.\"\"\"\n\n@cli.command()\ndef check(db_path, vault_path) -> None:\n    with Store.open(db_path) as store:\n        findings = check_integrity(*_integrity_inputs(store))",
      "note": "il potere di leggere resta dove sta il SQL; il check riceve dati. E' il punto in cui la decisione 5 diventa codice"
    },
    {
      "module": "src/memex/store.py",
      "language": "python",
      "code": "# una definizione sola, riusata da list, search e dalle Rule A1/A2\nACTIVE_PREDICATE = (\n    \"n.is_contested = 0 AND n.frozen_at IS NULL \"\n    \"AND n.trust_state IN ('auto-verified', 'human-approved')\"\n)\n\ndef mark_frozen(self, *, node_id: str, reason: str, at: str) -> int: ...\ndef clear_frozen(self, *, node_id: str) -> int: ...\n# righe toccate: 0 è un esito da smaltire, non un silenzio (decisione 7)\n\ndef list_nodes(self, *, kind=None, tier=None, trust_state=None,\n               confidence=None, include_frozen: bool = False,\n               limit=None, offset=None) -> list[dict[str, Any]]: ...\n\ndef get_node(self, node_id: str) -> dict[str, Any] | None: ...\ndef get_stats(self) -> dict: ...\n\n# le tre letture che servono al check (decisione 5: il check riceve dati)\ndef list_content_locations(self) -> list[tuple[str, str | None]]: ...\ndef list_sources(self) -> list[tuple[str, bool]]: ...\n\n# esistente e riusata: filtra OPZIONALMENTE (node_id=None), quindi legge il vault\ndef list_edges(self, *, node_id: str | None = None, type: str | None = None,\n               relation: str | None = None) -> list[dict]: ...",
      "note": "include_frozen entra nelle clause esistenti, non in una funzione nuova; i campi frozen_at/frozen_reason escono insieme a is_contested/contested_at"
    },
    {
      "module": "src/memex/store.py — migrazione",
      "language": "sql",
      "code": "ALTER TABLE node ADD COLUMN frozen_at     TEXT;\nALTER TABLE node ADD COLUMN frozen_reason TEXT;",
      "note": "colonne nullable: nessun rebuild della tabella (il pattern node_new in store.py serve ai cambi di NOT NULL). Da aggiungere al guard della migrazione."
    },
    {
      "module": "src/memex/cli.py",
      "language": "python",
      "code": "@cli.command()\ndef check(db_path, vault_path) -> None: ...      # read-only, exit 1 se ci sono finding\n\n@cli.command()\ndef freeze(db_path, vault_path, node_id: str, reason: str) -> None: ...\n\n@cli.command()\ndef thaw(db_path, vault_path, node_id: str) -> None: ...\n\n@cli.command()  # esistenti, con il filtro frozen\ndef list_nodes(db_path, vault_path, include_frozen: bool = False) -> None: ...\n\ndef search(db_path, vault_path, query: str) -> None: ...\ndef sync(db_path, vault_path) -> None: ...",
      "note": "il ticket propone `memex check` \"o estensione di status\": comando separato, perché `status` è un riepilogo e questo è un elenco con exit code"
    },
    {
      "module": "src/memex/rules.py — ontologia",
      "language": "text",
      "code": "Rule A1 — Agent stop condition\n  trust_state ∈ {'auto-verified', 'human-approved'}\n  AND is_contested = 0\n  AND frozen_at IS NULL          <- nuovo\n\nRule A2 — Contested nodes require descent\n  IF is_contested = 1 OR trust_state ∈ {'draft','stale'} OR frozen_at IS NOT NULL\n    THEN descend",
      "note": "il posto giusto per la regola: l'ontologia dichiara già sé stessa come non implementata — dopo #120 A1/A2 hanno un'implementazione sola, ACTIVE_PREDICATE"
    },
    {
      "module": "src/memex/cli.py — search (esistente)",
      "language": "python",
      "code": "# il filtro non è SQL: search scorre le derivazioni e legge i file\nderiv = store.get_node(deriv_id)\nif deriv is None or deriv.get(\"frozen_at\"):\n    continue\nif not deriv.get(\"content_path\"):\n    continue",
      "note": "il punto di deriva previsto dalla decisione 2, in chiaro: qui il predicato non è una WHERE"
    }
  ]
}
```
