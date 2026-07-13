from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

app_path = ROOT / "app.py"
app = app_path.read_text(encoding="utf-8")

route_start = app.index('@app.post("/api/challenge/guess")')
route_end = app.index('\n\n@app.get("/api/challenge/{replay_id}/distribution")', route_start)

replacement = r'''def _soft_rank_position(rank: int, population: int = OSU_RANK_POPULATION) -> float:
    softness = 2_500.0
    maximum = max(2, int(population))
    clipped = max(1, min(maximum, int(rank)))
    scale = math.log1p((maximum - 1) / softness)
    return math.log1p((clipped - 1) / softness) / scale


def _soft_rank_from_position(position: float, population: int = OSU_RANK_POPULATION) -> int:
    softness = 2_500.0
    maximum = max(2, int(population))
    unit = max(0.0, min(1.0, float(position)))
    scale = math.log1p((maximum - 1) / softness)
    return max(1, min(maximum, int(round(1 + softness * math.expm1(unit * scale)))))


def rankbot_guess_for_attempt(
    *,
    actual_rank: int,
    predicted_rank: int,
    attempt: int,
    population: int = OSU_RANK_POPULATION,
) -> tuple[int, bool, str, str, float]:
    """Open with the model prediction, then binary-search the visible slider range."""
    maximum = max(2, int(population))
    actual = max(1, min(maximum, int(actual_rank)))
    opening = max(1, min(maximum, int(predicted_rank)))
    lower = 1
    upper = maximum

    guess = opening
    feedback = challenge_feedback(actual, guess)

    for turn in range(1, max(1, int(attempt)) + 1):
        if turn == 1:
            guess = opening
        else:
            left = _soft_rank_position(lower, maximum)
            right = _soft_rank_position(upper, maximum)
            guess = _soft_rank_from_position((left + right) / 2.0, maximum)
            guess = max(lower, min(upper, guess))

        feedback = challenge_feedback(actual, guess)
        correct, direction, _, _ = feedback

        if turn >= attempt or correct:
            return guess, *feedback

        if direction == "better":
            upper = min(upper, max(1, guess - 1))
        elif direction == "worse":
            lower = max(lower, min(maximum, guess + 1))

        if lower > upper:
            lower = upper = actual

    return guess, *feedback


@app.post("/api/challenge/guess")
async def challenge_guess(payload: ChallengeGuessPayload) -> JSONResponse:
    if not database_configured():
        raise HTTPException(status_code=503, detail={"code": "database_not_configured", "message": "Connect a database first."})
    try:
        row = await asyncio.to_thread(get_challenge_submission, payload.replay_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"code": "database_error", "message": str(exc)}) from exc
    if not row or not row.get("actual_rank"):
        raise HTTPException(status_code=404, detail={"code": "challenge_missing", "message": "Challenge replay was not found."})

    mode = payload.mode.strip().lower()
    if mode == "daily":
        challenge_date = payload.challenge_date or datetime.now(timezone.utc).date()
        daily_rows = await asyncio.to_thread(get_daily_challenge, challenge_date, 3)
        if payload.replay_id not in {item["public_id"] for item in daily_rows}:
            raise HTTPException(status_code=400, detail={"code": "not_daily_replay", "message": "Replay is not part of that daily challenge."})
        challenge_key = challenge_date.isoformat()
    elif mode == "infinite":
        challenge_key = payload.replay_id
    else:
        raise HTTPException(status_code=400, detail={"code": "invalid_mode", "message": "Mode must be daily or infinite."})

    try:
        await asyncio.to_thread(
            record_challenge_guess,
            replay_id=payload.replay_id,
            visitor_id=payload.visitor_id,
            mode=mode,
            challenge_key=challenge_key,
            guess_rank=payload.guess_rank,
        )
    except Exception as exc:
        print(json.dumps({"event": "challenge_guess_store_failed", "error": repr(exc)}), flush=True)

    actual_rank = int(row["actual_rank"])
    predicted_rank = max(1, min(OSU_RANK_POPULATION, int(row.get("predicted_rank") or 1)))
    correct, direction, closeness, log_error = challenge_feedback(actual_rank, payload.guess_rank)
    bot_guess, bot_correct, bot_direction, bot_closeness, bot_log_error = rankbot_guess_for_attempt(
        actual_rank=actual_rank,
        predicted_rank=predicted_rank,
        attempt=payload.attempt,
    )

    reveal = correct or bot_correct or payload.attempt >= MAX_CHALLENGE_ATTEMPTS
    if correct and bot_correct:
        if abs(log_error - bot_log_error) < 1e-12:
            turn_winner = "tie"
        else:
            turn_winner = "player" if log_error < bot_log_error else "bot"
    elif correct:
        turn_winner = "player"
    elif bot_correct:
        turn_winner = "bot"
    else:
        turn_winner = "pending"

    response: dict[str, Any] = {
        "ok": True,
        "correct": correct,
        "direction": direction,
        "closeness": closeness,
        "attempt": payload.attempt,
        "maxAttempts": MAX_CHALLENGE_ATTEMPTS,
        "revealed": reveal,
        "logError": log_error,
        "botGuess": bot_guess,
        "botCorrect": bot_correct,
        "botDirection": bot_direction,
        "botCloseness": bot_closeness,
        "botLogError": bot_log_error,
        "turnWinner": turn_winner,
    }
    if reveal:
        distribution = await asyncio.to_thread(
            challenge_guess_distribution,
            replay_id=payload.replay_id,
            mode=mode,
            challenge_key=challenge_key,
            rank_population=OSU_RANK_POPULATION,
        )
        response.update({
            "actualRank": actual_rank,
            "predictedRank": predicted_rank,
            "player": row["player"],
            "avatarURL": row.get("avatar_url"),
            "distribution": distribution,
        })
    return JSONResponse(response)
'''

app = app[:route_start] + replacement + app[route_end:]
app_path.write_text(app, encoding="utf-8")

clean_js_path = ROOT / "public" / "clean.js"
clean_js = clean_js_path.read_text(encoding="utf-8")

old_layout = '''      <div class="challenge-content clean-content">
        <div class="video-column"><div class="video-wrap"><video class="challenge-video" src="${escapeHTML(item.videoURL)}" autoplay loop playsinline preload="auto"></video><button class="video-toggle on" type="button" aria-label="Toggle sound">${ICON_SOUND}</button><button class="video-play" type="button" aria-label="Play or pause replay">pause</button></div></div>
        <aside class="challenge-side clean-history" hidden><ol class="guess-list duel-turn-list" aria-label="Turn history"></ol></aside>
      </div>'''
new_layout = '''      <div class="challenge-content clean-content">
        <div class="video-column"><div class="video-wrap"><video class="challenge-video" src="${escapeHTML(item.videoURL)}" autoplay loop playsinline preload="auto"></video><button class="video-toggle on" type="button" aria-label="Toggle sound">${ICON_SOUND}</button><button class="video-play" type="button" aria-label="Play or pause replay">pause</button></div></div>
      </div>
      <aside class="challenge-side clean-history" hidden><ol class="guess-list duel-turn-list" aria-label="Turn history"></ol></aside>'''
clean_js = clean_js.replace(old_layout, new_layout)

old_result = '''  const resultHTML = (round, mode) => {
    const winner = winnerFor(round) || "tie";
    const playerRatio = bestRatio(round.guesses, round.actualRank);
    const botRatio = bestRatio(round.botGuesses, round.actualRank);
    const botRank = round.botGuesses.at(-1)?.guessRank || round.predictedRank;
    const title = winner === "player" ? "you win" : winner === "bot" ? "rankbot wins" : "tie";
    return `<div class="duel-result-strip ${winner}">
      <div class="actual-block"><span>actual rank</span><strong>${formatRank(round.actualRank)}</strong><small>${escapeHTML(round.player || "player")}</small></div>
      <div class="outcome-block"><span>${title}</span><strong>bot ${formatRank(botRank)}</strong><small>you ${playerRatio.toFixed(2)}× · bot ${botRatio.toFixed(2)}×</small></div>
      <button class="primary-button next-challenge" type="button">${mode === "daily" ? "next" : "next replay"}</button>
    </div>`;
  };'''
new_result = '''  const resultHTML = (round, mode) => {
    const winner = winnerFor(round) || "tie";
    const playerRatio = bestRatio(round.guesses, round.actualRank);
    const botRatio = bestRatio(round.botGuesses, round.actualRank);
    const botRank = round.botGuesses.at(-1)?.guessRank || round.predictedRank;
    const title = winner === "player" ? "you win" : winner === "bot" ? "rankbot wins" : "tie";
    const ratioText = (value) => Number.isFinite(value) ? `${value.toFixed(2)}×` : "—";
    return `<div class="duel-result-strip ${winner}">
      <div class="actual-block"><span>actual rank</span><strong>${formatRank(round.actualRank)}</strong><small>${escapeHTML(round.player || "player")}</small></div>
      <div class="outcome-block"><span>${title}</span><strong>rankbot ${formatRank(botRank)}</strong><small>you ${ratioText(playerRatio)} · bot ${ratioText(botRatio)}</small></div>
      <button class="primary-button next-challenge" type="button">${mode === "daily" ? "next" : "next replay"}</button>
    </div>`;
  };'''
clean_js = clean_js.replace(old_result, new_result)

if old_layout in clean_js or 'bot ${ratioText(botRatio)}' not in clean_js:
    raise RuntimeError("clean.js patch did not apply")

clean_js_path.write_text(clean_js, encoding="utf-8")

clean_css = r'''/* Stable, minimal game layout. Loaded last. */
footer,.version-stamp,.challenge-rule,.bot-lock,.infinite-prefetch-status,#view-infinite .mode-intro{display:none!important}
main{padding-bottom:max(26px,env(safe-area-inset-bottom))}
.clean-shell{width:min(1040px,calc(100vw - 28px));margin:16px auto 0;padding-bottom:max(14px,env(safe-area-inset-bottom));overflow:visible}
.clean-topline{display:grid;grid-template-columns:auto 1fr auto;align-items:center;gap:12px;min-height:32px;margin-bottom:8px}
.clean-topline .game-round{color:var(--muted)}
.duel-title{justify-self:center;color:#fff;font:800 11px/1 var(--font);letter-spacing:.7px;text-transform:uppercase}
.clean-mapline{display:flex;align-items:baseline;justify-content:space-between;gap:16px;padding:0 2px 9px;border-bottom:1px solid var(--border-soft)}
.clean-mapline strong{min-width:0;overflow:hidden;color:#fff;font:750 13px/1.2 var(--font);text-overflow:ellipsis;white-space:nowrap}
.clean-mapline span{flex:0 0 auto;color:var(--muted);font:650 10px/1 var(--font);white-space:nowrap}
.clean-content{display:block!important;padding-top:12px}
.video-column{min-width:0;width:100%}
.clean-shell .video-wrap,#replayVideo,.gallery-dialog video{width:100%;aspect-ratio:16/9;height:auto!important;max-height:min(68vh,680px);background:#000;overflow:hidden}
.clean-shell .challenge-video,#replayVideo,.gallery-dialog video{display:block;width:100%;height:100%!important;object-fit:contain!important;background:#000}
.clean-history[hidden]{display:none!important}
.clean-history{width:100%;margin-top:9px;padding:0!important;border:0!important;background:transparent!important;overflow:visible}
.clean-history .duel-turn-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(205px,1fr));gap:7px;max-height:none;padding:0}
.clean-history .duel-turn{grid-template-columns:20px minmax(0,1fr);gap:4px 7px;padding:7px!important;background:rgba(255,255,255,.018)}
.clean-history .duel-turn-number{font-size:9px}
.clean-history .duel-guess{grid-template-columns:25px minmax(0,1fr) auto;gap:6px}
.clean-history .duel-guess strong{font-size:11px}
.clean-history .duel-guess em{font-size:7px}
.clean-dock{position:static!important;inset:auto!important;margin-top:10px;padding:0!important;overflow:visible!important}
.clean-dock .guess-zone{padding:11px!important;border:1px solid var(--border);background:rgba(255,255,255,.018);overflow:visible}
.clean-dock .guess-form{display:grid;grid-template-columns:minmax(0,1fr) 132px;align-items:end;gap:13px}
.duel-rank-control{min-width:0}
.duel-guess-head{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;margin-bottom:7px}
.duel-guess-head>div{display:grid;gap:4px}
.duel-guess-head>div>span,.duel-guess-head .rank-number-label>span,.known-range-copy span{color:var(--muted);font:800 8px/1 var(--font);letter-spacing:.75px;text-transform:uppercase}
.duel-guess-head .live-rank{color:#fff;font:800 clamp(24px,3.2vw,38px)/.92 var(--font);font-variant-numeric:tabular-nums}
.duel-guess-head .rank-number-label{display:grid;justify-items:end;gap:4px;margin:0}
.duel-guess-head .rank-number-shell{width:150px}
.known-range-copy{margin:0 0 5px}
.known-range-copy strong{font-size:10px}
.range-callout{min-height:13px;margin:7px 0 0;color:var(--muted);font:750 10px/1.2 var(--font);text-transform:none}
.clean-dock .guess-submit{width:132px;height:44px;margin-bottom:19px}
.clean-reveal{max-height:none!important;overflow:visible!important;margin:0!important;padding:0!important}
.duel-result-strip{display:grid;grid-template-columns:minmax(230px,1.25fr) minmax(210px,1fr) 118px;align-items:center;gap:18px;min-height:104px;padding:14px 15px;border:1px solid var(--border);background:rgba(255,255,255,.025)}
.duel-result-strip.player{border-color:rgba(93,226,160,.48)}
.duel-result-strip.bot{border-color:rgba(255,102,171,.45)}
.actual-block,.outcome-block{display:grid;gap:4px;min-width:0}
.actual-block span,.outcome-block span{color:var(--muted);font:800 9px/1 var(--font);letter-spacing:.8px;text-transform:uppercase}
.actual-block strong{color:#fff;font:850 clamp(30px,4.6vw,54px)/.92 var(--font);font-variant-numeric:tabular-nums;white-space:nowrap}
.actual-block small,.outcome-block small{overflow:hidden;color:var(--muted);font:650 10px/1.2 var(--font);text-overflow:ellipsis;white-space:nowrap}
.outcome-block strong{color:#fff;font:800 16px/1 var(--font);font-variant-numeric:tabular-nums}
.duel-result-strip.player .outcome-block span{color:var(--green)}
.duel-result-strip.bot .outcome-block span{color:var(--pink)}
.duel-result-strip .next-challenge{align-self:stretch;width:118px;min-height:50px;margin:0!important}
.generation-card{min-height:260px;display:grid;place-content:center;text-align:center}
.generation-card p{display:none}
#view-analyze .page-heading .kicker,#view-analyze .page-heading>p,.process-card .steps small,.process-card .tiny-note,#view-gallery .page-heading p{display:none!important}
#view-analyze .page-heading{margin-bottom:14px}
#view-analyze .page-heading h1{font-size:clamp(28px,4vw,44px)}
.result-block{padding-bottom:max(18px,env(safe-area-inset-bottom))}
@media(max-width:760px){.clean-shell{width:calc(100vw - 16px);margin-top:8px}.clean-topline{grid-template-columns:auto auto;justify-content:space-between}.duel-title{display:none}.clean-mapline{display:grid;gap:4px}.clean-mapline strong,.clean-mapline span{white-space:normal}.clean-content{padding-top:8px}.clean-shell .video-wrap{max-height:none}.clean-history .duel-turn-list{grid-template-columns:1fr 1fr}.clean-dock{margin-top:8px}.clean-dock .guess-zone{padding:9px!important}.clean-dock .guess-form{grid-template-columns:1fr}.clean-dock .guess-submit{width:100%;height:46px;margin:0}.duel-result-strip{grid-template-columns:1fr 1fr;gap:10px;min-height:0;padding:12px}.duel-result-strip .next-challenge{grid-column:1/-1;width:100%;min-height:50px;margin-bottom:max(2px,env(safe-area-inset-bottom))!important}}
@media(max-width:520px){.duel-guess-head{align-items:stretch}.duel-guess-head .live-rank{font-size:28px}.duel-guess-head .rank-number-shell{width:120px}.clean-history .duel-turn-list{grid-template-columns:1fr}.actual-block strong{font-size:42px}.attempt-stack{gap:6px}}
'''
(ROOT / "public" / "clean.css").write_text(clean_css, encoding="utf-8")

index_path = ROOT / "public" / "index.html"
index = index_path.read_text(encoding="utf-8")
index = index.replace('/clean.css?v=1', '/clean.css?v=2').replace('/clean.js?v=1', '/clean.js?v=2')
index_path.write_text(index, encoding="utf-8")

print("game fix applied")
