/* Multi-turn rankbot duel layer. Loaded after game.js. */
(() => {
  const previousUpdateChallengeRound = updateChallengeRound;
  const previousRenderDailySummary = renderDailySummary;

  const rankRatio = (left, right) => Math.max(left, right) / Math.max(1, Math.min(left, right));
  const logRank = (rank) => Math.log10(Math.max(1, Number(rank) || 1));
  const fromLogRank = (value) => clamp(Math.round(10 ** value), 1, rankPopulation);
  const heatName = (guess) => guess.correct ? "hit" : guess.closeness === "very_close" ? "hot" : guess.closeness === "close" ? "warm" : "cold";
  const directionCopy = (guess) => guess.correct ? "nailed it" : `${heatName(guess)} · ${guess.direction === "better" ? "go left" : "go right"}`;

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

  const normalizeResult = (guessRank, result) => {
    const actualEstimate = estimateActual(guessRank, result);
    const logError = Math.max(0, Number(result.logError) || 0);
    const allowance = allowanceFor(actualEstimate);
    const correct = Boolean(result.correct) || Math.abs(guessRank - actualEstimate) <= 100 || logError <= allowance;
    return {
      ...result,
      correct,
      direction: correct ? "correct" : actualEstimate < guessRank ? "better" : "worse",
      closeness: closenessFor(logError, allowance, correct),
      actualEstimate,
    };
  };

  const duelRange = (round) => {
    let lower = 1;
    let upper = rankPopulation;
    for (const turn of round.guesses || []) {
      for (const guess of [turn, turn.bot].filter(Boolean)) {
        const value = clamp(Math.round(Number(guess.guessRank) || 1), 1, rankPopulation);
        if (guess.correct) {
          const actual = Number(guess.actualRank || guess.actualEstimate || round.actualRank);
          if (actual > 0) return { lower: Math.round(actual), upper: Math.round(actual) };
        }
        if (guess.direction === "better") upper = Math.min(upper, Math.max(1, value - 1));
        if (guess.direction === "worse") lower = Math.max(lower, Math.min(rankPopulation, value + 1));
      }
    }
    if (round.revealed && Number(round.actualRank) > 0) return { lower: Number(round.actualRank), upper: Number(round.actualRank) };
    if (lower > upper) {
      const midpoint = clamp(Math.round((lower + upper) / 2), 1, rankPopulation);
      return { lower: midpoint, upper: midpoint };
    }
    return { lower, upper };
  };

  const seededUnit = (text) => {
    let hash = 2166136261;
    for (const character of String(text)) {
      hash ^= character.charCodeAt(0);
      hash = Math.imul(hash, 16777619);
    }
    return ((hash >>> 0) % 1000003) / 1000003;
  };

  const initialBotPrior = (round) => {
    const item = round.item || {};
    const star = Math.max(0, Number(item.star) || 0);
    const accuracy = Math.max(0, Math.min(1, (Number(item.accuracyPercent) || 0) / 100));
    const mods = (item.mods || []).map(String);
    const speedBonus = mods.some((mod) => mod === "DT" || mod === "NC") ? 0.13 : mods.includes("HT") ? -0.10 : 0;
    const hiddenBonus = mods.includes("HD") ? 0.05 : 0;
    const skill = clamp(0.42 * star + 2.6 * (accuracy - 0.90) + speedBonus + hiddenBonus + 0.08, 0.10, 5.65);
    return clamp(Math.round(rankPopulation * 10 ** (-skill)), 1, rankPopulation);
  };

  const chooseBotGuess = (round, attempt) => {
    const { lower, upper } = duelRange(round);
    const prior = Number(round.botPrior) || initialBotPrior(round);
    round.botPrior = prior;
    if (attempt === 1) return clamp(prior, lower, upper);

    const lowLog = logRank(lower);
    const highLog = logRank(upper);
    const midpoint = (lowLog + highLog) / 2;
    const clippedPrior = Math.max(lowLog, Math.min(highLog, logRank(prior)));
    const priorWeight = Math.max(0.10, 0.42 - 0.08 * (attempt - 2));
    const jitterScale = Math.max(0, (highLog - lowLog) * (0.12 / attempt));
    const jitter = (seededUnit(`${round.item?.id}:${attempt}`) - 0.5) * 2 * jitterScale;
    return fromLogRank((1 - priorWeight) * midpoint + priorWeight * clippedPrior + jitter);
  };

  const forceReveal = async (payload, actualEstimate) => requestJSON("/api/challenge/guess", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...payload, guessRank: Math.max(1, Math.round(actualEstimate)) }),
  });

  submitChallengeGuess = async function duelSubmitGuess(round, mode, challengeDate) {
    const guessRank = round.rankControl.value();
    const attempt = round.guesses.length + 1;
    const botGuess = chooseBotGuess(round, attempt);
    const common = {
      replayID: round.item.id,
      attempt,
      mode,
      challengeDate,
    };
    const userPayload = { ...common, guessRank, visitorID };
    const botPayload = { ...common, guessRank: botGuess, visitorID: `${visitorID}:rankbot`.slice(0, 128) };

    round.botThinking = true;
    updateChallengeRound(round, mode, challengeDate);

    let [userRaw, botRaw] = await Promise.all([
      requestJSON("/api/challenge/guess", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(userPayload),
      }),
      requestJSON("/api/challenge/guess", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(botPayload),
      }),
    ]);

    let userResult = normalizeResult(guessRank, userRaw);
    let botResult = normalizeResult(botGuess, botRaw);

    if (userResult.correct && !userRaw.revealed) {
      userRaw = await forceReveal(userPayload, userResult.actualEstimate);
      userResult = normalizeResult(guessRank, { ...userRaw, correct: true });
    } else if (botResult.correct && !botRaw.revealed) {
      botRaw = await forceReveal(botPayload, botResult.actualEstimate);
      botResult = normalizeResult(botGuess, { ...botRaw, correct: true });
    }

    const revealSource = [userRaw, botRaw].find((result) => result.revealed && Number(result.actualRank) > 0);
    const turn = {
      guessRank,
      ...userResult,
      bot: { guessRank: botGuess, ...botResult },
    };
    round.guesses.push(turn);
    round.botThinking = false;

    if (revealSource || attempt >= MAX_ATTEMPTS || userResult.correct || botResult.correct) {
      round.revealed = true;
      round.actualRank = Number(revealSource?.actualRank || userResult.actualEstimate || botResult.actualEstimate);
      round.predictedRank = Number(revealSource?.predictedRank || 0);
      round.player = revealSource?.player;
      round.distribution = revealSource?.distribution || null;
      round.duelWinner = userResult.correct && botResult.correct
        ? (Number(userResult.logError) <= Number(botResult.logError) ? "player" : "bot")
        : userResult.correct ? "player" : botResult.correct ? "bot" : null;
    }

    updateChallengeRound(round, mode, challengeDate);
    if (mode === "daily") saveDailyState();
  };

  const renderDuelHistory = (round) => {
    const list = round.root?.querySelector(".guess-list");
    if (!list) return;
    list.innerHTML = (round.guesses || []).map((turn, index) => {
      const playerClass = turn.correct ? "hit" : turn.closeness || "far";
      const bot = turn.bot || {};
      const botClass = bot.correct ? "hit" : bot.closeness || "far";
      return `<li class="duel-turn">
        <span class="duel-turn-number">${index + 1}</span>
        <div class="duel-guess ${playerClass}"><b>you</b><strong>${formatRank(turn.guessRank)}</strong><em>${escapeHTML(directionCopy(turn))}</em></div>
        <div class="duel-guess bot ${botClass}"><b>bot</b><strong>${formatRank(bot.guessRank)}</strong><em>${escapeHTML(directionCopy(bot))}</em></div>
      </li>`;
    }).join("");
  };

  const updateDuelRange = (round) => {
    const root = round.root;
    if (!root) return;
    const { lower, upper } = duelRange(round);
    const left = rankToSoftPosition(lower) / SLIDER_STEPS * 100;
    const right = rankToSoftPosition(upper) / SLIDER_STEPS * 100;
    const range = root.querySelector(".rank-known-range");
    const leftMask = root.querySelector(".rank-range-mask-left");
    const rightMask = root.querySelector(".rank-range-mask-right");
    const text = root.querySelector(".known-range-text");
    if (range) {
      range.style.left = `${left.toFixed(3)}%`;
      range.style.width = `${Math.max(0.35, right - left).toFixed(3)}%`;
    }
    if (leftMask) leftMask.style.width = `${left.toFixed(3)}%`;
    if (rightMask) rightMask.style.width = `${Math.max(0, 100 - right).toFixed(3)}%`;
    if (text) text.textContent = lower === upper ? formatRank(lower) : `${formatRank(lower)} – ${formatRank(upper)}`;
  };

  const bestRatio = (round, side) => {
    if (!Number(round.actualRank) || !round.guesses?.length) return Infinity;
    return Math.min(...round.guesses.map((turn) => rankRatio(Number(side === "bot" ? turn.bot?.guessRank : turn.guessRank), Number(round.actualRank))));
  };

  const renderDuelResult = (round) => {
    if (!round.revealed || !Number(round.actualRank)) return;
    const root = round.root;
    const panel = root?.querySelector(".reveal-panel");
    if (!panel) return;
    panel.querySelector(".battle-result")?.remove();
    const playerRatio = bestRatio(round, "player");
    const botRatio = bestRatio(round, "bot");
    const winner = round.duelWinner || (playerRatio <= botRatio ? "player" : "bot");
    const won = winner === "player";
    const result = document.createElement("div");
    result.className = `battle-result ${won ? "player-won" : "bot-won"}`;
    result.innerHTML = `<div><span>${won ? "you beat rankbot" : "rankbot wins"}</span><strong>${formatRank(round.actualRank)}</strong></div><p>your best <b>${playerRatio.toFixed(2)}×</b> · bot <b>${botRatio.toFixed(2)}×</b></p>`;
    panel.prepend(result);
  };

  updateChallengeRound = function duelUpdateRound(round, mode, challengeDate) {
    previousUpdateChallengeRound(round, mode, challengeDate);
    if (!round.root) return;
    renderDuelHistory(round);
    updateDuelRange(round);

    const turn = round.guesses.length + 1;
    const lockValue = round.root.querySelector(".bot-lock-value");
    const banner = round.root.querySelector(".versus-banner span");
    const callout = round.root.querySelector(".range-callout");
    const button = round.root.querySelector(".guess-submit");
    if (round.botThinking) {
      if (lockValue) lockValue.textContent = "thinking…";
      if (banner) banner.textContent = "bot thinking";
    } else if (round.revealed) {
      if (lockValue) lockValue.textContent = "round over";
      if (banner) banner.textContent = "final result";
    } else {
      if (lockValue) lockValue.textContent = `turn ${turn} locked`;
      if (banner) banner.textContent = `turn ${turn} locked`;
    }
    if (button && !round.revealed) button.textContent = `lock turn ${turn}`;
    if (callout && round.guesses.length) {
      const last = round.guesses.at(-1);
      const lead = Number(last.logError) <= Number(last.bot?.logError);
      callout.textContent = last.correct ? "you hit it." : last.bot?.correct ? "rankbot hit it." : lead ? "you took the lead." : "rankbot took the lead.";
    }
    renderDuelResult(round);
  };

  renderDailySummary = function duelDailySummary() {
    previousRenderDailySummary();
    const root = document.querySelector("#dailyRoot");
    const old = root?.querySelector(".daily-battle-summary");
    if (!root || !dailyState?.rounds) return;
    old?.remove();
    let wins = 0;
    let played = 0;
    for (const round of dailyState.rounds) {
      if (!round.revealed || !Number(round.actualRank) || !round.guesses?.length) continue;
      played += 1;
      if (bestRatio(round, "player") <= bestRatio(round, "bot")) wins += 1;
    }
    const heading = root.querySelector(".daily-summary h1");
    if (!heading) return;
    const battle = document.createElement("div");
    battle.className = "daily-battle-summary";
    battle.innerHTML = `<span>best of three</span><strong>${wins}–${Math.max(0, played - wins)}</strong><em>${wins >= 2 ? "you win the set" : played >= 3 ? "rankbot wins" : `${3 - played} left`}</em>`;
    heading.after(battle);
  };
})();
