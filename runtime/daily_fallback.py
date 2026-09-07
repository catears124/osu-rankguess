from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from typing import Any

from backend import database as _database

_INSTALLED = False


def _daily_replay_ids(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [str(item) for item in parsed if item]
    return []


def _fallback_rows(
    challenge_date: date,
    *,
    excluded_ids: set[str],
    excluded_players: set[str],
    count: int,
) -> list[dict[str, Any]]:
    if count <= 0:
        return []

    with _database._connect() as connection:  # noqa: SLF001
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM replay_submissions
                WHERE published = TRUE
                  AND actual_rank IS NOT NULL
                  AND actual_rank > 0
                ORDER BY created_at DESC
                LIMIT 2000
                """
            )
            candidates = [
                row
                for row in cursor.fetchall()
                if str(row["public_id"]) not in excluded_ids
            ]

            cursor.execute(
                """
                SELECT challenge_date, replay_ids
                FROM daily_challenges
                WHERE challenge_date < %s
                ORDER BY challenge_date DESC
                LIMIT 365
                """,
                (challenge_date,),
            )
            history = list(cursor.fetchall())

    last_used: dict[str, date] = {}
    for history_row in history:
        used_date = history_row["challenge_date"]
        for replay_id in _daily_replay_ids(history_row["replay_ids"]):
            if replay_id not in last_used:
                last_used[replay_id] = used_date

    salt = (
        os.getenv("DAILY_CHALLENGE_SALT")
        or os.getenv("CACHE_SIGNING_SECRET")
        or "osu-rankguess-daily-fallback"
    )

    def selection_key(row: dict[str, Any]) -> tuple[int, date, bytes]:
        public_id = str(row["public_id"])
        used = last_used.get(public_id)
        digest = hashlib.sha256(
            f"{salt}:{challenge_date.isoformat()}:fallback:{public_id}".encode("utf-8")
        ).digest()
        return (1 if used else 0, used or date.min, digest)

    ordered = sorted(candidates, key=selection_key)
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    seen_players = set(excluded_players)

    # Prefer distinct players so a degraded Daily still feels like three rounds,
    # not multiple replays from one account.
    for row in ordered:
        player = str(row.get("player") or "").casefold()
        if player and player in seen_players:
            continue
        selected.append(row)
        selected_ids.add(str(row["public_id"]))
        if player:
            seen_players.add(player)
        if len(selected) >= count:
            return selected

    # Player diversity is a soft constraint; availability wins.
    for row in ordered:
        public_id = str(row["public_id"])
        if public_id in selected_ids:
            continue
        selected.append(row)
        if len(selected) >= count:
            break
    return selected


def get_daily_challenge(challenge_date: date, count: int = 3) -> list[dict[str, Any]]:
    """Return prepared Daily rows, filling temporary gaps from the public gallery.

    Fresh Daily generation remains authoritative. Gallery fallback rows are never
    written into ``daily_challenges``, so the background job continues preparing
    the missing fresh slots and automatically replaces the fallback on later
    requests once those slots are finalized.
    """
    if not _database.database_configured():
        return []

    target = max(0, int(count))
    if target == 0:
        return []

    _database.ensure_schema()
    prepared_ids = list(_database._daily_ids_for_date(challenge_date, target))  # noqa: SLF001

    prepared_rows: list[dict[str, Any]] = []
    if prepared_ids:
        with _database._connect() as connection:  # noqa: SLF001
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM replay_submissions WHERE public_id = ANY(%s)",
                    (prepared_ids,),
                )
                rows_by_id = {
                    str(row["public_id"]): row for row in cursor.fetchall()
                }
        prepared_rows = [
            rows_by_id[public_id]
            for public_id in prepared_ids
            if public_id in rows_by_id
        ][:target]

    missing = target - len(prepared_rows)
    if missing <= 0:
        return prepared_rows

    fallback_rows = _fallback_rows(
        challenge_date,
        excluded_ids={str(row["public_id"]) for row in prepared_rows},
        excluded_players={
            str(row.get("player") or "").casefold()
            for row in prepared_rows
            if row.get("player")
        },
        count=missing,
    )

    if fallback_rows:
        print(
            json.dumps(
                {
                    "event": "daily_gallery_fallback",
                    "date": challenge_date.isoformat(),
                    "preparedReplayCount": len(prepared_rows),
                    "fallbackReplayCount": len(fallback_rows),
                    "requestedReplayCount": target,
                },
                separators=(",", ":"),
            ),
            flush=True,
        )

    return prepared_rows + fallback_rows


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _database.get_daily_challenge = get_daily_challenge
    _INSTALLED = True
