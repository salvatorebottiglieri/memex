# ADR-0017: Agent as a long-lived RPC service

The agent seam (`MEMEX_AGENT`) called the omp CLI as a **one-shot subprocess per call**:
`omp -p --mode json --no-session`, wait for exit, parse JSONL stdout, extract the last
`message_end` text. Three observed failures (2026-08-05, vault batch of 23 derivations):

1. **Boot per call** — the Rust engine, discovery, and model registry start from zero
   for every node: ~20s/node of wall-clock that is not LLM time (458s for 23 nodes).
2. **Fragile text parsing** — the agent produces text; JSON is extracted afterwards.
   The adversarial validator hit exactly this: a non-JSON reply silently downgraded the
   quality gate to pass-with-warning.
3. **No streaming / no abort** — the subprocess blocks until the turn ends; a hard
   `timeout=` is the only escape hatch. No `steer`, no graceful `abort`, no `compact`.

## Decision

Replace the one-shot CLI calls with a **long-lived `omp --mode rpc` process** per memex
invocation: NDJSON over stdio, the protocol the omp SDK documents for non-Node hosts.
The agent is spawned lazily on the first call, reused across the whole batch, and
disposed at process exit.

- `OMPRpcAgent` (`memex.derivers.pi`) keeps the `Agent` seam and `call_llm(prompt, *,
  allow_read) -> str`; derive/extract-ideas/review/validate are unchanged callers.
- Turn lifecycle: one prompt in flight per process (serialized by a lock); the client
  assembles `text_delta` events and resolves on `agent_end` with the stop reason.
- **Timeout** (default 600s, `MEMEX_OMP_TIMEOUT`): `abort` command → SIGTERM → SIGKILL;
  a call either returns the full turn or raises — never a partial success.
- **Crash handling**: the in-flight call fails with the stderr tail; the process
  respawns once; a second crash raises.
- **Reader mode preserved**: the process runs with `--tools=read`, so multi-megabyte
  sources stay derivable (the reason omp beats a direct-API agent for memex).
- The one-shot `PiAgent`/`OMPAgent` classes are **removed** — no second convention.

## Alternatives considered

- **SDK in-process (`@oh-my-pi/pi-coding-agent`)**: the SDK is Bun-gated TypeScript
  (774 `Bun.*` API uses; `bun:sqlite` in the session store). No maintained Bun-capable
  JS embedder for CPython exists (Pythonmonkey = SpiderMonkey/Node-compat; PyMiniRacer =
  bare V8). omp itself is the polyglot-in-one-process product (Rust + embedded Bun
  runtime), but as a compiled binary — its Rust core is not published as an embeddable
  crate/C ABI, so a PyO3 wrapper is not possible today. Rejected: infeasible from CPython.
- **Direct-API agent (AnthropicAgent)**: truly in-process, but loses reader mode and
  omp's model routing. Kept as an alternative agent, not the default.
- **Status quo (one-shot)**: the three failures above remain.

## Consequences

- **Positive**: engine boot paid once per invocation instead of per node.
- **Positive**: streaming is available (deltas can be surfaced), and turns can be
  aborted gracefully instead of hard-killed.
- **Positive**: the wire is the same one `--mode json` already used — the codebase
  already spoke it, one-shot.
- **Positive**: reader mode and omp's auth/model discovery are inherited unchanged.
- **Negative**: one long-lived child process per CLI invocation (was: one short-lived
  per call). Cleaned up via `atexit`; a hard kill of memex can orphan it.
- **Risk**: the RPC wire is versioned (`protocolVersion` in `ready`); a breaking omp
  upgrade surfaces as a clear startup error, not silent misbehavior.
- **Done (2026-08-05)**: `set_host_tools` with typed host tools (`submit_derivation`,
  `submit_ideas`, `submit_review`, `submit_validation`) — structured results with
  text parsing demoted to fallback; the validator's parse-failure downgrade is gone.
  Verified live on the real wire and pinned by the fake-omp test suite.
