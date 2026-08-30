"""Best-effort memory usage accounting."""
import logging
from collections.abc import Iterable

from . import db

_LOG = logging.getLogger(__name__)


def _record_usage(conn, memory_ids: Iterable[int], use_type: str, session_id: str | None, scope: str | None) -> None:
    ids = sorted({memory_id for memory_id in memory_ids if isinstance(memory_id, int)})
    if not ids:
        return
    conn.execute(
        "UPDATE memory SET access_count = access_count + 1, last_accessed = now() WHERE id = ANY(%s)",
        (ids,),
    )
    conn.execute(
        "INSERT INTO usage_log (memory_id, use_type, session_id, scope) "
        "SELECT selected.memory_id, %s, %s, %s FROM unnest(%s::bigint[]) AS selected(memory_id)",
        (use_type, session_id, scope, ids),
    )


def log_usage(memory_ids: Iterable[int], use_type: str, session_id: str | None = None, scope: str | None = None) -> None:
    """Record served memory usage without turning an accounting failure into a serve failure."""
    try:
        with db.connect() as conn, conn.transaction():
            _record_usage(conn, memory_ids, use_type, session_id, scope)
    except Exception:
        _LOG.warning("ACM usage log failed", exc_info=True)


def log_bundle_event(session_id: str | None, scope: str | None, outcome: str) -> None:
    """Record a bundle serve outcome (hit/miss) partitionable by session and scope."""
    try:
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO bundle_event (session_id, scope, outcome) VALUES (%s, %s, %s)",
                (session_id, scope, outcome),
            )
    except Exception:
        _LOG.warning("ACM bundle event log failed", exc_info=True)


def last_event_scope(session_id: str | None) -> str | None:
    """Best-effort scope for a bundle read with no live row (expired/invalidated)."""
    if not session_id:
        return None
    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT scope FROM bundle_event WHERE session_id = %s AND scope IS NOT NULL ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return row["scope"] if row else None
    except Exception:
        return None


def log_compaction_usage(session_id: str | None, scope: str | None) -> None:
    """Attribute a completed compaction to memories served since this session's prior compact."""
    if not session_id or not scope:
        return
    try:
        with db.connect() as conn, conn.transaction():
            checkpoint = conn.execute(
                "SELECT COALESCE(max(id), 0) AS id FROM usage_log "
                "WHERE session_id = %s AND scope = %s AND use_type = 'compact'",
                (session_id, scope),
            ).fetchone()["id"]
            rows = conn.execute(
                "SELECT DISTINCT memory_id FROM usage_log WHERE session_id = %s AND scope = %s "
                "AND use_type IN ('fetch', 'bundle') AND id > %s",
                (session_id, scope, checkpoint),
            ).fetchall()
            _record_usage(conn, (row["memory_id"] for row in rows), "compact", session_id, scope)
    except Exception:
        _LOG.warning("ACM compact usage log failed", exc_info=True)
