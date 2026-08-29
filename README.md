# Agentic Context Management

Local ACM service plus Oh My Pi extension. OMP core remains untouched: `extension/index.ts` uses public extension hooks/tools only.

## Architecture

```text
OMP extension
  ├─ session_before_compact ──> POST /compact ──> validated summary
  ├─ agent_end ──────────────> POST /ingest ───> async extract/store
  ├─ turn_end ───────────────> POST /anticipate -> bundle cache
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

`/acm status` reports health/stats. `/acm selfcheck` uses its own `project:acm-selfcheck-<session>` scope and `acm-selfcheck-<session>` bundle key, then prints endpoint pass/fail rows. `/acm inject on|off` toggles bundle injection for current runtime. `/acm last-compaction` shows latest validated hook result.

Disable extension through OMP settings:

```json
{ "disabledExtensions": ["extension-module:acm"] }
```

## Current gaps

| Area | Current behavior | Upgrade trigger |
|---|---|---|
| Trust boundary | Sanitizes persisted/served memory; service is local-only, no auth. | Expose beyond loopback -> add authenticated API boundary and ACLs. |
| Anticipation | Best-effort async cache; a missing/expired bundle injects nothing. | Need guaranteed recall -> use explicit `acm_fetch`. |
| Compaction | Real hook proof must stay below OMP's 30-second generic handler cap. | Measured regression -> split compaction into host-safe phases; do not raise OMP core cap. |
| Selfcheck | Intentionally writes only synthetic scope/bundle; may wait up to about one minute for async bundle. | Need non-mutating probe -> add service-owned health/readiness route. |

## Per-turn pattern

1. Use `acm_fetch` when cache misses or work needs durable facts.
2. Use `acm_ingest` only for durable facts, decisions, preferences, or episodes.
3. Let `turn_end` prefetch; do not block on anticipation.
4. Accept ACM compaction only when validation score is at least `0.80`.
