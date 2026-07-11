from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
import threading
from datetime import date
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # Local tooling may inspect the project before dependencies are installed.
    psycopg = None
    dict_row = None


_RAW_DATABASE_URL = os.getenv("POSTGRES_URL") or os.getenv("DATABASE_URL")
_DATABASE_URL_SOURCE = (
    "POSTGRES_URL"
    if os.getenv("POSTGRES_URL")
    else "DATABASE_URL"
    if os.getenv("DATABASE_URL")
    else None
)

_ALLOWED_LIBPQ_QUERY_PARAMETERS = {
    "application_name",
    "channel_binding",
    "connect_timeout",
    "gssencmode",
    "keepalives",
    "keepalives_count",
    "keepalives_idle",
    "keepalives_interval",
    "load_balance_hosts",
    "options",
    "passfile",
    "requirepeer",
    "service",
    "servicefile",
    "sslcert",
    "sslcrl",
    "sslkey",
    "sslmode",
    "sslpassword",
    "sslrootcert",
    "target_session_attrs",
}


def _sanitize_database_url(value: str | None) -> tuple[str | None, tuple[str, ...]]:
    if not value:
        return None, ()

    parts = urlsplit(value.strip())
    kept: list[tuple[str, str]] = []
    removed: list[str] = []

    for key, parameter_value in parse_qsl(parts.query, keep_blank_values=True):
        if key in _ALLOWED_LIBPQ_QUERY_PARAMETERS:
            kept.append((key, parameter_value))
        else:
            removed.append(key)

    if parts.hostname and "supabase" in parts.hostname.lower():
        if not any(key == "sslmode" for key, _ in kept):
            kept.append(("sslmode", "require"))

    sanitized = urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(kept),
            parts.fragment,
        )
    )
    return sanitized, tuple(sorted(set(removed)))


_DATABASE_URL, _REMOVED_DATABASE_URL_PARAMETERS = _sanitize_database_url(
    _RAW_DATABASE_URL
)
_schema_ready = False
_schema_lock = threading.Lock()


def database_configured() -> bool:
    return bool(_DATABASE_URL)


def database_diagnostics() -> dict[str, Any]:
    return {
        "configured": database_configured(),
        "source": _DATABASE_URL_SOURCE,
        "removedQueryParameters": list(_REMOVED_DATABASE_URL_PARAMETERS),
    }


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
                        thumbnail_url TEXT,
                        source TEXT NOT NULL DEFAULT 'upload',
                        published BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cursor.execute(
                    "ALTER TABLE replay_submissions ADD COLUMN IF NOT EXISTS thumbnail_url TEXT"
                )
                cursor.execute(
                    "ALTER TABLE replay_submissions ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'upload'"
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
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS infinite_challenges (
                        challenge_id TEXT PRIMARY KEY,
                        replay_hash TEXT NOT NULL,
                        render_id BIGINT,
                        player TEXT NOT NULL,
                        actual_rank INTEGER NOT NULL,
                        predicted_rank INTEGER NOT NULL,
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
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '24 hours')
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS infinite_challenges_recent_idx
                    ON infinite_challenges (replay_hash, expires_at DESC)
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS challenge_guesses (
                        id BIGSERIAL PRIMARY KEY,
                        replay_id TEXT NOT NULL,
                        challenge_key TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        guess_rank INTEGER NOT NULL,
                        log_guess DOUBLE PRECISION NOT NULL,
                        attempt SMALLINT NOT NULL,
                        session_hash TEXT NOT NULL,
                        correct BOOLEAN NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (replay_id, challenge_key, mode, session_hash, attempt)
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS challenge_guesses_distribution_idx
                    ON challenge_guesses (replay_id, challenge_key, mode, session_hash, attempt)
                    """
                )
                cursor.execute(
                    "DELETE FROM infinite_challenges WHERE expires_at < NOW()"
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
        "thumbnail_url",
        "source",
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


def submission_exists(*, replay_hash: str | None = None, render_id: int | None = None) -> bool:
    if not database_configured():
        return False
    ensure_schema()
    if replay_hash is None and render_id is None:
        raise ValueError("replay_hash or render_id is required")

    conditions: list[str] = []
    values: list[Any] = []
    if replay_hash is not None:
        conditions.append("replay_hash = %s")
        values.append(replay_hash)
    if render_id is not None:
        conditions.append("render_id = %s")
        values.append(render_id)

    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT 1 FROM replay_submissions WHERE {' OR '.join(conditions)} LIMIT 1",
                values,
            )
            return cursor.fetchone() is not None


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


def update_submission_thumbnail(public_id: str, thumbnail_url: str) -> None:
    if not database_configured():
        return
    ensure_schema()
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE replay_submissions
                SET thumbnail_url = %s, updated_at = NOW()
                WHERE public_id = %s
                """,
                (thumbnail_url, public_id),
            )


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


def save_infinite_challenge(record: dict[str, Any]) -> dict[str, Any]:
    if not database_configured():
        raise RuntimeError("Database is required for infinite challenges")
    ensure_schema()
    columns = [
        "challenge_id",
        "replay_hash",
        "render_id",
        "player",
        "actual_rank",
        "predicted_rank",
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
    ]
    values = [record.get(column) for column in columns]
    placeholders = ", ".join(["%s"] * len(columns))
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM infinite_challenges WHERE expires_at < NOW()")
            cursor.execute(
                f"""
                INSERT INTO infinite_challenges ({', '.join(columns)})
                VALUES ({placeholders})
                ON CONFLICT (challenge_id) DO UPDATE SET
                    render_id = EXCLUDED.render_id,
                    player = EXCLUDED.player,
                    actual_rank = EXCLUDED.actual_rank,
                    predicted_rank = EXCLUDED.predicted_rank,
                    star = EXCLUDED.star,
                    accuracy_percent = EXCLUDED.accuracy_percent,
                    mods = EXCLUDED.mods,
                    artist = EXCLUDED.artist,
                    title = EXCLUDED.title,
                    version = EXCLUDED.version,
                    creator = EXCLUDED.creator,
                    length_seconds = EXCLUDED.length_seconds,
                    map_id = EXCLUDED.map_id,
                    map_link = EXCLUDED.map_link,
                    video_url = EXCLUDED.video_url,
                    expires_at = NOW() + INTERVAL '24 hours'
                RETURNING *
                """,
                values,
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("Could not save infinite challenge")
            return row


def get_infinite_challenge(challenge_id: str) -> dict[str, Any] | None:
    if not database_configured():
        return None
    ensure_schema()
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM infinite_challenges
                WHERE challenge_id = %s AND expires_at > NOW()
                """,
                (challenge_id,),
            )
            return cursor.fetchone()


def infinite_replay_recent(replay_hash: str) -> bool:
    if not database_configured():
        return False
    ensure_schema()
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1 FROM infinite_challenges
                WHERE replay_hash = %s AND expires_at > NOW()
                LIMIT 1
                """,
                (replay_hash,),
            )
            return cursor.fetchone() is not None


def save_challenge_guess(
    *,
    replay_id: str,
    challenge_key: str,
    mode: str,
    guess_rank: int,
    attempt: int,
    session_hash: str,
    correct: bool,
) -> None:
    if not database_configured():
        return
    ensure_schema()
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO challenge_guesses (
                    replay_id,
                    challenge_key,
                    mode,
                    guess_rank,
                    log_guess,
                    attempt,
                    session_hash,
                    correct
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (replay_id, challenge_key, mode, session_hash, attempt)
                DO UPDATE SET
                    guess_rank = EXCLUDED.guess_rank,
                    log_guess = EXCLUDED.log_guess,
                    correct = EXCLUDED.correct
                """,
                (
                    replay_id,
                    challenge_key,
                    mode,
                    int(guess_rank),
                    math.log10(max(1, int(guess_rank))),
                    int(attempt),
                    session_hash,
                    bool(correct),
                ),
            )


def challenge_guess_distribution(
    *,
    replay_id: str,
    challenge_key: str,
    mode: str,
    rank_population: int,
    bucket_count: int = 12,
) -> dict[str, Any]:
    if not database_configured():
        return {"count": 0, "medianRank": None, "buckets": []}
    ensure_schema()
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                WITH first_guesses AS (
                    SELECT DISTINCT ON (session_hash)
                        session_hash,
                        guess_rank,
                        attempt,
                        created_at
                    FROM challenge_guesses
                    WHERE replay_id = %s
                      AND challenge_key = %s
                      AND mode = %s
                    ORDER BY session_hash, attempt ASC, created_at ASC
                )
                SELECT guess_rank FROM first_guesses
                """,
                (replay_id, challenge_key, mode),
            )
            guesses = [int(row["guess_rank"]) for row in cursor.fetchall()]

    population = max(10, int(rank_population))
    maximum_log = math.log10(population)
    edges = [1]
    for index in range(1, bucket_count + 1):
        value = int(round(10 ** (maximum_log * index / bucket_count)))
        edges.append(max(edges[-1] + 1, min(population, value)))
    edges[-1] = population

    counts = [0 for _ in range(bucket_count)]
    for guess in guesses:
        clamped = min(population, max(1, guess))
        position = 0 if maximum_log <= 0 else int(
            min(bucket_count - 1, math.floor(math.log10(clamped) / maximum_log * bucket_count))
        )
        counts[position] += 1

    buckets = []
    for index, count in enumerate(counts):
        minimum = edges[index]
        maximum = edges[index + 1]
        buckets.append(
            {
                "minRank": minimum,
                "maxRank": maximum,
                "count": count,
            }
        )

    return {
        "count": len(guesses),
        "medianRank": int(round(statistics.median(guesses))) if guesses else None,
        "buckets": buckets,
    }
