/* Focused game fixes loaded after polish.js. */
(() => {
  const baseRankControlHTML = rankControlHTML;
  const baseUpdateChallengeRound = updateChallengeRound;

  const rankRatio = (guess, actual) => {
    const left = Number(guess);
    const right = Number(actual);
    if (!(left > 0) || !(right > 0)) return Infinity;
    return Math.max(left, right) / Math.max(1, Math.min(left, right));
  };

  const bestRatio = (guesses, actualRank) => {
    if (!Array.isArray(guesses) || !guesses.length) return Infinity;
    return Math.min(...guesses.map((guess) => rankRatio(guess?.guessRank, actualRank)));
  };

  const firstHit = (guesses) => Array.isArray(guesses)
    ? guesses.findIndex((guess) => guess?.correct)
    : -1;

  const winnerFor = (round) => {
    const playerHit = firstHit(round.guesses);
    const botHit = firstHit(round.botGuesses);
    if (playerHit >= 0 || botHit >= 0) {
      if (playerHit < 0) return "bot";
      if (botHit < 0) return "player";
      if (playerHit !== botHit) return playerHit < botHit ? "player" : "bot";
      const playerError = Number(round.guesses[playerHit]?.logError) || 0;
      const botError = Number(round.botGuesses[botHit]?.logError) || 0;
      if (Math.abs(playerError - botError) < 1e-9) return "tie";
      return playerError < botError ? "player" : "bot";
    }

    const playerRatio = bestRatio(round.guesses, round.actualRank);
    const botRatio = bestRatio(round.botGuesses, round.actualRank);
    if (Math.abs(playerRatio - botRatio) < 1e-9) return "tie";
    return playerRatio < botRatio ? "player" : "bot";
  };

  const ratioText = (value) => Number.isFinite(value) ? `${value.toFixed(2)}×` : "—";

  const estimateActual = (guessRank, result) => {
    if (Number(result?.actualRank) > 0) return Number(result.actualRank);
    const error = Math.max(0, Number(result?.logError) || 0);
    const factor = 10 ** error;
    if (result?.direction === "better") return guessRank / factor;
    if (result?.direction === "worse") return guessRank * factor;
    return guessRank;
  };

  const allowanceFor = (actualRank) => 0.022 + 0.075 / Math.sqrt(1 + Math.max(1, actualRank) / 1000);

  const evaluateGuess = (guessRank, actualRank) => {
    const guess = clamp(Math.round(Number(guessRank) || 1), 1, rankPopulation);
    const actual = clamp(Math.round(Number(actualRank) || 1), 1, rankPopulation);
    const logError = Math.abs(Math.log10(guess) - Math.log10(actual));
    const correct = Math.abs(guess - actual) <= 100 || logError <= allowanceFor(actual);
    return {
      guessRank: guess,
      correct,
      direction: correct ? "correct" : actual < guess ? "better" : "worse",
      logError,
    };
  };

  const botRange = (round) => {
    let lower = 1;
    let upper = rankPopulation;
    for (const guess of round.botGuesses || []) {
      const value = clamp(Math.round(Number(guess?.guessRank) || 1), 1, rankPopulation);
      if (guess?.direction === "better") upper = Math.min(upper, Math.max(1, value - 1));
      if (guess?.direction === "worse") lower = Math.max(lower, Math.min(rankPopulation, value + 1));
    }
    if (lower > upper) lower = upper = clamp(Math.round((lower + upper) / 2), 1, rankPopulation);
    return { lower, upper };
  };

  const chooseBotGuess = (round, attempt, openingGuess) => {
    if (attempt <= 1 || !round.botGuesses?.length) {
      return clamp(Math.round(Number(openingGuess) || 50_000), 1, rankPopulation);
    }

    const { lower, upper } = botRange(round);
    const previous = clamp(Math.round(Number(round.botGuesses.at(-1)?.guessRank) || openingGuess || 50_000), lower, upper);
    const left = rankToSoftPosition(lower) / SLIDER_STEPS;
    const right = rankToSoftPosition(upper) / SLIDER_STEPS;
    const previousPosition = rankToSoftPosition(previous) / SLIDER_STEPS;
    const target = (left + right) / 2;
    const progress = Math.min(0.68, 0.42 + (attempt - 2) * 0.08);
    const nextPosition = previousPosition + (target - previousPosition) * progress;
    return clamp(softPositionToRank(Math.round(nextPosition * SLIDER_STEPS)), lower, upper);
  };

  const directionLabel = (guess) => {
    if (guess?.correct) return "correct";
    if (guess?.direction === "better") return "too high";
    if (guess?.direction === "worse") return "too low";
    return "guess";
  };

  const renderHistory = (round) => {
    const list = round.root?.querySelector(".guess-list");
    if (!list) return;
    list.innerHTML = (round.guesses || []).map((guess, index) => `<li class="duel-turn ${guess?.correct ? "player-hit" : ""}">
      <div class="guess-summary"><span>${directionLabel(guess)}:</span><strong>${formatRank(guess?.guessRank)}</strong></div>
      <span class="duel-turn-number">${String(index + 1).padStart(2, "0")}</span>
    </li>`).join("");
  };

  const resultHTML = (round, mode) => {
    const winner = winnerFor(round);
    const playerRatio = bestRatio(round.guesses, round.actualRank);
    const botRatio = bestRatio(round.botGuesses, round.actualRank);
    const heading = winner === "player" ? "you win" : winner === "bot" ? "rankbot wins" : "tie";
    const comparison = winner === "player"
      ? "closer than rankbot"
      : winner === "bot"
        ? "rankbot was closer"
        : "same error";
    const turns = (round.guesses || []).map((guess, index) => {
      const botGuess = round.botGuesses?.[index];
      return `<div class="result-turn">
        <span>${String(index + 1).padStart(2, "0")}</span>
        <strong>${formatRank(guess?.guessRank)}</strong>
        <strong>${formatRank(botGuess?.guessRank)}</strong>
      </div>`;
    }).join("");

    return `<div class="result-backdrop" role="dialog" aria-modal="true" aria-labelledby="roundResultTitle">
      <section class="result-dialog" tabindex="-1">
        <span class="result-kicker">actual rank</span>
        <strong class="result-actual">${formatRank(round.actualRank)}</strong>
        <small class="result-player">${escapeHTML(round.player || "player")}</small>
        <div class="result-verdict ${winner}">
          <span id="roundResultTitle">${heading}</span>
          <strong>${comparison}</strong>
          <small>you ${ratioText(playerRatio)} · rankbot ${ratioText(botRatio)}</small>
        </div>
        <div class="result-turns" aria-label="Final guesses">
          <div class="result-turn result-turn-head"><span>turn</span><span>you</span><span>rankbot</span></div>
          ${turns}
        </div>
        <button class="primary-button next-challenge" type="button">${mode === "daily" ? "next" : "next replay"}</button>
      </section>
    </div>`;
  };

  rankControlHTML = function fixedRankControl(initialRank = 50_000) {
    return baseRankControlHTML(initialRank)
      .replace('type="number"', 'type="text" pattern="[0-9]*" autocomplete="off" spellcheck="false"');
  };

  submitChallengeGuess = async function fixedSubmitGuess(round, mode, challengeDate) {
    if (!Array.isArray(round.guesses)) round.guesses = [];
    if (!Array.isArray(round.botGuesses)) round.botGuesses = [];

    const guessRank = round.rankControl.value();
    const attempt = round.guesses.length + 1;
    let result = await requestJSON("/api/challenge/guess", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        replayID: round.item.id,
        guessRank,
        attempt,
        mode,
        challengeDate,
        visitorID,
      }),
    });

    const actualEstimate = estimateActual(guessRank, result);
    const playerGuess = {
      guessRank,
      correct: Boolean(result.correct),
      direction: result.direction,
      logError: Number(result.logError) || 0,
    };

    const openingGuess = Number(round.botOpening || round.botGuesses?.[0]?.guessRank || result.botGuess) || 50_000;
    round.botOpening = openingGuess;
    const botGuessRank = chooseBotGuess(round, attempt, openingGuess);
    const botGuess = evaluateGuess(botGuessRank, actualEstimate);

    round.guesses.push(playerGuess);
    round.botGuesses.push(botGuess);

    const shouldReveal = playerGuess.correct || botGuess.correct || attempt >= MAX_ATTEMPTS;
    if (shouldReveal && !(Number(result.actualRank) > 0)) {
      result = await requestJSON("/api/challenge/guess", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          replayID: round.item.id,
          guessRank: clamp(Math.round(actualEstimate), 1, rankPopulation),
          attempt,
          mode,
          challengeDate,
          visitorID,
        }),
      });
    }

    if (shouldReveal) {
      round.revealed = true;
      round.actualRank = Number(result.actualRank) || clamp(Math.round(actualEstimate), 1, rankPopulation);
      round.predictedRank = Number(result.predictedRank) || openingGuess;
      round.player = result.player || round.player;
      round.distribution = result.distribution || round.distribution || null;
      round.winner = winnerFor(round);
    }

    updateChallengeRound(round, mode, challengeDate);
    if (mode === "daily") saveDailyState();
  };

  updateChallengeRound = function fixedUpdateChallengeRound(round, mode, challengeDate) {
    baseUpdateChallengeRound(round, mode, challengeDate);
    if (!round?.root) return;

    renderHistory(round);
    if (!round.revealed) return;

    const panel = round.root.querySelector(".reveal-panel");
    if (!panel) return;
    panel.hidden = false;
    panel.innerHTML = resultHTML(round, mode);
    const dialog = panel.querySelector(".result-dialog");
    requestAnimationFrame(() => dialog?.focus({ preventScroll: true }));
    panel.querySelector(".next-challenge")?.addEventListener("click", () => {
      document.body.classList.remove("result-open");
      if (mode === "daily") advanceDaily();
      else loadInfinite();
    });
  };
})();
