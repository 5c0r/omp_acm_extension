# Agentic Context Management

Local ACM service plus Oh My Pi extension. OMP core remains untouched: `extension/index.ts` uses public extension hooks/tools only.

## Architecture

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

## Runbook

```bash
# Start local dependencies and service.
docker compose -p acm up -d --build acm-service

# Service lifecycle proof.
docker compose -p acm run --rm --no-deps \
  -v "$PWD/service/tests:/app/tests:ro" \
  -e ACM_TEST_BASE_URL=http://acm-service:8927 \
  acm-service pytest tests/test_lifecycle.py -q

# Extension tests; second command invokes local ACM for real deadline proof.
(cd extension && bun test extension.test.ts)
(cd extension && ACM_LIVE_TEST=1 bun test extension.test.ts)
```

Install extension:

```bash
ln -s /Users/tri.nguyen/Projekti/petty/acm-worktrees/feat-acm-omp-extension/extension \
  ~/.omp/agent/extensions/acm
```

`/acm status` reports active mode, health, and stats. `/acm selfcheck` uses its own `project:acm-selfcheck-<session>` scope and `acm-selfcheck-<session>` bundle key, then prints endpoint pass/fail rows. `/acm inject on|off` toggles bundle injection for current runtime. `/acm last-compaction` shows latest validated hook result.

Disable extension through OMP settings:

```json
{ "disabledExtensions": ["extension-module:acm"] }
```

## Mode presets

Select a preset at OMP startup with `omp --acm-mode=<mode>` or `ACM_MODE=<mode> omp`. `ACM_MODE` takes precedence over `--acm-mode`; absent config selects `full`.

| Preset | Agent-end ingest | Anticipation and automatic bundle injection | Automatic compaction hook |
|---|---:|---:|---:|
| `full` | On | On | On |
| `memory` | On | On | Off |
| `compaction` | Off | Off | On |

`ACM_AUTO_INJECT=0` and `ACM_AUTO_ARM` retain their existing granular behavior inside applicable presets. `acm_compact` remains available in every preset.

OMP extensions cannot read arbitrary `config.yml` keys; use flag or env until OMP core exposes extension settings. `compaction` leaves memory ownership to Hindsight/Mnemopi; no Hindsight-to-ACM bridge exists.

## OMP today vs the paper (evidence-verified)

| Primitive | OMP today | ACM delta |
|---|---|---|
| Architecting | Static prompt packages in `packages/coding-agent/src/prompts/memories/` plus fixed Mnemopi schema: partial, hand-defined coverage (paper §7, T4 function a). | `acm_architect` synthesizes a per-agent architecture that governs extraction and compaction. |
| Ingesting | Mnemopi LLM fact extraction, `proactiveLinking` episodic graph, local embeddings, FTS, working-memory TTL, sleep/consolidation: largely present. [Source: `omp://mnemosyne-memory-backend.md`] | Architecture-driven extraction; entity-resolution cascade with aliases; temporal validity; async job IDs; write-side sanitization. |
| Scoping | Global, per-project, and per-project-tagged banks: two-level personal scope. [Source: `omp://mnemosyne-memory-backend.md`] | Provenance-tagged, token-budgeted assembly. |
| Anticipating | `compaction.asyncEnabled` speculates only about compaction; Mnemopi `enhancedRecall` is a query cache, which paper §4 explicitly excludes. Absent. [Source: `omp://mnemosyne-memory-backend.md`] | Trajectory-predicted precomputed session bundle; `context` is pure cache read; `acm_fetch` is explicit miss fallback; hit-rate stats. |
| Compacting & consolidation | `remote`/`snapcompact`/`handoff`/`shake`/`soft` method order, pruning, and useless-elision: strongest-in-class machinery, but no validation contract. [Source: `omp://compaction.md`] | Probe/reference-pair recoverability validation, equivalence judging, deterministic verbatim/file checks, score-0 fall-through, retry ladder with real budget growth. Hook is exact-digest cache read under OMP's 30-second cap. Automatic validated compaction ships off until live OMP proves an auto-threshold compaction matches an armed digest; `acm_compact` remains guaranteed path. |

## Current gaps

| Area | Current behavior | Upgrade trigger |
|---|---|---|
| Trust boundary | Sanitizes persisted/served memory; service is local-only, no auth. | Expose beyond loopback -> add authenticated API boundary and ACLs. |
| Anticipation | Best-effort async cache; a missing/expired bundle injects nothing. | Need guaranteed recall -> use explicit `acm_fetch`. |
| Compaction | Automatic validated compaction is off (`ACM_AUTO_ARM=0`): public `turn_end` lacks native preparation's dynamic cut point, turn prefix, and file ops. Hook only reads an exact canonical-digest match; any miss falls through native. | Live OMP proves an automatic threshold compaction matches an armed digest -> enable arming; otherwise use explicit `acm_compact`. |
| Selfcheck | Intentionally writes only synthetic scope/bundle; may wait up to about one minute for async bundle. | Need non-mutating probe -> add service-owned health/readiness route. |

## Per-turn pattern

1. Use `acm_fetch` when cache misses or work needs durable facts.
2. Use `acm_ingest` only for durable facts, decisions, preferences, or episodes.
3. Let `turn_end` prefetch; do not block on anticipation.
4. Accept ACM compaction only when validation score is at least `0.80`.
