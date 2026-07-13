/* Final interaction and layout polish. Loaded after clean.js. */
(() => {
  const stageNames = ["replay", "submit", "render", "metadata", "rank"];
  let activeAnalysisStep = -1;

  const shortDate = (value) => {
    const date = new Date(`${value}T00:00:00Z`);
    if (Number.isNaN(date.getTime())) return String(value || "");
    return new Intl.DateTimeFormat(undefined, {
      month: "short",
      day: "numeric",
      timeZone: "UTC",
    }).format(date);
  };

  const ensureRound = (round) => {
    if (!round) return round;
    if (!Array.isArray(round.guesses)) round.guesses = [];
    if (!Array.isArray(round.botGuesses)) round.botGuesses = [];

    const nestedBots = [];
    round.guesses = round.guesses.map((guess, index) => {
      if (!guess || typeof guess !== "object") return guess;
      const { bot, ...playerGuess } = guess;
      nestedBots[index] = bot || null;
      return playerGuess;
    });

    if (!round.botGuesses.length && nestedBots.some(Boolean)) {
      round.botGuesses = nestedBots;
    }
    while (round.botGuesses.length < round.guesses.length) round.botGuesses.push(null);
    return round;
  };

  const playerRange = (round) => {
    let lower = 1;
    let upper = rankPopulation;
    for (const guess of round.guesses || []) {
      const value = clamp(Math.round(Number(guess?.guessRank) || 1), 1, rankPopulation);
      if (guess?.correct && Number(round.actualRank) > 0) {
        lower = Number(round.actualRank);
        upper = Number(round.actualRank);
        break;
      }
      if (guess?.direction === "better") upper = Math.min(upper, Math.max(1, value - 1));
      if (guess?.direction === "worse") lower = Math.max(lower, Math.min(rankPopulation, value + 1));
    }
    if (round.revealed && Number(round.actualRank) > 0) lower = upper = Number(round.actualRank);
    if (lower > upper) lower = upper = clamp(Math.round((lower + upper) / 2), 1, rankPopulation);
    return { lower, upper };
  };

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
    const turns = round.guesses.map((guess, index) => {
      const botGuess = round.botGuesses[index];
      return `<div class="result-turn">
        <span>${String(index + 1).padStart(2, "0")}</span>
        <div><small>you</small><strong>${formatRank(guess?.guessRank)}</strong></div>
        <div><small>rankbot</small><strong>${formatRank(botGuess?.guessRank)}</strong></div>
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

  rankControlHTML = function polishedRankControl(initialRank = 50_000) {
    const position = rankToSoftPosition(initialRank);
    const ranks = [...new Set([
      1,
      1_000,
      10_000,
      100_000,
      1_000_000,
      rankPopulation,
    ].filter((rank) => rank <= rankPopulation))];
    const ticks = ranks.map((rank) => {
      const left = rankToSoftPosition(rank) / SLIDER_STEPS * 100;
      return `<button type="button" class="slider-tick" data-rank="${rank}" style="left:${left.toFixed(3)}%">${compactRank(rank)}</button>`;
    }).join("");

    return `<div class="rank-control polish-rank">
      <div class="polish-rank-head">
        <div class="polish-live-rank"><span>your guess</span><strong class="live-rank">${formatRank(initialRank)}</strong></div>
        <label class="rank-number-label"><span>exact rank</span><div class="rank-number-shell"><b>#</b><input class="rank-number-input" type="number" min="1" max="${rankPopulation}" inputmode="numeric" value="${initialRank}" required /></div></label>
      </div>
      <div class="rank-slider-shell game-slider-shell">
        <span class="rank-range-mask rank-range-mask-left" aria-hidden="true"></span>
        <span class="rank-known-range" aria-hidden="true"></span>
        <span class="rank-range-mask rank-range-mask-right" aria-hidden="true"></span>
        <span class="rank-slider-fill" aria-hidden="true"></span>
        <input class="rank-slider" type="range" min="0" max="${SLIDER_STEPS}" step="1" value="${position}" aria-label="Rank guess" />
      </div>
      <div class="slider-scale" aria-label="Rank shortcuts">${ticks}</div>
    </div>`;
  };

  challengeCardHTML = function polishedChallengeCard(item, label) {
    const map = item.beatmap || {};
    const title = `${map.artist ? `${map.artist} — ` : ""}${map.title || "Unknown map"}`;
    const stats = `${map.version || "Unknown difficulty"} · ${Number(item.star || 0).toFixed(2)}★ · ${Number(item.accuracyPercent || 0).toFixed(2)}% · ${(item.mods || ["NM"]).join("")}`;
    const pips = Array.from({ length: MAX_ATTEMPTS }, () => "<i></i>").join("");

    return `<div class="challenge-shell game-shell duel-shell clean-shell polish-shell">
      <div class="challenge-topline polish-topline">
        <div class="round-meta"><span class="mode-chip">${escapeHTML(label)}</span></div>
        <div class="attempt-stack"><span class="attempt-copy">rankbot locked · turn 1 of ${MAX_ATTEMPTS}</span><div class="attempt-pips" aria-label="Turns">${pips}</div></div>
      </div>
      <div class="clean-mapline polish-mapline"><strong>${escapeHTML(title)}</strong><span>${escapeHTML(stats)}</span></div>
      <div class="polish-stage">
        <section class="polish-video-card">
          <div class="video-wrap" data-video-state="loading">
            <video class="challenge-video" src="${escapeHTML(item.videoURL)}" muted autoplay loop playsinline preload="auto" tabindex="0" aria-label="Replay video. Click to pause or play."></video>
            <button class="video-toggle" type="button" aria-label="Toggle sound">sound off</button>
            <span class="video-playback-hint" aria-live="polite"></span>
          </div>
        </section>
        <aside class="challenge-side polish-history">
          <div class="history-head"><strong>guesses</strong></div>
          <ol class="guess-list duel-turn-list" aria-label="Turn history"></ol>
        </aside>
      </div>
      <div class="guess-dock clean-dock polish-dock"><div class="guess-zone"><form class="guess-form polish-form">${rankControlHTML()}<div class="guess-actions"><button class="primary-button guess-submit" type="submit">lock guess</button></div></form><p class="challenge-error" hidden></p><div class="reveal-panel clean-reveal" hidden></div></div></div>
    </div>`;
  };

  bindChallengeVideo = function clickToToggle(root) {
    const video = root.querySelector(".challenge-video");
    const sound = root.querySelector(".video-toggle");
    const hint = root.querySelector(".video-playback-hint");
    const wrap = root.querySelector(".video-wrap");
    if (!video || !sound || !wrap) return;

    let hintTimer = 0;
    let userPaused = false;
    video.autoplay = true;
    video.defaultMuted = true;
    video.muted = true;
    video.loop = true;
    sound.textContent = "sound off";

    const showHint = (text, persist = false) => {
      if (!hint) return;
      window.clearTimeout(hintTimer);
      hint.textContent = text;
      hint.classList.add("visible");
      if (!persist) hintTimer = window.setTimeout(() => hint.classList.remove("visible"), 650);
    };

    const syncState = () => {
      const state = video.paused ? "paused" : "playing";
      wrap.dataset.videoState = state;
      video.setAttribute("aria-label", `Replay video. Click to ${video.paused ? "play" : "pause"}.`);
    };

    const play = async (showFailure = false) => {
      if (userPaused) return false;
      try {
        await video.play();
        syncState();
        return true;
      } catch {
        syncState();
        if (showFailure) showHint("click to play", true);
        return false;
      }
    };

    const togglePlayback = async () => {
      if (video.paused) {
        userPaused = false;
        const started = await play(true);
        if (started) showHint("playing");
      } else {
        userPaused = true;
        video.pause();
        syncState();
        showHint("paused");
      }
    };

    video.addEventListener("click", togglePlayback);
    video.addEventListener("keydown", (event) => {
      if (!["Enter", " "].includes(event.key)) return;
      event.preventDefault();
      togglePlayback();
    });
    video.addEventListener("play", syncState);
    video.addEventListener("pause", syncState);
    video.addEventListener("loadeddata", () => play(true), { once: true });
    video.addEventListener("error", () => showHint("video unavailable", true));

    sound.addEventListener("click", async () => {
      video.muted = !video.muted;
      sound.textContent = video.muted ? "sound off" : "sound on";
      sound.classList.toggle("on", !video.muted);
      if (video.paused) {
        userPaused = false;
        await play(true);
      }
    });

    requestAnimationFrame(() => play(true));
  };

  const renderTurns = (round) => round.guesses.map((guess, index) => {
    const botGuess = round.botGuesses[index];
    const bot = round.revealed && botGuess
      ? `<div class="duel-guess duel-bot"><i>rankbot</i><strong>${formatRank(botGuess.guessRank)}</strong></div>`
      : "";
    return `<li class="duel-turn ${guess?.correct ? "player-hit" : ""} ${round.revealed && botGuess?.correct ? "bot-hit" : ""}">
      <div class="duel-turn-values">
        <div class="duel-guess duel-player"><i>you</i><strong>${formatRank(guess?.guessRank)}</strong></div>
        ${bot}
      </div>
      <span class="duel-turn-number">${String(index + 1).padStart(2, "0")}</span>
    </li>`;
  }).join("");

  const paintPlayerRange = (round) => {
    const root = round.root;
    if (!root) return;
    const { lower, upper } = playerRange(round);
    const left = rankToSoftPosition(lower) / SLIDER_STEPS * 100;
    const right = rankToSoftPosition(upper) / SLIDER_STEPS * 100;
    const width = Math.max(0.5, right - left);
    const rangeLeft = Math.min(left, Math.max(0, 100 - width));
    const range = root.querySelector(".rank-known-range");
    const leftMask = root.querySelector(".rank-range-mask-left");
    const rightMask = root.querySelector(".rank-range-mask-right");
    const shell = root.querySelector(".rank-slider-shell");
    if (range) {
      range.style.left = `${rangeLeft.toFixed(3)}%`;
      range.style.width = `${width.toFixed(3)}%`;
    }
    if (leftMask) leftMask.style.width = `${left.toFixed(3)}%`;
    if (rightMask) rightMask.style.width = `${Math.max(0, 100 - right).toFixed(3)}%`;
    if (shell) shell.setAttribute("aria-label", `Possible rank range ${formatRank(lower)} to ${formatRank(upper)}`);
    root.querySelectorAll(".slider-tick").forEach((tick) => {
      const rank = Number(tick.dataset.rank);
      tick.classList.toggle("outside-range", rank < lower || rank > upper);
    });
  };

  submitChallengeGuess = async function polishedSubmitGuess(round, mode, challengeDate) {
    ensureRound(round);
    const guessRank = round.rankControl.value();
    const attempt = round.guesses.length + 1;
    const result = await requestJSON("/api/challenge/guess", {
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

    round.guesses.push({
      guessRank,
      correct: Boolean(result.correct),
      direction: result.direction,
      logError: Number(result.logError) || 0,
    });
    round.botGuesses.push({
      guessRank: Number(result.botGuess) || 1,
      correct: Boolean(result.botCorrect),
      direction: result.botDirection,
      logError: Number(result.botLogError) || 0,
    });

    if (result.revealed) {
      round.revealed = true;
      round.actualRank = result.actualRank;
      round.predictedRank = result.predictedRank;
      round.player = result.player;
      round.distribution = result.distribution || null;
      round.winner = result.turnWinner && result.turnWinner !== "pending"
        ? result.turnWinner
        : winnerFor(round);
    }

    updateChallengeRound(round, mode, challengeDate);
    if (mode === "daily") saveDailyState();
  };

  updateChallengeRound = function polishedUpdateChallengeRound(round, mode, challengeDate) {
    ensureRound(round);
    const root = round.root;
    if (!root) return;
    const turns = round.guesses.length;
    const list = root.querySelector(".guess-list");
    if (list) list.innerHTML = renderTurns(round);

    const attemptCopy = root.querySelector(".attempt-copy");
    if (attemptCopy) attemptCopy.textContent = round.revealed
      ? "round over"
      : `rankbot locked · turn ${turns + 1} of ${MAX_ATTEMPTS}`;

    [...root.querySelectorAll(".attempt-pips i")].forEach((pip, index) => {
      pip.classList.toggle("used", index < turns);
      pip.classList.toggle("hit", Boolean(round.guesses[index]?.correct || round.botGuesses[index]?.correct));
    });

    paintPlayerRange(round);
    const form = root.querySelector(".guess-form");
    if (form) form.hidden = Boolean(round.revealed);
    const button = form?.querySelector(".guess-submit");
    if (button && !round.revealed) button.textContent = "lock guess";

    const panel = root.querySelector(".reveal-panel");
    if (!panel) return;
    panel.hidden = !round.revealed;
    document.body.classList.toggle("result-open", Boolean(round.revealed));
    if (!round.revealed) return;

    panel.innerHTML = resultHTML(round, mode);
    const dialog = panel.querySelector(".result-dialog");
    requestAnimationFrame(() => dialog?.focus({ preventScroll: true }));
    panel.querySelector(".next-challenge")?.addEventListener("click", () => {
      document.body.classList.remove("result-open");
      if (mode === "daily") advanceDaily();
      else loadInfinite();
    });
  };

  mountChallenge = function polishedMountChallenge(rootElement, item, round, label, mode, challengeDate = null) {
    rootElement.innerHTML = challengeCardHTML(item, label);
    round.root = rootElement;
    ensureRound(round);
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
        if (!round.revealed) button.textContent = "lock guess";
      }
    });

    updateChallengeRound(round, mode, challengeDate);
  };

  saveDailyState = function polishedSaveDailyState() {
    if (!dailyPayload || !dailyState) return;
    const serializable = {
      current: dailyState.current,
      rounds: dailyState.rounds.map((round) => {
        ensureRound(round);
        return {
          id: round.item.id,
          guesses: round.guesses,
          botGuesses: round.botGuesses,
          revealed: round.revealed,
          actualRank: round.actualRank,
          predictedRank: round.predictedRank,
          player: round.player,
          distribution: round.distribution,
          winner: round.winner,
        };
      }),
    };
    storage.set(dailyStorageKey(), JSON.stringify(serializable));
  };

  restoreDailyState = function polishedRestoreDailyState(payload) {
    let saved = null;
    try { saved = JSON.parse(storage.get(dailyStorageKey(payload.date)) || "null"); } catch { saved = null; }
    const rounds = payload.replays.map((item) => {
      const previous = saved?.rounds?.find((round) => round.id === item.id) || {};
      return ensureRound({
        item,
        guesses: previous.guesses || [],
        botGuesses: previous.botGuesses || [],
        revealed: Boolean(previous.revealed),
        actualRank: previous.actualRank,
        predictedRank: previous.predictedRank,
        player: previous.player,
        distribution: previous.distribution || null,
        winner: previous.winner || null,
      });
    });
    return { current: clamp(Number(saved?.current) || 0, 0, rounds.length), rounds };
  };

  renderDaily = function polishedDaily() {
    const root = document.querySelector("#dailyRoot");
    if (!root || !dailyState || !dailyPayload) return;
    if (dailyState.current >= dailyState.rounds.length) return renderDailySummary();

    document.body.classList.remove("result-open");
    root.innerHTML = '<div id="dailyChallengeMount"></div>';
    const round = dailyState.rounds[dailyState.current];
    mountChallenge(
      document.querySelector("#dailyChallengeMount"),
      round.item,
      round,
      "daily",
      "daily",
      dailyPayload.date,
    );

    const meta = root.querySelector(".round-meta");
    if (meta) {
      meta.innerHTML = `<time datetime="${escapeHTML(dailyPayload.date)}">${escapeHTML(shortDate(dailyPayload.date))}</time><nav class="daily-inline" aria-label="Daily replay">${dailyState.rounds.map((candidate, index) => `<button type="button" data-daily-index="${index}" class="${candidate.revealed ? "done" : index === dailyState.current ? "current" : ""}" ${index > dailyState.current ? "disabled" : ""}>${index + 1}</button>`).join("")}</nav>`;
    }

    root.querySelectorAll("[data-daily-index]").forEach((button) => {
      button.addEventListener("click", () => {
        dailyState.current = Number(button.dataset.dailyIndex);
        saveDailyState();
        renderDaily();
      });
    });
  };

  renderDailySummary = function polishedDailySummary() {
    document.body.classList.remove("result-open");
    const root = document.querySelector("#dailyRoot");
    if (!root || !dailyState || !dailyPayload) return;
    const grid = dailyState.rounds.map((round) => {
      const solvedAt = round.guesses.findIndex((guess) => guess.correct);
      if (solvedAt < 0) return "⬛⬛⬛⬛⬛";
      return `${"⬛".repeat(solvedAt)}🟩${"⬜".repeat(4 - solvedAt)}`;
    }).join("\n");
    let wins = 0;
    let losses = 0;
    let ties = 0;
    for (const round of dailyState.rounds) {
      ensureRound(round);
      const winner = winnerFor(round);
      if (winner === "player") wins += 1;
      else if (winner === "bot") losses += 1;
      else ties += 1;
    }

    root.innerHTML = `<section class="daily-summary">
      <p class="kicker">${escapeHTML(dailyPayload.date)}</p>
      <h1>daily complete.</h1>
      <div class="daily-battle-summary"><span>you vs rankbot</span><strong>${wins}–${losses}</strong><em>${ties ? `${ties} tie` : wins > losses ? "you win the set" : "rankbot wins"}</em></div>
      <div class="share-grid" aria-label="Daily result">${grid.replaceAll("\n", "<br>")}</div>
      <div class="summary-table">${dailyState.rounds.map((round, index) => `<div><span>${index + 1}</span><b>${formatRank(round.actualRank)}</b><em>${round.guesses.length} guess${round.guesses.length === 1 ? "" : "es"}</em></div>`).join("")}</div>
      <button class="primary-button narrow" id="shareDaily" type="button">share result</button>
    </section>`;

    document.querySelector("#shareDaily")?.addEventListener("click", async () => {
      const text = `osu!rankguess ${dailyPayload.date}\n${grid}\nhttps://osu-rankguess.vercel.app/#daily`;
      try {
        if (navigator.share) await navigator.share({ text });
        else await copyText(text);
        document.querySelector("#shareDaily").textContent = navigator.share ? "shared" : "copied";
      } catch {
        await copyText(text);
        document.querySelector("#shareDaily").textContent = "copied";
      }
    });
  };

  const processCard = document.querySelector(".process-card");
  if (processCard) {
    processCard.innerHTML = `<div class="analysis-progress">
      <div class="analysis-progress-head"><span>analysis</span><strong id="analysisPercent">0%</strong></div>
      <div class="analysis-progress-track" aria-hidden="true"><i id="analysisProgressBar"></i></div>
      <p id="analysisProgressDetail">choose a replay to begin</p>
      <div class="analysis-stage-list" aria-label="Analysis progress">${stageNames.map((name, index) => `<span data-analysis-step="${index}"><i></i>${name}</span>`).join("")}</div>
    </div>`;
    processCard.appendChild(renderStatus);

    const updateAnalysis = (index, state, detail) => {
      activeAnalysisStep = index;
      const percent = index < 0
        ? 0
        : state === "done"
          ? Math.round((index + 1) / stageNames.length * 100)
          : Math.round((index + 0.35) / stageNames.length * 100);
      const bar = document.querySelector("#analysisProgressBar");
      const percentNode = document.querySelector("#analysisPercent");
      const detailNode = document.querySelector("#analysisProgressDetail");
      if (bar) bar.style.width = `${Math.max(0, Math.min(100, percent))}%`;
      if (percentNode) percentNode.textContent = `${Math.max(0, Math.min(100, percent))}%`;
      if (detailNode) detailNode.textContent = detail || (index < 0 ? "choose a replay to begin" : stageNames[index]);
      document.querySelectorAll("[data-analysis-step]").forEach((node) => {
        const step = Number(node.dataset.analysisStep);
        node.classList.toggle("done", step < index || (step === index && state === "done"));
        node.classList.toggle("active", step === index && state === "active");
        node.classList.toggle("failed", step === index && state === "failed");
      });
    };

    resetSteps = function polishedResetSteps() {
      renderStatus.hidden = true;
      renderStatus.textContent = "";
      updateAnalysis(-1, "idle", "choose a replay to begin");
    };

    setStep = function polishedSetStep(index, state, detail) {
      renderStatus.hidden = state !== "active";
      if (detail) renderStatus.textContent = detail;
      updateAnalysis(index, state, detail);
    };

    failCurrentStep = function polishedFailStep(message) {
      renderStatus.hidden = false;
      renderStatus.textContent = message;
      updateAnalysis(Math.max(0, activeAnalysisStep), "failed", message);
    };

    const observer = new MutationObserver(() => {
      if (renderStatus.hidden || !renderStatus.textContent.trim()) return;
      const detail = document.querySelector("#analysisProgressDetail");
      if (detail) detail.textContent = renderStatus.textContent.trim();
    });
    observer.observe(renderStatus, { childList: true, characterData: true, subtree: true, attributes: true });
    resetSteps();
  }
})();
