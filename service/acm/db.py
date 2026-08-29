"""Postgres schema + connection helpers. All queries MUST carry a scope predicate."""
import os
import threading

import psycopg
from psycopg.rows import dict_row

PG_DSN = os.environ.get("ACM_PG_DSN", "postgresql://acm:acm@localhost:5433/acm")
EMBED_DIM = int(os.environ.get("ACM_EMBED_DIM", "1024"))

_DDL = f"""
CREATE TABLE IF NOT EXISTS scope (
    id        serial PRIMARY KEY,
    kind      text NOT NULL CHECK (kind IN ('project', 'user')),
    name      text NOT NULL,
    parent_id integer REFERENCES scope(id),
    UNIQUE (kind, name)
);

CREATE TABLE IF NOT EXISTS architecture (
    scope_id integer PRIMARY KEY REFERENCES scope(id) ON DELETE CASCADE,
    spec     jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS memory (
    id          serial PRIMARY KEY,
    scope_id    integer NOT NULL REFERENCES scope(id) ON DELETE CASCADE,
    kind        text NOT NULL,
    content     text NOT NULL,
    importance  real NOT NULL DEFAULT 0.5,
    valid_from  date,
    valid_until date,
    source_ref  text,
    embedding   vector({EMBED_DIM}),
    status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    last_accessed timestamptz NOT NULL DEFAULT now(),
    access_count integer NOT NULL DEFAULT 0,
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
);
CREATE INDEX IF NOT EXISTS memory_tsv_idx ON memory USING gin (tsv);
CREATE INDEX IF NOT EXISTS memory_scope_idx ON memory (scope_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS memory_hnsw_idx ON memory USING hnsw (embedding vector_cosine_ops);

ALTER TABLE memory ADD COLUMN IF NOT EXISTS pinned boolean NOT NULL DEFAULT false;
ALTER TABLE memory ADD COLUMN IF NOT EXISTS merged_into integer REFERENCES memory(id);
ALTER TABLE memory ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

CREATE TABLE IF NOT EXISTS usage_log (
    id         bigserial PRIMARY KEY,
    memory_id  bigint NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
    use_type   text NOT NULL CHECK (use_type IN ('fetch', 'bundle', 'compact')),
    session_id text,
    scope      text,
    ts         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS usage_log_memory_idx ON usage_log (memory_id);
CREATE INDEX IF NOT EXISTS usage_log_ts_idx ON usage_log (ts);

CREATE TABLE IF NOT EXISTS entity (
    id             serial PRIMARY KEY,
    scope_id       integer NOT NULL REFERENCES scope(id) ON DELETE CASCADE,
    canonical_name text NOT NULL,
    aliases        text[] NOT NULL DEFAULT '{{}}',
    embedding      vector({EMBED_DIM}),
    UNIQUE (scope_id, canonical_name)
);
CREATE INDEX IF NOT EXISTS entity_name_trgm_idx ON entity USING gin (canonical_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS entity_hnsw_idx ON entity USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS mention (
    memory_id integer NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
    entity_id integer NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    role      text,
    PRIMARY KEY (memory_id, entity_id)
);

CREATE TABLE IF NOT EXISTS edge (
    id          serial PRIMARY KEY,
    scope_id    integer NOT NULL REFERENCES scope(id) ON DELETE CASCADE,
    from_entity integer NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    to_entity   integer NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    relation    text NOT NULL,
    memory_id   integer REFERENCES memory(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS edge_from_idx ON edge (from_entity);
CREATE INDEX IF NOT EXISTS edge_to_idx ON edge (to_entity);

CREATE TABLE IF NOT EXISTS ingest_job (
    id         serial PRIMARY KEY,
    scope_id   integer NOT NULL REFERENCES scope(id) ON DELETE CASCADE,
    payload    jsonb NOT NULL,
    digest     text NOT NULL,
    status     text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'failed', 'skipped')),
    result     jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ingest_job_digest_idx ON ingest_job (scope_id, digest);

CREATE TABLE IF NOT EXISTS compaction (
    id                 serial PRIMARY KEY,
    scope_id           integer REFERENCES scope(id) ON DELETE CASCADE,
    summary            text NOT NULL DEFAULT '',
    validation_score   real NOT NULL,
    compression_ratio  real NOT NULL,
    digest             text,
    status             text NOT NULL DEFAULT 'done' CHECK (status IN ('in_progress', 'done', 'failed')),
    probes             jsonb NOT NULL DEFAULT '[]'::jsonb,
    from_extension     boolean NOT NULL DEFAULT false,
    created_at         timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE compaction ADD COLUMN IF NOT EXISTS digest text;
ALTER TABLE compaction ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'done';
ALTER TABLE compaction ADD COLUMN IF NOT EXISTS probes jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE compaction ADD COLUMN IF NOT EXISTS from_extension boolean NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS compaction_match_idx ON compaction (scope_id, digest, created_at DESC)
    WHERE status = 'done' AND validation_score >= 0.8;
CREATE UNIQUE INDEX IF NOT EXISTS compaction_in_progress_digest_idx ON compaction (digest)
    WHERE status = 'in_progress';
CREATE TABLE IF NOT EXISTS prefetch (
    id              serial PRIMARY KEY,
    scope_id        integer NOT NULL REFERENCES scope(id) ON DELETE CASCADE,
    query_digest    text NOT NULL,
    query_embedding vector({EMBED_DIM}) NOT NULL,
    items           jsonb NOT NULL,
    expires_at      timestamptz NOT NULL,
    hit             boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bundle (
    session_id        text PRIMARY KEY,
    scope_id          integer NOT NULL REFERENCES scope(id) ON DELETE CASCADE,
    rendered          text NOT NULL,
    predicted_intents jsonb NOT NULL,
    served_count      integer NOT NULL DEFAULT 0,
    expires_at        timestamptz NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE bundle ADD COLUMN IF NOT EXISTS memory_ids jsonb;
ALTER TABLE bundle ADD COLUMN IF NOT EXISTS memory_ids_version smallint;

CREATE TABLE IF NOT EXISTS stats (
    key   text PRIMARY KEY,
    value jsonb NOT NULL
);
"""

_seed_lock = threading.Lock()
_seeded = False
_SCHEMA_LOCK = 8_465_414



def connect():
    return psycopg.connect(PG_DSN, row_factory=dict_row, autocommit=True)


def ensure_schema() -> None:
    global _seeded
    with connect() as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_SCHEMA_LOCK,))
        try:
            conn.execute(_DDL)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_SCHEMA_LOCK,))
    with _seed_lock:
        if _seeded:
            return
        with connect() as conn:
            conn.execute(
                "INSERT INTO scope (kind, name, parent_id) VALUES ('user', 'default', NULL) "
                "ON CONFLICT (kind, name) DO NOTHING"
            )
        _seeded = True


def get_or_create_scope(kind: str, name: str) -> int:
    """project:<name> scopes are children of user:default."""
    with connect() as conn:
        row = conn.execute(
            "INSERT INTO scope (kind, name, parent_id) "
            "VALUES (%(kind)s, %(name)s, (SELECT id FROM scope WHERE kind='user' AND name='default')) "
            "ON CONFLICT (kind, name) DO UPDATE SET name = EXCLUDED.name "
            "RETURNING id",
            {"kind": kind, "name": name},
        ).fetchone()
        return row["id"]


def scope_chain(scope_id: int) -> list[int]:
    """Narrowest-first chain: given scope up to its ancestors (project -> user)."""
    chain: list[int] = []
    cur = scope_id
    with connect() as conn:
        while cur is not None:
            row = conn.execute(
                "SELECT id, parent_id FROM scope WHERE id = %s", (cur,)
            ).fetchone()
            if row is None:
                break
            chain.append(row["id"])
            cur = row["parent_id"]
    return chain


def bump_stat(key: str, amount: int = 1) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO stats (key, value) VALUES (%(k)s, to_jsonb(%(v)s::integer)) "
            "ON CONFLICT (key) DO UPDATE SET value = "
            "to_jsonb(((stats.value #>> '{}')::integer + %(v)s::integer))",
            {"k": key, "v": amount},
        )
