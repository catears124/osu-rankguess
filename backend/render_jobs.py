from __future__ import annotations

import threading
from typing import Any

from database import _connect, database_configured, ensure_schema

_lock = threading.Lock()
_memory_jobs: dict[str, dict[str, Any]] = {}
_table_ready = False


def _normalize_prefix(value: str) -> str:
    return "".join(character for character in str(value or "").lower() if character in "0123456789abcdef")[:12]


def _ensure_table() -> None:
    global _table_ready
    if _table_ready or not database_configured():
        return
    with _lock:
        if _table_ready:
            return
        ensure_schema()
        with _connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ordr_render_jobs (
                        replay_hash_prefix TEXT PRIMARY KEY,
                        render_id BIGINT NOT NULL,
                        player TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS ordr_render_jobs_render_id_idx
                    ON ordr_render_jobs (render_id)
                    """
                )
        _table_ready = True


def get_render_job(replay_hash_prefix: str) -> dict[str, Any] | None:
    prefix = _normalize_prefix(replay_hash_prefix)
    if not prefix:
        return None
    if not database_configured():
        return _memory_jobs.get(prefix)
    _ensure_table()
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT replay_hash_prefix, render_id, player, created_at, updated_at FROM ordr_render_jobs WHERE replay_hash_prefix = %s",
                (prefix,),
            )
            return cursor.fetchone()


def save_render_job(replay_hash_prefix: str, render_id: int, player: str | None = None) -> dict[str, Any]:
    prefix = _normalize_prefix(replay_hash_prefix)
    render_id = int(render_id)
    if not prefix or render_id <= 0:
        raise ValueError("valid replay hash prefix and render id are required")
    record = {"replay_hash_prefix": prefix, "render_id": render_id, "player": player}
    if not database_configured():
        _memory_jobs[prefix] = record
        return record
    _ensure_table()
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ordr_render_jobs (replay_hash_prefix, render_id, player)
                VALUES (%s, %s, %s)
                ON CONFLICT (replay_hash_prefix) DO UPDATE SET
                    render_id = EXCLUDED.render_id,
                    player = COALESCE(EXCLUDED.player, ordr_render_jobs.player),
                    updated_at = NOW()
                RETURNING replay_hash_prefix, render_id, player, created_at, updated_at
                """,
                (prefix, render_id, player),
            )
            return cursor.fetchone()


def delete_render_job(replay_hash_prefix: str) -> None:
    prefix = _normalize_prefix(replay_hash_prefix)
    if not prefix:
        return
    _memory_jobs.pop(prefix, None)
    if not database_configured():
        return
    _ensure_table()
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM ordr_render_jobs WHERE replay_hash_prefix = %s", (prefix,))
