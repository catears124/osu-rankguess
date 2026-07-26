from __future__ import annotations

import math
from typing import Any

import database as _database
from runtime import community as _community

_INSTALLED = False

# These boundaries mirror public/daily-review.js exactly. Keeping the server and
# client on the same buckets prevents an observed guess from being reassigned to
# a neighboring bar when the browser normalizes the distribution.
_BUCKETS = (
    (1, 99),
    (100, 315),
    (316, 999),
    (1_000, 3_161),
    (3_162, 9_999),
    (10_000, 31_622),
    (31_623, 99_999),
    (100_000, 223_606),
    (223_607, 499_999),
    (500_000, 707_106),
    (707_107, 999_999),
    (1_000_000, 2_345_207),
    (2_345_208, 5_500_000),
)


def _community_buckets(rank_population: int) -> list[tuple[int, int]]:
    maximum = max(1, int(rank_population))
    buckets: list[tuple[int, int]] = []
    for lower, upper in _BUCKETS:
        if lower > maximum:
            break
        buckets.append((lower, min(upper, maximum)))
    if not buckets:
        return [(1, maximum)]
    if buckets[-1][1] < maximum:
        buckets[-1] = (buckets[-1][0], maximum)
    return buckets


def _bucket_index(rank: int, buckets: list[tuple[int, int]]) -> int:
    clipped = max(1, int(rank))
    for index, (_lower, upper) in enumerate(buckets):
        if clipped <= upper:
            return index
    return len(buckets) - 1


def _observed_guesses(*, replay_id: str, mode: str, challenge_key: str) -> list[int]:
    _database.ensure_schema()
    with _database._connect() as connection:  # noqa: SLF001
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT guess_rank
                FROM challenge_guesses
                WHERE replay_id = %s AND mode = %s AND challenge_key = %s
                ORDER BY guess_rank
                """,
                (replay_id, mode, challenge_key),
            )
            return [int(row["guess_rank"]) for row in cursor.fetchall()]


def _quantile(values: list[int], q: float) -> int | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    position = q * (len(values) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return values[lower]
    weight = position - lower
    return int(round(values[lower] * (1.0 - weight) + values[upper] * weight))


def _live_distribution(
    *,
    replay_id: str,
    mode: str,
    challenge_key: str,
    rank_population: int,
    bin_count: int = 12,
) -> dict[str, Any]:
    del bin_count  # The browser contract uses the fixed buckets above.

    if not _database.database_configured():
        return {"count": 0, "observedCount": 0, "bins": []}

    maximum = max(1, int(rank_population))
    buckets = _community_buckets(maximum)
    guesses = _observed_guesses(
        replay_id=replay_id,
        mode=mode,
        challenge_key=challenge_key,
    )

    observed_bins = [0] * len(buckets)
    for guess in guesses:
        observed_bins[_bucket_index(min(maximum, guess), buckets)] += 1

    observed_count = len(guesses)
    baseline_target = _community._baseline_target()  # noqa: SLF001
    baseline_count = max(0, baseline_target - observed_count)
    row = _database.get_challenge_submission(replay_id) or {}
    baseline_bins = [0] * len(buckets)
    for rank in _community._synthetic_baseline(  # noqa: SLF001
        replay_id=replay_id,
        challenge_key=challenge_key,
        rank_population=maximum,
        count=baseline_count,
        actual_rank=row.get("actual_rank"),
        predicted_rank=row.get("predicted_rank"),
    ):
        baseline_bins[_bucket_index(rank, buckets)] += 1

    result: dict[str, Any] = {
        "count": observed_count,
        "observedCount": observed_count,
        "baselineCount": baseline_count,
        "baselineTarget": baseline_target,
        "smoothed": baseline_count > 0,
        "bins": [
            {
                "lower": lower,
                "upper": upper,
                "count": observed_bins[index] + baseline_bins[index],
                "observedCount": observed_bins[index],
                "baselineCount": baseline_bins[index],
            }
            for index, (lower, upper) in enumerate(buckets)
        ],
        "rawBins": [
            {
                "lower": lower,
                "upper": upper,
                "count": observed_bins[index],
            }
            for index, (lower, upper) in enumerate(buckets)
        ],
    }

    if guesses:
        result.update(
            {
                "medianRank": _quantile(guesses, 0.5),
                "q25Rank": _quantile(guesses, 0.25),
                "q75Rank": _quantile(guesses, 0.75),
                "geometricMeanRank": int(
                    round(10 ** (sum(math.log10(max(1, value)) for value in guesses) / observed_count))
                ),
            }
        )
    return result


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _database.challenge_guess_distribution = _live_distribution
    _INSTALLED = True
