from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import date
from typing import Any

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # Local tooling may inspect the project before dependencies are installed.
    psycopg = None
    dict_row = None


_DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
_schema_ready = False
_schema_lock = threading.Lock()


def database_configured() -> bool:
    return bool(_DATABASE_URL)


def _connect():
    if not _DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    if psycopg is None:
        raise RuntimeError("psycopg is not installed")
    return psycopg.connect(
        _DATABASE_URL,
        autocommit=True,
        row_factory=dict_row,
        connect_timeout=10,
    )


def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready or not database_configured():
        return

    with _schema_lock:
        if _schema_ready:
            return
        with _connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS replay_submissions (
                        id BIGSERIAL PRIMARY KEY,
                        public_id TEXT NOT NULL UNIQUE,
                        replay_hash TEXT NOT NULL UNIQUE,
                        render_id BIGINT UNIQUE,
                        player TEXT NOT NULL,
                        osu_user_id BIGINT,
                        avatar_url TEXT,
                        country_code TEXT,
                        actual_rank INTEGER,
                        predicted_rank INTEGER NOT NULL,
                        skill DOUBLE PRECISION NOT NULL,
                        top_percent DOUBLE PRECISION NOT NULL,
                        confidence TEXT,
                        star DOUBLE PRECISION NOT NULL,
                        accuracy_percent DOUBLE PRECISION NOT NULL,
                        mods TEXT NOT NULL,
                        artist TEXT,
                        title TEXT,
                        version TEXT,
                        creator TEXT,
                        length_seconds DOUBLE PRECISION,
                        map_id BIGINT,
                        map_link TEXT,
                        video_url TEXT NOT NULL,
                        published BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS replay_submissions_gallery_idx
                    ON replay_submissions (published, created_at DESC)
                    """
                )
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS replay_submissions_challenge_idx
                    ON replay_submissions (published, actual_rank, created_at DESC)
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS daily_challenges (
                        challenge_date DATE PRIMARY KEY,
                        replay_ids JSONB NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
        _schema_ready = True


def make_public_id(replay_hash: str) -> str:
    salt = os.getenv("GALLERY_ID_SALT") or os.getenv("CACHE_SIGNING_SECRET") or "osu-rankguess"
    return hashlib.sha256(f"{salt}:{replay_hash}".encode("utf-8")).hexdigest()[:24]


def save_submission(record: dict[str, Any]) -> dict[str, Any] | None:
    if not database_configured():
        return None
    ensure_schema()

    columns = [
        "public_id",
        "replay_hash",
        "render_id",
        "player",
        "osu_user_id",
        "avatar_url",
        "country_code",
        "actual_rank",
        "predicted_rank",
        "skill",
        "top_percent",
        "confidence",
        "star",
        "accuracy_percent",
        "mods",
        "artist",
        "title",
        "version",
        "creator",
        "length_seconds",
        "map_id",
        "map_link",
        "video_url",
        "published",
    ]
    values = [record.get(column) for column in columns]
    placeholders = ", ".join(["%s"] * len(columns))
    updates = ", ".join(
        f"{column} = EXCLUDED.{column}"
        for column in columns
        if column not in {"public_id", "replay_hash"}
    )

    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO replay_submissions ({', '.join(columns)})
                VALUES ({placeholders})
                ON CONFLICT (replay_hash) DO UPDATE SET
                    {updates},
                    updated_at = NOW()
                RETURNING *
                """,
                values,
            )
            return cursor.fetchone()


def list_gallery(limit: int = 24, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    if not database_configured():
        return [], 0
    ensure_schema()
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS count FROM replay_submissions WHERE published = TRUE"
            )
            count_row = cursor.fetchone() or {"count": 0}
            cursor.execute(
                """
                SELECT *
                FROM replay_submissions
                WHERE published = TRUE
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            return list(cursor.fetchall()), int(count_row["count"])


def get_submission(public_id: str) -> dict[str, Any] | None:
    if not database_configured():
        return None
    ensure_schema()
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM replay_submissions
                WHERE public_id = %s AND published = TRUE
                """,
                (public_id,),
            )
            return cursor.fetchone()


def random_challenge_submission(exclude_public_id: str | None = None) -> dict[str, Any] | None:
    if not database_configured():
        return None
    ensure_schema()
    with _connect() as connection:
        with connection.cursor() as cursor:
            if exclude_public_id:
                cursor.execute(
                    """
                    SELECT * FROM replay_submissions
                    WHERE published = TRUE
                      AND actual_rank IS NOT NULL
                      AND actual_rank > 0
                      AND public_id <> %s
                    ORDER BY RANDOM()
                    LIMIT 1
                    """,
                    (exclude_public_id,),
                )
            else:
                cursor.execute(
                    """
                    SELECT * FROM replay_submissions
                    WHERE published = TRUE
                      AND actual_rank IS NOT NULL
                      AND actual_rank > 0
                    ORDER BY RANDOM()
                    LIMIT 1
                    """
                )
            return cursor.fetchone()


def _daily_ids_for_date(challenge_date: date, count: int) -> list[str]:
    salt = os.getenv("DAILY_CHALLENGE_SALT") or os.getenv("CACHE_SIGNING_SECRET") or "osu-rankguess-daily"
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT replay_ids FROM daily_challenges WHERE challenge_date = %s",
                (challenge_date,),
            )
            existing = cursor.fetchone()
            if existing:
                replay_ids = existing["replay_ids"]
                return list(replay_ids if isinstance(replay_ids, list) else json.loads(replay_ids))

            cursor.execute(
                """
                SELECT public_id
                FROM replay_submissions
                WHERE published = TRUE
                  AND actual_rank IS NOT NULL
                  AND actual_rank > 0
                ORDER BY created_at DESC
                LIMIT 2000
                """
            )
            candidates = [row["public_id"] for row in cursor.fetchall()]
            if len(candidates) < count:
                return []

            selected = sorted(
                candidates,
                key=lambda public_id: hashlib.sha256(
                    f"{salt}:{challenge_date.isoformat()}:{public_id}".encode("utf-8")
                ).digest(),
            )[:count]

            cursor.execute(
                """
                INSERT INTO daily_challenges (challenge_date, replay_ids)
                VALUES (%s, %s::jsonb)
                ON CONFLICT (challenge_date) DO NOTHING
                """,
                (challenge_date, json.dumps(selected)),
            )
            cursor.execute(
                "SELECT replay_ids FROM daily_challenges WHERE challenge_date = %s",
                (challenge_date,),
            )
            row = cursor.fetchone()
            replay_ids = row["replay_ids"] if row else selected
            return list(replay_ids if isinstance(replay_ids, list) else json.loads(replay_ids))


def get_daily_challenge(challenge_date: date, count: int = 3) -> list[dict[str, Any]]:
    if not database_configured():
        return []
    ensure_schema()
    replay_ids = _daily_ids_for_date(challenge_date, count)
    if len(replay_ids) < count:
        return []

    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM replay_submissions WHERE public_id = ANY(%s)",
                (replay_ids,),
            )
            rows = {row["public_id"]: row for row in cursor.fetchall()}
    return [rows[public_id] for public_id in replay_ids if public_id in rows]


def challenge_count() -> int:
    if not database_configured():
        return 0
    ensure_schema()
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS count
                FROM replay_submissions
                WHERE published = TRUE
                  AND actual_rank IS NOT NULL
                  AND actual_rank > 0
                """
            )
            row = cursor.fetchone() or {"count": 0}
            return int(row["count"])
