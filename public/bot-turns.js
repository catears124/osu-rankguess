/* Turn-by-turn player vs rankbot duel. Loaded after game.js. */
(() => {
  const previousRenderDailySummary = renderDailySummary;

  const ensureRound = (round) => {
    if (!Array.isArray(round.guesses)) round.guesses = [];
    if (!Array.isArray(round.botGuesses)) round.botGuesses = [];
    return round;
  };

  const rankRatio = (left, right) => Math.max(left, right) / Math.max(1, Math.min(left, right));

  const feedbackLabel = (guess) => {
    if (!guess) return "waiting";
    if (guess.correct) return "hit";
    const heat = guess.closeness === "very_close" ? "hot" : guess.closeness === "close" ? "warm" : "cold";
    return `${heat} · ${guess.direction === "better" ? "go left" : "go right"}`;
  };

  const sharedRange = (round) => {
    let lower = 1;
    let upper = rankPopulation;
    const all = [...(round.guesses || []), ...(round.botGuesses || [])];
    for (const guess of all) {
      const value = clamp(Math.round(Number(guess.guessRank) || 1), 1, rankPopulation);
      if (guess.correct && round.revealed && Number(round.actualRank) > 0) {
        lower = Number(round.actualRank);
        upper = Number(round.actualRank);
        break;
      }
      if (guess.direction === "better") upper = Math.min(upper, Math.max(1, value - 1));
      if (guess.direction === "worse") lower = Math.max(lower, Math.min(rankPopulation, value + 1));
    }
    if (round.revealed && Number(round.actualRank) > 0) lower = upper = Number(round.actualRank);
    if (lower > upper) {
      const midpoint = clamp(Math.round((lower + upper) / 2), 1, rankPopulation);
      lower = upper = midpoint;
    }
    return { lower, upper };
  };

  const bestRatio = (guesses, actualRank) => {
    if (!Number(actualRank) || !guesses?.length) return Infinity;
    return Math.min(...guesses.map((guess) => rankRatio(Number(guess.guessRank), Number(actualRank))));
  };

  const firstHit = (guesses) => guesses?.findIndex((guess) => guess.correct) ?? -1;

  const resolveWinner = (round) => {
    const playerHit = firstHit(round.guesses);
    const botHit = firstHit(round.botGuesses);
    if (playerHit >= 0 || botHit >= 0) {
      if (playerHit < 0) return "bot";
      if (botHit < 0) return "player";
      if (playerHit < botHit) return "player";
      if (botHit < playerHit) return "bot";
      const playerError = Number(round.guesses[playerHit]?.logError) || 0;
      const botError = Number(round.botGuesses[botHit]?.logError) || 0;
      if (Math.abs(playerError - botError) < 1e-9) return "tie";
      return playerError < botError ? "player" : "bot";
    }
    if (!round.revealed || !Number(round.actualRank)) return null;
    const playerRatio = bestRatio(round.guesses, round.actualRank);
    const botRatio = bestRatio(round.botGuesses, round.actualRank);
    if (Math.abs(playerRatio - botRatio) < 1e-9) return "tie";
    return playerRatio < botRatio ? "player" : "bot";
  };

  const pointsFor = (round) => {
    const ratio = bestRatio(round.guesses, round.actualRank);
    if (!Number.isFinite(ratio)) return 0;
    const speedBonus = Math.max(0, (MAX_ATTEMPTS - round.guesses.length) * 70);
    const winBonus = resolveWinner(round) === "player" ? 300 : 0;
    return Math.max(0, Math.round(1000 / Math.pow(ratio, 1.4) + speedBonus + winBonus));
  };

  challengeCardHTML = function duelChallengeCard(item, label) {
    const map = item.beatmap || {};
    const title = `${map.artist ? `${map.artist} — ` : ""}${map.title || "Unknown map"}`;
    const stats = `${map.version || "Unknown difficulty"} · ${Number(item.star || 0).toFixed(2)}★ · ${Number(item.accuracyPercent || 0).toFixed(2)}% · ${(item.mods || ["NM"]).join("")}`;
    const prefetch = String(label).startsWith("infinite")
      ? '<span class="infinite-prefetch-status" data-state="loading">loading next replay</span>'
      : "";
    const pips = Array.from({ length: MAX_ATTEMPTS }, () => "<i></i>").join("");

    return `<div class="challenge-shell game-shell duel-shell">
      <div class="challenge-topline game-topline">
        <span class="game-round">${escapeHTML(label)}</span>
        <div class="versus-banner"><strong>you</strong><i>vs</i><strong>rankbot</strong><span class="duel-status">turn 1 · both locked</span></div>
        <div class="attempt-stack"><span class="attempt-copy">${MAX_ATTEMPTS} turns left</span><div class="attempt-pips" aria-label="Turns remaining">${pips}</div></div>
      </div>
      <div class="challenge-content">
        <div class="video-column"><div class="video-wrap"><video class="challenge-video" src="${escapeHTML(item.videoURL)}" autoplay loop playsinline preload="auto"></video><button class="video-toggle on" type="button" aria-label="Toggle sound">${ICON_SOUND}</button><button class="video-play" type="button" aria-label="Play or pause replay">pause</button></div></div>
        <aside class="challenge-side">
          <div class="stage-info"><strong>${escapeHTML(title)}</strong><span>${escapeHTML(stats)}</span>${prefetch}</div>
          <div class="bot-lock"><span>rankbot</span><strong class="bot-lock-value">turn 1 locked</strong><small>guesses reveal together</small></div>
          <ol class="guess-list duel-turn-list" aria-label="Turn history"></ol>
        </aside>
      </div>
      <div class="guess-dock"><div class="guess-zone"><form class="guess-form">${rankControlHTML()}<button class="primary-button guess-submit" type="submit">lock turn 1</button></form><p class="challenge-rule">you and rankbot get the same higher/lower clues. first hit wins.</p><p class="challenge-error" hidden></p><div class="reveal-panel" hidden></div></div></div>
    </div>`;
  };

  submitChallengeGuess = async function duelSubmitGuess(round, mode, challengeDate) {
    ensureRound(round);
    const guessRank = round.rankControl.value();
    const attempt = round.guesses.length + 1;
    const status = round.root?.querySelector(".duel-status");
    const lockValue = round.root?.querySelector(".bot-lock-value");
    if (status) status.textContent = `turn ${attempt} · revealing…`;
    if (lockValue) lockValue.textContent = "thinking…";

    const request = requestJSON("/api/challenge/guess", {
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

    const [result] = await Promise.all([request, sleep(420)]);
    const playerGuess = {
      guessRank,
      correct: Boolean(result.correct),
      direction: result.direction,
      closeness: result.closeness,
      logError: Number(result.logError) || 0,
    };
    const botGuess = {
      guessRank: Number(result.botGuess) || 1,
      correct: Boolean(result.botCorrect),
      direction: result.botDirection,
      closeness: result.botCloseness,
      logError: Number(result.botLogError) || 0,
    };

    round.guesses.push(playerGuess);
    round.botGuesses.push(botGuess);
    if (result.revealed) {
      round.revealed = true;
      round.actualRank = result.actualRank;
      round.predictedRank = result.predictedRank;
      round.player = result.player;
      round.distribution = result.distribution || null;
      round.winner = result.turnWinner && result.turnWinner !== "pending"
        ? result.turnWinner
        : resolveWinner(round);
    }

    updateChallengeRound(round, mode, challengeDate);
    if (mode === "daily") saveDailyState();
  };

  const paintRange = (round) => {
    const root = round.root;
    if (!root) return;
    const { lower, upper } = sharedRange(round);
    const left = rankToSoftPosition(lower) / SLIDER_STEPS * 100;
    const right = rankToSoftPosition(upper) / SLIDER_STEPS * 100;
    const width = Math.max(0.35, right - left);
    const range = root.querySelector(".rank-known-range");
    const leftMask = root.querySelector(".rank-range-mask-left");
    const rightMask = root.querySelector(".rank-range-mask-right");
    const text = root.querySelector(".known-range-text");
    if (range) { range.style.left = `${left.toFixed(3)}%`; range.style.width = `${width.toFixed(3)}%`; }
    if (leftMask) leftMask.style.width = `${left.toFixed(3)}%`;
    if (rightMask) rightMask.style.width = `${Math.max(0, 100 - right).toFixed(3)}%`;
    if (text) text.textContent = lower === upper ? formatRank(lower) : `${formatRank(lower)} – ${formatRank(upper)}`;
    root.querySelectorAll(".slider-tick").forEach((tick) => {
      const rank = Number(tick.dataset.rank);
      tick.classList.toggle("outside-range", rank < lower || rank > upper);
    });
  };

  const renderTurns = (round) => round.guesses.map((playerGuess, index) => {
    const botGuess = round.botGuesses[index];
    const winnerClass = playerGuess.correct && botGuess?.correct
      ? "double-hit"
      : playerGuess.correct ? "player-hit" : botGuess?.correct ? "bot-hit" : "";
    return `<li class="duel-turn ${winnerClass}">
      <span class="duel-turn-number">${String(index + 1).padStart(2, "0")}</span>
      <div class="duel-guess duel-player"><i>you</i><strong>${formatRank(playerGuess.guessRank)}</strong><em>${escapeHTML(feedbackLabel(playerGuess))}</em></div>
      <div class="duel-guess duel-bot"><i>bot</i><strong>${formatRank(botGuess?.guessRank)}</strong><em>${escapeHTML(feedbackLabel(botGuess))}</em></div>
    </li>`;
  }).join("");

  const revealHTML = (round, mode) => {
    const winner = resolveWinner(round) || "tie";
    const playerRatio = bestRatio(round.guesses, round.actualRank);
    const botRatio = bestRatio(round.botGuesses, round.actualRank);
    const title = winner === "player" ? "you beat rankbot" : winner === "bot" ? "rankbot wins" : "dead even";
    const className = winner === "player" ? "player-won" : winner === "bot" ? "bot-won" : "tie";
    const community = mode === "daily" ? `<div class="reveal-community">${renderDistribution(round.distribution, round)}</div>` : "";
    return `<div class="battle-result ${className}"><div><span>${title}</span><strong>${pointsFor(round).toLocaleString()} pts</strong></div><p>you <b>${playerRatio.toFixed(2)}×</b> · bot <b>${botRatio.toFixed(2)}×</b></p></div>
      <div class="reveal-stats"><div class="reveal-answer"><span>actual rank</span><strong>${formatRank(round.actualRank)}</strong><small>${escapeHTML(round.player || "player")} · model opened ${formatRank(round.predictedRank)}</small></div>${community}</div>
      <button class="secondary-button next-challenge" type="button">${mode === "daily" ? "next replay" : "generate another"}</button>`;
  };

  updateChallengeRound = function duelUpdateRound(round, mode, challengeDate) {
    ensureRound(round);
    const root = round.root;
    if (!root) return;
    const turns = round.guesses.length;
    const left = Math.max(0, MAX_ATTEMPTS - turns);
    const winner = round.revealed ? resolveWinner(round) : null;

    const list = root.querySelector(".guess-list");
    if (list) list.innerHTML = renderTurns(round);
    const attemptCopy = root.querySelector(".attempt-copy");
    if (attemptCopy) attemptCopy.textContent = round.revealed ? "round over" : `${left} turn${left === 1 ? "" : "s"} left`;
    const status = root.querySelector(".duel-status");
    if (status) status.textContent = round.revealed
      ? (winner === "player" ? "you win" : winner === "bot" ? "rankbot wins" : "tie game")
      : `turn ${turns + 1} · both locked`;
    const lockValue = root.querySelector(".bot-lock-value");
    if (lockValue) lockValue.textContent = round.revealed
      ? `${formatRank(round.botGuesses.at(-1)?.guessRank)} · ${winner === "player" ? "you win" : winner === "bot" ? "bot wins" : "tie"}`
      : `turn ${turns + 1} locked`;

    [...root.querySelectorAll(".attempt-pips i")].forEach((pip, index) => {
      pip.classList.toggle("used", index < turns);
      pip.classList.toggle("hit", Boolean(round.guesses[index]?.correct || round.botGuesses[index]?.correct));
    });

    const lastPlayer = round.guesses.at(-1);
    const lastBot = round.botGuesses.at(-1);
    const callout = root.querySelector(".range-callout");
    if (callout) {
      if (!lastPlayer) callout.textContent = "watch the replay. rankbot already locked turn 1.";
      else if (round.revealed) callout.textContent = winner === "player" ? "you got closer first." : winner === "bot" ? "rankbot found it first." : "same turn, same error.";
      else callout.textContent = `you: ${feedbackLabel(lastPlayer)} · bot: ${feedbackLabel(lastBot)}`;
    }

    paintRange(round);
    const form = root.querySelector(".guess-form");
    if (form) form.hidden = Boolean(round.revealed);
    const button = form?.querySelector(".guess-submit");
    if (button && !round.revealed) button.textContent = `lock turn ${turns + 1}`;
    const panel = root.querySelector(".reveal-panel");
    if (panel) {
      panel.hidden = !round.revealed;
      if (round.revealed) {
        panel.innerHTML = revealHTML(round, mode);
        panel.querySelector(".next-challenge")?.addEventListener("click", () => mode === "daily" ? advanceDaily() : loadInfinite());
        if (mode === "daily" && !round.distribution) fetchDistribution(round, mode, challengeDate).catch(() => {});
      }
    }
  };

  saveDailyState = function duelSaveDailyState() {
    if (!dailyPayload || !dailyState) return;
    const serializable = {
      current: dailyState.current,
      rounds: dailyState.rounds.map(({ item, guesses, botGuesses, revealed, actualRank, predictedRank, player, distribution, winner }) => ({
        id: item.id, guesses, botGuesses, revealed, actualRank, predictedRank, player, distribution, winner,
      })),
    };
    storage.set(dailyStorageKey(), JSON.stringify(serializable));
  };

  restoreDailyState = function duelRestoreDailyState(payload) {
    let saved = null;
    try { saved = JSON.parse(storage.get(dailyStorageKey(payload.date)) || "null"); } catch { saved = null; }
    const rounds = payload.replays.map((item) => {
      const previous = saved?.rounds?.find((round) => round.id === item.id) || {};
      const compatible = !previous.guesses?.length || Array.isArray(previous.botGuesses);
      return {
        item,
        guesses: compatible ? previous.guesses || [] : [],
        botGuesses: compatible ? previous.botGuesses || [] : [],
        revealed: compatible && Boolean(previous.revealed),
        actualRank: compatible ? previous.actualRank : undefined,
        predictedRank: compatible ? previous.predictedRank : undefined,
        player: compatible ? previous.player : undefined,
        distribution: compatible ? previous.distribution || null : null,
        winner: compatible ? previous.winner || null : null,
      };
    });
    return { current: clamp(Number(saved?.current) || 0, 0, rounds.length), rounds };
  };

  renderDailySummary = function duelDailySummary() {
    previousRenderDailySummary();
    document.querySelectorAll("#dailyRoot .daily-battle-summary").forEach((element) => element.remove());
    const root = document.querySelector("#dailyRoot");
    const heading = root?.querySelector(".daily-summary h1");
    if (!root || !heading || !dailyState?.rounds) return;
    let wins = 0;
    let losses = 0;
    let ties = 0;
    let points = 0;
    for (const round of dailyState.rounds) {
      const winner = resolveWinner(round);
      if (winner === "player") wins += 1;
      else if (winner === "bot") losses += 1;
      else ties += 1;
      points += pointsFor(round);
    }
    const battle = document.createElement("div");
    battle.className = "daily-battle-summary";
    battle.innerHTML = `<span>you vs rankbot</span><strong>${wins}–${losses}</strong><em>${ties ? `${ties} tie · ` : ""}${points.toLocaleString()} pts</em>`;
    heading.after(battle);
  };
})();
