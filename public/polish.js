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
      <p class="range-callout">pick a rank</p>
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
          <div class="video-wrap"><video class="challenge-video" src="${escapeHTML(item.videoURL)}" muted loop playsinline preload="auto"></video></div>
          <div class="video-controls">
            <button class="video-play" type="button" aria-label="Hold to play replay">hold to play</button>
            <button class="video-toggle" type="button" aria-label="Toggle sound">sound off</button>
          </div>
        </section>
        <aside class="challenge-side polish-history">
          <div class="history-head"><strong>guesses</strong><span>you / bot</span></div>
          <ol class="guess-list duel-turn-list" aria-label="Turn history"></ol>
        </aside>
      </div>
      <div class="guess-dock clean-dock polish-dock"><div class="guess-zone"><form class="guess-form polish-form">${rankControlHTML()}<button class="primary-button guess-submit" type="submit">lock guess</button></form><p class="challenge-error" hidden></p><div class="reveal-panel clean-reveal" hidden></div></div></div>
    </div>`;
  };

  bindChallengeVideo = function holdToPlay(root) {
    const video = root.querySelector(".challenge-video");
    const play = root.querySelector(".video-play");
    const sound = root.querySelector(".video-toggle");
    if (!video || !play || !sound) return;

    let held = false;
    video.autoplay = false;
    video.muted = true;
    video.pause();
    play.textContent = "hold to play";
    sound.textContent = "sound off";

    const start = async () => {
      if (held) return;
      held = true;
      play.classList.add("holding");
      play.textContent = "playing";
      try { await video.play(); } catch {}
    };

    const stop = () => {
      if (!held) return;
      held = false;
      video.pause();
      play.classList.remove("holding");
      play.textContent = "hold to play";
    };

    play.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      try { play.setPointerCapture(event.pointerId); } catch {}
      start();
    });
    play.addEventListener("pointerup", stop);
    play.addEventListener("pointercancel", stop);
    play.addEventListener("lostpointercapture", stop);
    play.addEventListener("contextmenu", (event) => event.preventDefault());
    play.addEventListener("keydown", (event) => {
      if (event.repeat || !["Enter", " "].includes(event.key)) return;
      event.preventDefault();
      start();
    });
    play.addEventListener("keyup", (event) => {
      if (!["Enter", " "].includes(event.key)) return;
      event.preventDefault();
      stop();
    });
    play.addEventListener("blur", stop);
    window.addEventListener("pointerup", stop, { passive: true });

    video.addEventListener("play", () => {
      if (!held) video.pause();
    });
    sound.addEventListener("click", () => {
      video.muted = !video.muted;
      sound.textContent = video.muted ? "sound off" : "sound on";
      sound.classList.toggle("on", !video.muted);
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
