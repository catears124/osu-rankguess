/* Rank-aware challenge acceptance rule. Loaded after refine.js. */
(() => {
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

  submitChallengeGuess = async function rankAwareSubmit(round, mode, challengeDate) {
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
    } else {
      const factor = result.direction === "better" ? 0.58 : 1.72;
      round.rankControl.setRank(clamp(Math.round(guessRank * factor), 1, rankPopulation));
    }
    updateChallengeRound(round, mode, challengeDate);
    if (mode === "daily") saveDailyState();
  };
})();
