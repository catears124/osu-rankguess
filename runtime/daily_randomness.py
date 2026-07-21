from __future__ import annotations

import hashlib
import itertools
import math
from datetime import date
from typing import Any

from runtime import daily_diversity as _daily_diversity
from runtime import daily_fresh as _daily_fresh

_INSTALLED = False

# Weighted candidate bands. Elite play is deliberately rare because the model is
# least calibrated in the extreme tail; it remains possible without dominating
# the daily experience.
_WINDOWS = (
    {
        "name": "elite",
        "rank_min": 1,
        "rank_max": 2_500,
        "weight": 2,
        "source": "global",
        "page_min": 1,
        "page_max": 55,
        "page_attempts": 10,
    },
    {
        "name": "high",
        "rank_min": 2_501,
        "rank_max": 7_500,
        "weight": 15,
        "source": "global",
        "page_min": 51,
        "page_max": 160,
        "page_attempts": 10,
    },
    {
        "name": "strong",
        "rank_min": 7_501,
        "rank_max": 20_000,
        "weight": 30,
        "source": "global",
        "page_min": 151,
        "page_max": 420,
        "page_attempts": 10,
    },
    {
        "name": "mid",
        "rank_min": 20_001,
        "rank_max": 75_000,
        "weight": 25,
        "source": "country",
        "page_min": 1,
        "page_max": 45,
        "page_attempts": 16,
    },
    {
        "name": "lower",
        "rank_min": 75_001,
        "rank_max": 200_000,
        "weight": 17,
        "source": "country-deep",
        "page_min": 1,
        "page_max": 50,
        "page_attempts": 16,
    },
    {
        "name": "deep",
        "rank_min": 200_001,
        "rank_max": 500_000,
        "weight": 8,
        "source": "country-deep",
        "page_min": 1,
        "page_max": 55,
        "page_attempts": 18,
    },
)


def _valid_plans() -> tuple[tuple[int, int, int], ...]:
    plans: list[tuple[int, int, int]] = []
    for plan in itertools.combinations(range(len(_WINDOWS)), 3):
        # Preserve real range without forcing a fixed daily composition: at least
        # one sub-20k target, one 20k+ target, and three buckets spanning 3+ bands.
        if min(plan) > 2 or max(plan) < 3:
            continue
        if max(plan) - min(plan) < 3:
            continue
        plans.append(plan)
    return tuple(plans)


_PLANS = _valid_plans()


def _plan_weight(plan: tuple[int, int, int]) -> int:
    return math.prod(int(_WINDOWS[index]["weight"]) for index in plan)


def _daily_plan(challenge_date: date) -> tuple[dict[str, Any], ...]:
    weighted = tuple((plan, _plan_weight(plan)) for plan in _PLANS)
    total = sum(weight for _, weight in weighted)
    digest = hashlib.sha256(
        f"daily-rank-plan:v2:{challenge_date.isoformat()}".encode("utf-8")
    ).digest()
    ticket = int.from_bytes(digest[:8], "big") % total

    selected = weighted[-1][0]
    cursor = 0
    for plan, weight in weighted:
        cursor += weight
        if ticket < cursor:
            selected = plan
            break

    return tuple(dict(_WINDOWS[index]) for index in selected)


def _rank_window(challenge_date: date, band: int) -> tuple[str, int, int]:
    plan = _daily_plan(challenge_date)
    window = plan[band % len(plan)]
    return (
        str(window["name"]),
        int(window["rank_min"]),
        int(window["rank_max"]),
    )


def _window_config(challenge_date: date, band: int) -> dict[str, Any]:
    plan = _daily_plan(challenge_date)
    return dict(plan[band % len(plan)])


async def _find_daily_candidate(
    app_module: Any,
    band: int,
    challenge_date: date,
    selection_key: str,
) -> dict[str, Any]:
    window = _window_config(challenge_date, band)
    rank_min = int(window["rank_min"])
    rank_max = int(window["rank_max"])
    source = str(window["source"])

    if source == "global":
        strategies = (
            {
                "rank_min": rank_min,
                "rank_max": rank_max,
                "page_min": int(window["page_min"]),
                "page_max": int(window["page_max"]),
                "page_attempts": int(window["page_attempts"]),
            },
        )
    else:
        countries = (
            _daily_diversity._DEEP_COUNTRIES  # noqa: SLF001
            if source == "country-deep"
            else _daily_diversity._COUNTRIES  # noqa: SLF001
        )
        strategies = (
            {
                "rank_min": rank_min,
                "rank_max": rank_max,
                "page_min": int(window["page_min"]),
                "page_max": int(window["page_max"]),
                "countries": countries,
                "page_attempts": int(window["page_attempts"]),
            },
            {
                "rank_min": max(20_001, rank_min // 2),
                "rank_max": min(500_000, rank_max * 2),
                "page_min": 1,
                "page_max": 60,
                "countries": _daily_diversity._COUNTRIES,  # noqa: SLF001
                "page_attempts": 14,
            },
        )

    failures: list[str] = []
    for strategy_index, strategy in enumerate(strategies):
        try:
            candidate = await _daily_diversity._candidate_from_rankings(  # noqa: SLF001
                app_module,
                f"{selection_key}:random-strategy-{strategy_index}",
                **strategy,
            )
            candidate["user"].update(
                {
                    "dailyRankBand": window["name"],
                    "dailyTargetRankMin": rank_min,
                    "dailyTargetRankMax": rank_max,
                    "dailyRankingSource": candidate.get("rankingSource"),
                }
            )
            return candidate
        except Exception as exc:
            failures.append(str(exc))

    # Availability remains more important than perfect stratification. The old
    # sampler is retained only as a final fallback after targeted searches fail.
    fallback = await app_module.find_seed_candidate(
        band,
        challenge_date,
        selection_key=f"{selection_key}:random-availability-fallback",
    )
    fallback["user"].update(
        {
            "dailyRankBand": f"{window['name']}-fallback",
            "dailyTargetRankMin": rank_min,
            "dailyTargetRankMax": rank_max,
            "dailySelectionFailures": failures[-3:],
            "dailyRankingSource": "global:fallback",
        }
    )
    fallback["rankingSource"] = "global:fallback"
    return fallback


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _daily_fresh._find_daily_candidate = _find_daily_candidate  # noqa: SLF001
    _INSTALLED = True
