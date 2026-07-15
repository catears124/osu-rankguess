"""Normalize the public community-distribution payload for display."""
from __future__ import annotations

from typing import Any

import database as _database

_INSTALLED = False
_ORIGINAL_DISTRIBUTION = _database.challenge_guess_distribution


def _display_distribution(
    *,
    replay_id: str,
    mode: str,
    challenge_key: str,
    rank_population: int,
    bin_count: int = 12,
) -> dict[str, Any]:
    result = dict(
        _ORIGINAL_DISTRIBUTION(
            replay_id=replay_id,
            mode=mode,
            challenge_key=challenge_key,
            rank_population=rank_population,
            bin_count=bin_count,
        )
    )
    bins = []
    for item in result.get("bins") or []:
        if not isinstance(item, dict):
            continue
        bins.append(
            {
                "lower": item.get("lower"),
                "upper": item.get("upper"),
                "count": max(0, int(item.get("count") or 0)),
            }
        )

    result["count"] = sum(item["count"] for item in bins)
    result["bins"] = bins
    for key in (
        "observedCount",
        "baselineCount",
        "baselineTarget",
        "smoothed",
        "rawBins",
    ):
        result.pop(key, None)
    return result


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _database.challenge_guess_distribution = _display_distribution
    _INSTALLED = True
