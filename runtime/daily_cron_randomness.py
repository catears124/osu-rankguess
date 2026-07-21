from __future__ import annotations

import secrets
import threading
from datetime import datetime, timezone
from typing import Any

from runtime import community as _community
from runtime import cron as _cron
from runtime import daily_fresh as _daily_fresh

_INSTALLED = False
_LOCK = threading.Lock()
_MINIMUM_MINUTES = 20
_MAXIMUM_MINUTES = 120
_ORIGINAL_CLAIM_WINDOW = _cron._claim_window  # noqa: SLF001


def _daily_schedule_key(challenge_date) -> str:
    return f"daily-random:{challenge_date.isoformat()}"


def _claim_future_daily_window(job_key: str) -> dict[str, Any]:
    _community._ensure_scheduler_schema()  # noqa: SLF001
    first_delay = _MINIMUM_MINUTES + secrets.randbelow(
        _MAXIMUM_MINUTES - _MINIMUM_MINUTES + 1
    )
    next_delay = _MINIMUM_MINUTES + secrets.randbelow(
        _MAXIMUM_MINUTES - _MINIMUM_MINUTES + 1
    )

    with _LOCK:
        with _cron._database._connect() as connection:  # noqa: SLF001
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO background_job_schedule (job_key, next_run_at)
                    VALUES (%s, NOW() + (%s * INTERVAL '1 minute'))
                    ON CONFLICT (job_key) DO NOTHING
                    RETURNING next_run_at, last_run_at
                    """,
                    (job_key, first_delay),
                )
                inserted = cursor.fetchone()
                if inserted:
                    return {
                        "claimed": False,
                        "delayMinutes": first_delay,
                        "nextRunAt": inserted["next_run_at"],
                        "lastRunAt": inserted.get("last_run_at"),
                    }

                cursor.execute(
                    """
                    UPDATE background_job_schedule
                    SET last_run_at = NOW(),
                        next_run_at = NOW() + (%s * INTERVAL '1 minute'),
                        updated_at = NOW()
                    WHERE job_key = %s
                      AND next_run_at <= NOW()
                    RETURNING next_run_at, last_run_at
                    """,
                    (next_delay, job_key),
                )
                claimed = cursor.fetchone()
                if claimed:
                    return {
                        "claimed": True,
                        "delayMinutes": next_delay,
                        "nextRunAt": claimed["next_run_at"],
                        "lastRunAt": claimed["last_run_at"],
                    }

                cursor.execute(
                    """
                    SELECT next_run_at, last_run_at
                    FROM background_job_schedule
                    WHERE job_key = %s
                    """,
                    (job_key,),
                )
                current = cursor.fetchone() or {}
                return {
                    "claimed": False,
                    "delayMinutes": None,
                    "nextRunAt": current.get("next_run_at"),
                    "lastRunAt": current.get("last_run_at"),
                }


def _claim_window() -> dict[str, Any]:
    target = _daily_fresh._next_daily_target()  # noqa: SLF001
    if target is None:
        return _ORIGINAL_CLAIM_WINDOW()

    challenge_date, slot = target
    today = datetime.now(timezone.utc).date()
    if challenge_date <= today:
        return {
            "claimed": True,
            "delayMinutes": 0,
            "nextRunAt": None,
            "lastRunAt": None,
            "schedule": "same-day-recovery",
            "dailyTarget": challenge_date.isoformat(),
            "dailySlot": slot,
        }

    claim = _claim_future_daily_window(_daily_schedule_key(challenge_date))
    claim.update(
        {
            "schedule": "daily-random-20-120m",
            "dailyTarget": challenge_date.isoformat(),
            "dailySlot": slot,
        }
    )
    return claim


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _daily_fresh._claim_window = _claim_window  # noqa: SLF001
    _cron._claim_window = _claim_window  # noqa: SLF001
    _INSTALLED = True
