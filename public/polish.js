/* Final interaction and layout polish. Loaded after clean.js. */
(() => {
  const stageNames = ["replay", "submit", "render", "metadata", "rank"];
  const baseUpdateChallengeRound = updateChallengeRound;
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
    const botRank = round.botGuesses?.at(-1)?.guessRank || round.predictedRank;
    const heading = winner === "player" ? "you win" : winner === "bot" ? "rankbot wins" : "tie";
    const comparison = winner === "player"
      ? "closer than rankbot"
      : winner === "bot"
        ? "rankbot was closer"
        : "same error";

    return `<div class="polish-result ${winner}">
      <div class="result-rank"><span>actual rank</span><strong>${formatRank(round.actualRank)}</strong><small>${escapeHTML(round.player || "player")}</small></div>
      <div class="result-outcome"><span>${heading}</span><strong>${comparison}</strong><small>you ${ratioText(playerRatio)} · rankbot ${ratioText(botRatio)}${Number(botRank) > 0 ? ` · ${formatRank(botRank)}` : ""}</small></div>
      <button class="primary-button next-challenge" type="button">${mode === "daily" ? "next" : "next replay"}</button>
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
      <div class="polish-range-copy"><span>possible range</span><strong class="known-range-text">${formatRank(1)} – ${formatRank(rankPopulation)}</strong></div>
      <div class="rank-slider-shell game-slider-shell">
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
        <div class="attempt-stack"><span class="attempt-copy">turn 1 of ${MAX_ATTEMPTS}</span><div class="attempt-pips" aria-label="Turns">${pips}</div></div>
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
          <div class="history-head"><strong>guesses</strong><span>you / bot</span></div>
          <ol class="guess-list duel-turn-list" aria-label="Turn history"></ol>
        </aside>
      </div>
      <div class="guess-dock clean-dock polish-dock"><div class="guess-zone"><form class="guess-form polish-form">${rankControlHTML()}<div class="guess-actions"><p class="range-callout">pick a rank</p><button class="primary-button guess-submit" type="submit">lock guess</button></div></form><p class="challenge-error" hidden></p><div class="reveal-panel clean-reveal" hidden></div></div></div>
    </div>`;
  };

  bindChallengeVideo = function clickToToggle(root) {
    const video = root.querySelector(".challenge-video");
    const sound = root.querySelector(".video-toggle");
    const hint = root.querySelector(".video-playback-hint");
    const wrap = root.querySelector(".video-wrap");
    if (!video || !sound || !wrap) return;

    let hintTimer = 0;
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
        const started = await play(true);
        if (started) showHint("playing");
      } else {
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
      if (video.paused) await play(true);
    });

    requestAnimationFrame(() => play(true));
  };

  updateChallengeRound = function polishedUpdateChallengeRound(round, mode, challengeDate) {
    baseUpdateChallengeRound(round, mode, challengeDate);
    if (!round?.revealed || !round.root) return;

    const panel = round.root.querySelector(".reveal-panel");
    if (!panel) return;
    panel.hidden = false;
    panel.innerHTML = resultHTML(round, mode);
    panel.querySelector(".next-challenge")?.addEventListener("click", () => {
      if (mode === "daily") advanceDaily();
      else loadInfinite();
    });
  };

  renderDaily = function polishedDaily() {
    const root = document.querySelector("#dailyRoot");
    if (!root || !dailyState || !dailyPayload) return;
    if (dailyState.current >= dailyState.rounds.length) return renderDailySummary();

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
