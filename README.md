# Agentic Context Management

## What this project does

This project implements the [Agentic Context Management lifecycle](https://arxiv.org/abs/2607.21503) for [oh-my-pi](https://github.com/can1357/oh-my-pi) as an extension; OMP core remains untouched. A local service runs Postgres with pgvector and Ollama, while a thin TypeScript extension uses OMP's public hooks and tools.

```text
OMP extension
  ├─ session_before_compact ──> POST /compact/match ──> exact validated hit | native fallback
  ├─ agent_end ──────────────> POST /ingest ───> async extract/store
  ├─ turn_end ───────────────> POST /anticipate; automatic compaction arming disabled
  ├─ context ────────────────> GET /bundle/{session} -> user memory
  └─ acm_* tools, /acm command ────────────────> ACM HTTP API

ACM service ──> Postgres + pgvector
            └─> local Ollama chat + embedding models
```

### Deltas from OMP today

ACM adds per-agent generated memory architecture, anticipatory retrieval (trajectory → predicted intents → precomputed session bundle) with zero retrieval on the critical path and `acm_fetch` as explicit miss fallback, plus runtime-validated compaction. Compaction uses probe/reference-pair recoverability checks, equivalence judging, deterministic verbatim/file checks, a retry ladder with real budget growth, and honest score-`0.0` fall-through to native compaction.

### OMP today vs the paper (evidence-verified)

| Primitive | OMP today | ACM delta |
|---|---|---|
| Architecting | Static prompt packages in `packages/coding-agent/src/prompts/memories/` plus fixed Mnemopi schema: partial, hand-defined coverage (paper §7, T4 function a). | `acm_architect` synthesizes a per-agent architecture that governs extraction and compaction. |
| Ingesting | Mnemopi LLM fact extraction, `proactiveLinking` episodic graph, local embeddings, FTS, working-memory TTL, sleep/consolidation: largely present. [Source: `omp://mnemosyne-memory-backend.md`] | Architecture-driven extraction; entity-resolution cascade with aliases; temporal validity; async job IDs; write-side sanitization. |
| Scoping | Global, per-project, and per-project-tagged banks: two-level personal scope. [Source: `omp://mnemosyne-memory-backend.md`] | Provenance-tagged, token-budgeted assembly. |
| Anticipating | `compaction.asyncEnabled` speculates only about compaction; Mnemopi `enhancedRecall` is a query cache, which paper §4 explicitly excludes. Absent. [Source: `omp://mnemosyne-memory-backend.md`] | Trajectory-predicted precomputed session bundle; `context` is pure cache read; `acm_fetch` is explicit miss fallback; hit-rate stats. |
| Compacting & consolidation | `remote`/`snapcompact`/`handoff`/`shake`/`soft` method order, pruning, and useless-elision: strongest-in-class machinery, but no validation contract. [Source: `omp://compaction.md`] | Probe/reference-pair recoverability validation, equivalence judging, deterministic verbatim/file checks, score-0 fall-through, retry ladder with real budget growth. Hook is exact-digest cache read under OMP's 30-second cap. Automatic validated compaction ships off until live OMP proves an auto-threshold compaction matches an armed digest; `acm_compact` remains guaranteed path. |

ACM has three modes: `full` (default), `memory` (ACM memory only with native compaction), and `compaction` (ACM compaction only, coexisting with Hindsight/Mnemopi as `memory.backend`). Configure with `--acm-mode` or `ACM_MODE`.

Interactive sessions show status below editor: mode, bundle hits/misses, harvested count, and latest compaction score. At session start, ACM probes health then renders `ACM <mode> · connected` or `offline`.

### Honest limitations

| Area | Current behavior | Upgrade trigger |
|---|---|---|
| Trust boundary | Sanitizes persisted/served memory; service is local-only, no auth. | Expose beyond loopback -> add authenticated API boundary and ACLs. |
| Anticipation | Best-effort async cache; a missing/expired bundle injects nothing. | Need guaranteed recall -> use explicit `acm_fetch`. |
| Compaction | Automatic validated compaction is off (`ACM_AUTO_ARM=0`): public `turn_end` lacks native preparation's dynamic cut point, turn prefix, and file ops. Hook only reads an exact canonical-digest match; any miss falls through native. | Live OMP proves an automatic threshold compaction matches an armed digest -> enable arming; otherwise use explicit `acm_compact`. |
| Selfcheck | Intentionally writes only synthetic scope/bundle; may wait up to about one minute for async bundle. | Need non-mutating probe -> add service-owned health/readiness route. |
| Schema init | Each service process holds a Postgres advisory lock while reapplying additive `IF NOT EXISTS` DDL; no migration history or destructive DDL. | Startup contention or a non-additive migration -> introduce a versioned migration runner. |

## Install

### Prerequisites

- [oh-my-pi](https://github.com/can1357/oh-my-pi)
- Docker
- Ollama, with `qwen3:4b` and `qwen3-embedding:0.6b` (1024 dimensions) reachable on port `11434`, from a native install or container

Clone this repository, pull required Ollama models, then start local dependencies and ACM:

```bash
git clone https://github.com/5c0r/omp_acm_extension.git
cd omp_acm_extension
ollama pull qwen3:4b
ollama pull qwen3-embedding:0.6b
docker compose -p acm up -d --build
```

The service listens on `127.0.0.1:8927`; Postgres listens on `127.0.0.1:5433`.

Link the extension:

```bash
ln -s "$(pwd)/extension" "$HOME/.omp/agent/extensions/acm"
```

Start a fresh `omp` session. Footer should show `ACM <mode> · connected`; run `/acm selfcheck` until every row is `PASS`, then run `/acm status`.

### Memory UI

Open [http://127.0.0.1:8927/ui/](http://127.0.0.1:8927/ui/) for local memory operations. Dashboard is default: counts by kind/status/scope, bundle hit/miss, ingest outcomes, compaction validation history, and top-used memories. Browse filters by exact scope, kind, status, and text; a memory detail shows content, importance, temporal validity, provenance, entities, and its usage drill-down.

Detail actions edit content, archive/restore, merge into a same-scope memory, pin/unpin, set importance, and add/remove entity aliases. Archive is recoverable: ACM has no hard-delete UI action.

`/acm browse` provides same flow in OMP: select scope, select memory, inspect detail, then manage it through native dialogs. Non-interactive OMP sessions print scope summary only.

For a repeatable local roundtrip, seed the browse demo once or repeatedly:

```bash
curl -X POST http://127.0.0.1:8927/api/ui/seed-demo
```

It idempotently creates `project:browse-live` memory containing `Widget API key rotates weekly`.

### Runbook

```bash
# Service lifecycle proof.
docker compose -p acm run --rm --no-deps \
  -v "$PWD/service/tests:/app/tests:ro" \
  -e ACM_TEST_BASE_URL=http://acm-service:8927 \
  acm-service pytest tests/test_lifecycle.py -q

# Extension tests; second command invokes local ACM for real deadline proof.
(cd extension && bun test extension.test.ts)
(cd extension && ACM_LIVE_TEST=1 bun test extension.test.ts)
```

`/acm status` reports active mode, health, service stats, and session bundle/ingest counters. `/acm selfcheck` uses its own `project:acm-selfcheck-<session>` scope and `acm-selfcheck-<session>` bundle key, then prints endpoint pass/fail rows. `/acm inject on|off` toggles bundle injection for current runtime. `/acm last-compaction` shows latest validated hook result.

### Modes and off-switches

Select a preset at OMP startup with `omp --acm-mode=<mode>` or `ACM_MODE=<mode> omp`. `ACM_MODE` takes precedence over `--acm-mode`; absent config selects `full`.

| Preset | Agent-end ingest | Anticipation and automatic bundle injection | Automatic compaction hook |
|---|---:|---:|---:|
| `full` | On | On | On |
| `memory` | On | On | Off |
| `compaction` | Off | Off | On |

`ACM_AUTO_INJECT=0` and `ACM_AUTO_ARM` retain their existing granular behavior inside applicable presets. `acm_compact` remains available in every preset.

| Environment | Default | Effect |
|---|---|---|
| `ACM_WIDGET` | on | Set `0` to disable ACM footer status line (env name kept for compatibility). Session start probes health then renders `ACM <mode> · connected` or `offline`; activity counts advance only after verified ACM responses. |

OMP extensions cannot read arbitrary `config.yml` keys; use flag or env until OMP core exposes extension settings. `compaction` leaves memory ownership to Hindsight/Mnemopi; no Hindsight-to-ACM bridge exists.

Disable the extension through OMP settings:

```json
{ "disabledExtensions": ["extension-module:acm"] }
```

`ACM_AUTO_INJECT=0` disables automatic bundle injection. `ACM_WIDGET=0` disables footer status. `ACM_AUTO_ARM=0` is the fail-closed default; set `ACM_AUTO_ARM=1` only to opt into automatic validated-compaction arming.

### Per-turn pattern

1. Use `acm_fetch` when cache misses or work needs durable facts.
2. Use `acm_ingest` only for durable facts, decisions, preferences, or episodes.
3. Let `turn_end` prefetch; do not block on anticipation.
4. Accept ACM compaction only when validation score is at least `0.80`.

## Contributing

Branch from `main` using three segments: `feat/<scope>/<desc>`. Use conventional commits, for example `feat(acm): add retrieval scorer`.

Changes MUST keep suites green:

```bash
cd extension && bun test
```

Run service suites through Compose with commands above. Extension or hook changes also require a live smoke: `/acm selfcheck` and a fresh-session footer render.

Design reviews are load-bearing. Validation, anticipation, and arming semantics have failed naive versions: read [Honest limitations](#honest-limitations) and plan changelog history in merged PR #1 before touching compaction or arming logic.

No OMP core changes are accepted; use extension surface only.

## License

MIT — see [LICENSE](LICENSE).
