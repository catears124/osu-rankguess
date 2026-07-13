"""Add turn-by-turn rankbot behavior without changing the FastAPI app module."""
from __future__ import annotations

import asyncio
import functools
import json
import math
import os

def _adaptive_feedback(actual_rank: int, guess_rank: int) -> tuple[bool, str, str, float]:
    actual_rank = max(1, int(actual_rank))
    guess_rank = max(1, int(guess_rank))
    log_error = abs(math.log10(guess_rank / actual_rank))
    allowance = 0.022 + 0.075 / math.sqrt(1.0 + actual_rank / 1000.0)
    correct = abs(guess_rank - actual_rank) <= 100 or log_error <= allowance
    direction = "correct" if correct else ("better" if actual_rank < guess_rank else "worse")
    if correct:
        closeness = "exact"
    elif log_error <= allowance * 1.8:
        closeness = "very_close"
    elif log_error <= allowance * 3.5:
        closeness = "close"
    else:
        closeness = "far"
    return correct, direction, closeness, log_error


def _rankbot_turn(predicted_rank: int, actual_rank: int, attempt: int, population: int) -> dict:
    predicted_rank = min(population, max(1, int(predicted_rank)))
    actual_rank = min(population, max(1, int(actual_rank)))
    lower, upper = 1, population
    history: list[dict] = []

    for turn in range(1, max(1, int(attempt)) + 1):
        if turn == 1:
            guess = predicted_rank
        else:
            prior = min(upper, max(lower, predicted_rank))
            center = math.exp((math.log(max(1, lower)) + math.log(max(1, upper))) / 2.0)
            prior_weight = max(0.12, 0.52 - 0.11 * (turn - 2))
            guess = int(round(math.exp(
                prior_weight * math.log(max(1, prior))
                + (1.0 - prior_weight) * math.log(max(1.0, center))
            )))
            guess = min(upper, max(lower, guess))
            if history and guess == history[-1]["guess"] and lower < upper:
                guess = min(upper, max(lower, int(round(center))))

        correct, direction, closeness, log_error = _adaptive_feedback(actual_rank, guess)
        current = {
            "guess": guess,
            "correct": correct,
            "direction": direction,
            "closeness": closeness,
            "logError": log_error,
        }
        history.append(current)

        if correct:
            break
        if direction == "better":
            upper = min(upper, max(1, guess - 1))
        elif direction == "worse":
            lower = max(lower, min(population, guess + 1))
        if lower > upper:
            lower = upper = actual_rank

    return history[-1]


try:
    from fastapi import FastAPI as _FastAPI
    from fastapi.responses import JSONResponse as _JSONResponse
except Exception:  # pragma: no cover
    _FastAPI = None

if _FastAPI is not None and not getattr(_FastAPI, "_rankguess_duel_patch", False):
    _original_post = _FastAPI.post

    def _patched_post(self, path: str, *args, **kwargs):
        registrar = _original_post(self, path, *args, **kwargs)

        def decorate(endpoint):
            if path != "/api/challenge/guess":
                return registrar(endpoint)

            @functools.wraps(endpoint)
            async def wrapped(*endpoint_args, **endpoint_kwargs):
                response = await endpoint(*endpoint_args, **endpoint_kwargs)
                payload = endpoint_kwargs.get("payload")
                if payload is None and endpoint_args:
                    payload = endpoint_args[0]
                if payload is None or not hasattr(response, "body"):
                    return response

                try:
                    body = json.loads(bytes(response.body).decode("utf-8"))
                    from database import (
                        challenge_guess_distribution,
                        get_challenge_submission,
                    )
                    row = await asyncio.to_thread(get_challenge_submission, payload.replay_id)
                    if not row or not row.get("actual_rank") or not row.get("predicted_rank"):
                        return response

                    population = max(1, int(os.getenv("OSU_RANK_POPULATION", "5500000") or 5500000))
                    actual_rank = int(row["actual_rank"])
                    predicted_rank = int(row["predicted_rank"])
                    player_correct, player_direction, player_closeness, player_log_error = _adaptive_feedback(
                        actual_rank,
                        int(payload.guess_rank),
                    )
                    bot = _rankbot_turn(
                        predicted_rank,
                        actual_rank,
                        int(payload.attempt),
                        population,
                    )

                    reveal = player_correct or bool(bot["correct"]) or int(payload.attempt) >= 5
                    body.update({
                        "correct": player_correct,
                        "direction": player_direction,
                        "closeness": player_closeness,
                        "logError": player_log_error,
                        "botGuess": int(bot["guess"]),
                        "botCorrect": bool(bot["correct"]),
                        "botDirection": bot["direction"],
                        "botCloseness": bot["closeness"],
                        "botLogError": float(bot["logError"]),
                        "revealed": reveal,
                    })

                    if player_correct and bot["correct"]:
                        difference = player_log_error - float(bot["logError"])
                        body["turnWinner"] = "tie" if abs(difference) < 1e-9 else ("player" if difference < 0 else "bot")
                    elif player_correct:
                        body["turnWinner"] = "player"
                    elif bot["correct"]:
                        body["turnWinner"] = "bot"
                    else:
                        body["turnWinner"] = "pending"

                    if reveal:
                        mode = str(payload.mode).strip().lower()
                        challenge_key = (
                            payload.challenge_date.isoformat()
                            if mode == "daily" and payload.challenge_date is not None
                            else payload.replay_id
                        )
                        distribution = body.get("distribution")
                        if distribution is None:
                            try:
                                distribution = await asyncio.to_thread(
                                    challenge_guess_distribution,
                                    replay_id=payload.replay_id,
                                    mode=mode,
                                    challenge_key=challenge_key,
                                    rank_population=population,
                                )
                            except Exception:
                                distribution = None
                        body.update({
                            "actualRank": actual_rank,
                            "predictedRank": predicted_rank,
                            "player": row.get("player"),
                            "avatarURL": row.get("avatar_url"),
                            "distribution": distribution,
                        })
                    else:
                        for key in ("actualRank", "predictedRank", "player", "avatarURL", "distribution"):
                            body.pop(key, None)

                    return _JSONResponse(body, status_code=getattr(response, "status_code", 200))
                except Exception as exc:
                    print(json.dumps({"event": "rankbot_patch_failed", "error": repr(exc)}), flush=True)
                    return response

            return registrar(wrapped)

        return decorate

    _FastAPI.post = _patched_post
    _FastAPI._rankguess_duel_patch = True
