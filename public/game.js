/* Make the guessing modes feel like an actual game. Loaded last. */
(() => {
  const previousUpdateChallengeRound = updateChallengeRound;
  const previousRenderDailySummary = renderDailySummary;

  const estimateActual = (guessRank, result) => {
    if (Number(result.actualRank) > 0) return Number(result.actualRank);
    const factor = 10 ** Math.max(0, Number(result.logError) || 0);
    return result.direction === "better" ? guessRank / factor : guessRank * factor;
  };

  const allowanceFor = (actualRank) => 0.022 + 0.075 / Math.sqrt(1 + Math.max(1, actualRank) / 1000);

  const closenessFor = (logError, allowance, correct) => {
    if (correct) return "exact";
    if (logError <= allowance * 1.8) return "very_close";
    if (logError <= allowance * 3.5) return "close";
    return "far";
  };

  const knownRange = (round) => {
    let lower = 1;
    let upper = rankPopulation;

    for (const guess of round.guesses || []) {
      const value = clamp(Math.round(Number(guess.guessRank) || 1), 1, rankPopulation);
      if (guess.correct && Number(guess.actualRank) > 0) {
        lower = Number(guess.actualRank);
        upper = Number(guess.actualRank);
        break;
      }
      if (guess.direction === "better") upper = Math.min(upper, Math.max(1, value - 1));
      if (guess.direction === "worse") lower = Math.max(lower, Math.min(rankPopulation, value + 1));
    }

    if (round.revealed && Number(round.actualRank) > 0) {
      lower = Number(round.actualRank);
      upper = Number(round.actualRank);
    }

    if (lower > upper) {
      const midpoint = clamp(Math.round((lower + upper) / 2), 1, rankPopulation);
      lower = midpoint;
      upper = midpoint;
    }

    return { lower, upper };
  };

  const rankRatio = (left, right) => Math.max(left, right) / Math.max(1, Math.min(left, right));

  const bestPlayerRatio = (round) => {
    if (!Number(round.actualRank) || !round.guesses?.length) return Infinity;
    return Math.min(...round.guesses.map((guess) => rankRatio(Number(guess.guessRank), Number(round.actualRank))));
  };

  const playerPoints = (round) => {
    const ratio = bestPlayerRatio(round);
    if (!Number.isFinite(ratio)) return 0;
    const attemptPenalty = Math.max(0, (round.guesses.length - 1) * 55);
    return Math.max(0, Math.round(1000 / Math.pow(ratio, 1.45) - attemptPenalty));
  };

  const resultCopy = (guess) => {
    if (guess.correct) return "nailed it";
    const heat = guess.closeness === "very_close" ? "hot" : guess.closeness === "close" ? "warm" : "cold";
    const direction = guess.direction === "better" ? "go left" : "go right";
    return `${heat} · ${direction}`;
  };

  feedbackText = resultCopy;

  rankControlHTML = function gameRankControl(initialRank = 50_000) {
    const position = rankToSoftPosition(initialRank);
    const ranks = [...new Set([1, 1_000, 10_000, 100_000, 1_000_000, rankPopulation].filter((rank) => rank <= rankPopulation))];
    const ticks = ranks.map((rank) => `<button type="button" class="slider-tick" data-rank="${rank}" style="left:${(rankToSoftPosition(rank) / SLIDER_STEPS * 100).toFixed(3)}%">${compactRank(rank)}</button>`).join("");

    return `<div class="rank-control game-rank-control">
      <div class="guess-readout">
        <div class="guess-value"><span>lock in a rank</span><strong class="live-rank">${formatRank(initialRank)}</strong></div>
        <label class="rank-number-label"><span>exact rank</span><div class="rank-number-shell"><b>#</b><input class="rank-number-input" type="number" min="1" max="${rankPopulation}" inputmode="numeric" value="${initialRank}" required /></div></label>
      </div>
      <div class="known-range-copy"><span>possible range</span><strong class="known-range-text">${formatRank(1)} – ${formatRank(rankPopulation)}</strong></div>
      <div class="rank-slider-shell game-slider-shell">
        <span class="rank-range-mask rank-range-mask-left" aria-hidden="true"></span>
        <span class="rank-known-range" aria-hidden="true"></span>
        <span class="rank-range-mask rank-range-mask-right" aria-hidden="true"></span>
        <span class="rank-slider-fill" aria-hidden="true"></span>
        <input class="rank-slider" type="range" min="0" max="${SLIDER_STEPS}" step="1" value="${position}" aria-label="Rank guess slider" />
      </div>
      <div class="slider-scale" aria-label="Rank shortcuts">${ticks}</div>
      <div class="game-hint-line"><span class="range-callout">watch the replay, then lock it in</span><span>left = better rank · right = worse rank</span></div>
    </div>`;
  };

  challengeCardHTML = function gameChallengeCard(item, label) {
    const map = item.beatmap || {};
    const title = `${map.artist ? `${map.artist} — ` : ""}${map.title || "Unknown map"}`;
    const stats = `${map.version || "Unknown difficulty"} · ${Number(item.star || 0).toFixed(2)}★ · ${Number(item.accuracyPercent || 0).toFixed(2)}% · ${(item.mods || ["NM"]).join("")}`;
    const prefetch = String(label).startsWith("infinite")
      ? '<span class="infinite-prefetch-status" data-state="loading">loading next replay</span>'
      : "";
    const pips = Array.from({ length: MAX_ATTEMPTS }, () => "<i></i>").join("");

    return `<div class="challenge-shell game-shell">
      <div class="challenge-topline game-topline">
        <span class="game-round">${escapeHTML(label)}</span>
        <div class="versus-banner"><strong>you</strong><i>vs</i><strong>rankbot</strong><span>bot guess locked</span></div>
        <div class="attempt-stack"><span class="attempt-copy">${MAX_ATTEMPTS} guesses left</span><div class="attempt-pips" aria-label="Attempts remaining">${pips}</div></div>
      </div>
      <div class="challenge-content">
        <div class="video-column"><div class="video-wrap"><video class="challenge-video" src="${escapeHTML(item.videoURL)}" autoplay loop playsinline preload="auto"></video><button class="video-toggle on" type="button" aria-label="Toggle sound">${ICON_SOUND}</button><button class="video-play" type="button" aria-label="Play or pause replay">pause</button></div></div>
        <aside class="challenge-side">
          <div class="stage-info"><strong>${escapeHTML(title)}</strong><span>${escapeHTML(stats)}</span>${prefetch}</div>
          <div class="bot-lock"><span>rankbot</span><strong class="bot-lock-value">guess locked</strong><small>get closer than the model</small></div>
          <ol class="guess-list" aria-label="Previous guesses"></ol>
        </aside>
      </div>
      <div class="guess-dock"><div class="guess-zone"><form class="guess-form">${rankControlHTML()}<button class="primary-button guess-submit" type="submit">lock it in</button></form><p class="challenge-rule">every miss narrows the board. beat rankbot's final error.</p><p class="challenge-error" hidden></p><div class="reveal-panel" hidden></div></div></div>
    </div>`;
  };

  mountChallenge = function gameMountChallenge(rootElement, item, round, label, mode, challengeDate = null) {
    rootElement.innerHTML = challengeCardHTML(item, label);
    round.root = rootElement;
    round.rankControl = bindRankControl(rootElement);
    bindChallengeVideo(rootElement);

    const form = rootElement.querySelector(".guess-form");
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = form.querySelector(".guess-submit");
      const error = rootElement.querySelector(".challenge-error");
      error.hidden = true;
      button.disabled = true;
      button.textContent = "checking…";
      try {
        await submitChallengeGuess(round, mode, challengeDate);
      } catch (failure) {
        error.textContent = failure.message || "Guess failed.";
        error.hidden = false;
      } finally {
        button.disabled = false;
        button.textContent = "lock it in";
      }
    });

    updateChallengeRound(round, mode, challengeDate);
  };

  submitChallengeGuess = async function gameSubmitGuess(round, mode, challengeDate) {
    const guessRank = round.rankControl.value();
    const attempt = round.guesses.length + 1;
    const payload = {
      replayID: round.item.id,
      guessRank,
      attempt,
      mode,
      challengeDate,
      visitorID,
    };

    let result = await requestJSON("/api/challenge/guess", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    const actualEstimate = estimateActual(guessRank, result);
    const logError = Math.max(0, Number(result.logError) || 0);
    const allowance = allowanceFor(actualEstimate);
    const adaptiveCorrect = Math.abs(guessRank - actualEstimate) <= 100 || logError <= allowance;
    const adaptiveDirection = adaptiveCorrect ? "correct" : actualEstimate < guessRank ? "better" : "worse";

    if (adaptiveCorrect && !result.revealed) {
      result = await requestJSON("/api/challenge/guess", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...payload, guessRank: Math.max(1, Math.round(actualEstimate)) }),
      });
    }

    if (!adaptiveCorrect && result.revealed && attempt < MAX_ATTEMPTS) {
      result = {
        ...result,
        revealed: false,
        actualRank: undefined,
        predictedRank: undefined,
        player: undefined,
        distribution: undefined,
      };
    }

    result.correct = adaptiveCorrect;
    result.direction = adaptiveDirection;
    result.closeness = closenessFor(logError, allowance, adaptiveCorrect);

    round.guesses.push({ guessRank, ...result });
    if (result.revealed) {
      round.revealed = true;
      round.actualRank = result.actualRank;
      round.predictedRank = result.predictedRank;
      round.player = result.player;
      round.distribution = result.distribution || null;
    }

    // Keep the thumb on the user's last guess. The track shows the new range.
    updateChallengeRound(round, mode, challengeDate);
    if (mode === "daily") saveDailyState();
  };

  const updateRange = (round) => {
    const root = round.root;
    if (!root) return;
    const { lower, upper } = knownRange(round);
    const left = rankToSoftPosition(lower) / SLIDER_STEPS * 100;
    const right = rankToSoftPosition(upper) / SLIDER_STEPS * 100;
    const width = Math.max(0.35, right - left);

    const range = root.querySelector(".rank-known-range");
    const leftMask = root.querySelector(".rank-range-mask-left");
    const rightMask = root.querySelector(".rank-range-mask-right");
    const text = root.querySelector(".known-range-text");

    if (range) {
      range.style.left = `${left.toFixed(3)}%`;
      range.style.width = `${width.toFixed(3)}%`;
    }
    if (leftMask) leftMask.style.width = `${left.toFixed(3)}%`;
    if (rightMask) rightMask.style.width = `${Math.max(0, 100 - right).toFixed(3)}%`;
    if (text) text.textContent = lower === upper ? formatRank(lower) : `${formatRank(lower)} – ${formatRank(upper)}`;

    root.querySelectorAll(".slider-tick").forEach((tick) => {
      const rank = Number(tick.dataset.rank);
      tick.classList.toggle("outside-range", rank < lower || rank > upper);
    });
  };

  const updateGameFeedback = (round) => {
    const root = round.root;
    if (!root) return;
    const last = round.guesses?.at(-1);
    const callout = root.querySelector(".range-callout");
    const pips = [...root.querySelectorAll(".attempt-pips i")];
    const lock = root.querySelector(".bot-lock");
    const lockValue = root.querySelector(".bot-lock-value");

    pips.forEach((pip, index) => {
      pip.classList.toggle("used", index < round.guesses.length);
      pip.classList.toggle("hit", Boolean(round.guesses[index]?.correct));
    });

    root.querySelector(".game-shell")?.setAttribute("data-heat", last?.correct ? "exact" : last?.closeness || "idle");

    if (callout) {
      if (!last) callout.textContent = "watch the replay, then lock it in";
      else if (last.correct) callout.textContent = "nailed it. now see if you beat rankbot.";
      else if (last.direction === "better") callout.textContent = `${last.closeness === "very_close" ? "hot" : last.closeness === "close" ? "warm" : "cold"} — the player is better. move left.`;
      else callout.textContent = `${last.closeness === "very_close" ? "hot" : last.closeness === "close" ? "warm" : "cold"} — the player is worse. move right.`;
    }

    if (round.revealed && Number(round.actualRank) > 0 && Number(round.predictedRank) > 0) {
      const playerRatio = bestPlayerRatio(round);
      const botRatio = rankRatio(Number(round.predictedRank), Number(round.actualRank));
      const won = playerRatio <= botRatio;
      if (lock) lock.classList.add(won ? "player-won" : "bot-won");
      if (lockValue) lockValue.textContent = `${formatRank(round.predictedRank)} · ${won ? "you win" : "bot wins"}`;

      const panel = root.querySelector(".reveal-panel");
      if (panel && !panel.querySelector(".battle-result")) {
        const result = document.createElement("div");
        result.className = `battle-result ${won ? "player-won" : "bot-won"}`;
        result.innerHTML = `<div><span>${won ? "you beat the bot" : "rankbot got this one"}</span><strong>${playerPoints(round).toLocaleString()} pts</strong></div><p>your best <b>${playerRatio.toFixed(2)}×</b> · bot <b>${botRatio.toFixed(2)}×</b></p>`;
        panel.prepend(result);
      }
    }
  };

  updateChallengeRound = function gameUpdateRound(round, mode, challengeDate) {
    previousUpdateChallengeRound(round, mode, challengeDate);
    if (!round.root) return;

    const left = Math.max(0, MAX_ATTEMPTS - round.guesses.length);
    const copy = round.root.querySelector(".attempt-copy");
    if (copy) copy.textContent = round.revealed ? "round over" : `${left} guess${left === 1 ? "" : "es"} left`;

    updateRange(round);
    updateGameFeedback(round);
  };

  renderDailySummary = function gameDailySummary() {
    previousRenderDailySummary();
    const root = document.querySelector("#dailyRoot");
    const heading = root?.querySelector(".daily-summary h1");
    if (!root || !heading || !dailyState?.rounds) return;

    let wins = 0;
    let points = 0;
    for (const round of dailyState.rounds) {
      if (!Number(round.actualRank) || !Number(round.predictedRank) || !round.guesses?.length) continue;
      const playerRatio = bestPlayerRatio(round);
      const botRatio = rankRatio(Number(round.predictedRank), Number(round.actualRank));
      if (playerRatio <= botRatio) wins += 1;
      points += playerPoints(round);
    }

    const battle = document.createElement("div");
    battle.className = "daily-battle-summary";
    battle.innerHTML = `<span>you vs rankbot</span><strong>${wins}–${dailyState.rounds.length - wins}</strong><em>${points.toLocaleString()} pts</em>`;
    heading.after(battle);
  };
})();