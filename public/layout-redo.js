/* Final challenge/analyze layout and playback behavior. */
(() => {
  const originalShowView = showView;
  const originalRenderPrediction = renderPrediction;
  const originalSetFile = setFile;
  const originalUpdateChallengeRound = updateChallengeRound;
  const RULE_TEXT = "adaptive window · precise near the top, tighter through the long tail";

  const pauseAllVideos = ({ unloadDialog = false } = {}) => {
    document.querySelectorAll("video").forEach((video) => { try { video.pause(); } catch {} });
    if (unloadDialog) {
      const video = document.querySelector("#galleryDialog video");
      if (video) { video.removeAttribute("src"); video.load?.(); }
    }
  };

  const autoplayVideo = async (video, sound = true) => {
    if (!video) return false;
    video.autoplay = true;
    video.playsInline = true;
    video.muted = !sound;
    try {
      await video.play();
      video.dataset.autoplayFallback = "";
      return true;
    } catch {
      if (!sound) return false;
      video.muted = true;
      video.dataset.autoplayFallback = "muted";
      try { await video.play(); return true; } catch { return false; }
    }
  };

  const activeVideo = () => {
    const view = document.body.dataset.view || "daily";
    if (view === "gallery" && document.querySelector("#galleryDialog[open]")) return document.querySelector("#galleryDialog video");
    if (view === "analyze" && !results.hidden) return document.querySelector("#replayVideo");
    return document.querySelector(`.view[data-view="${view}"] .challenge-video`);
  };

  const restorePlayback = () => {
    const video = activeVideo();
    if (video) autoplayVideo(video, true).catch(() => {});
  };

  const fitAnalyzeRank = () => {
    const rank = document.querySelector("#predictedRank");
    const column = rank?.closest(".rank-result");
    if (!rank || !column || results.hidden) return;
    rank.style.fontSize = "";
    const available = Math.max(1, column.clientWidth - 2);
    let size = parseFloat(getComputedStyle(rank).fontSize) || 72;
    while (rank.scrollWidth > available && size > 28) {
      size -= 2;
      rank.style.fontSize = `${size}px`;
    }
  };
  const scheduleFit = () => requestAnimationFrame(() => requestAnimationFrame(fitAnalyzeRank));

  window.rankguessUI = { pauseAllVideos, autoplayVideo, restorePlayback };

  document.addEventListener("pointerdown", () => {
    const video = activeVideo();
    if (video && (video.paused || video.dataset.autoplayFallback === "muted")) autoplayVideo(video, true).catch(() => {});
  }, { passive: true });

  showView = function finalShowView(name) {
    pauseAllVideos({ unloadDialog: name !== "gallery" });
    originalShowView(name);
    const hasResult = name === "analyze" && !results.hidden;
    document.body.classList.toggle("analyze-results", hasResult);
    if (hasResult) { scheduleFit(); requestAnimationFrame(restorePlayback); }
  };

  setFile = function finalSetFile(file) {
    document.body.classList.remove("analyze-results");
    return originalSetFile(file);
  };

  renderPrediction = function finalRenderPrediction(payload) {
    originalRenderPrediction(payload);
    document.body.classList.add("analyze-results");
    scheduleFit();
    const video = document.querySelector("#replayVideo");
    if (video) { video.setAttribute("autoplay", ""); autoplayVideo(video, true).catch(() => {}); }
  };

  document.querySelector("#resetButton")?.addEventListener("click", () => {
    document.body.classList.remove("analyze-results");
    pauseAllVideos();
  });

  rankControlHTML = function finalRankControl(initialRank = 50_000) {
    const position = rankToSoftPosition(initialRank);
    const ranks = [...new Set([1, 1_000, 10_000, 100_000, 1_000_000, rankPopulation].filter((rank) => rank <= rankPopulation))];
    const ticks = ranks.map((rank) => `<button type="button" class="slider-tick" data-rank="${rank}" style="left:${(rankToSoftPosition(rank) / SLIDER_STEPS * 100).toFixed(3)}%">${compactRank(rank)}</button>`).join("");
    return `<div class="rank-control">
      <div class="guess-readout">
        <div class="guess-value"><span>your guess</span><strong class="live-rank">${formatRank(initialRank)}</strong></div>
        <label class="rank-number-label"><span>exact rank</span><div class="rank-number-shell"><b>#</b><input class="rank-number-input" type="number" min="1" max="${rankPopulation}" inputmode="numeric" value="${initialRank}" required /></div></label>
      </div>
      <div class="rank-slider-shell"><span class="rank-slider-fill" aria-hidden="true"></span><input class="rank-slider" type="range" min="0" max="${SLIDER_STEPS}" step="1" value="${position}" aria-label="Soft logarithmic rank guess slider" /></div>
      <div class="slider-scale" aria-label="Rank shortcuts">${ticks}</div>
    </div>`;
  };

  challengeCardHTML = function finalChallengeCard(item, label) {
    const map = item.beatmap || {};
    const title = `${map.artist ? `${map.artist} — ` : ""}${map.title || "Unknown map"}`;
    const stats = `${map.version || "Unknown difficulty"} · ${Number(item.star || 0).toFixed(2)}★ · ${Number(item.accuracyPercent || 0).toFixed(2)}% · ${(item.mods || ["NM"]).join("")}`;
    const prefetch = String(label).startsWith("infinite") ? '<span class="infinite-prefetch-status" data-state="loading">loading next replay</span>' : "";
    return `<div class="challenge-shell">
      <div class="challenge-topline"><span>${escapeHTML(label)}</span><span class="attempt-copy">five guesses · adaptive window</span></div>
      <div class="challenge-content">
        <div class="video-column"><div class="video-wrap"><video class="challenge-video" src="${escapeHTML(item.videoURL)}" autoplay loop playsinline preload="auto"></video><button class="video-toggle on" type="button" aria-label="Toggle sound">${ICON_SOUND}</button><button class="video-play" type="button" aria-label="Play or pause replay">pause</button></div></div>
        <aside class="challenge-side"><div class="stage-info"><strong>${escapeHTML(title)}</strong><span>${escapeHTML(stats)}</span>${prefetch}</div><ol class="guess-list" aria-label="Previous guesses"></ol></aside>
      </div>
      <div class="guess-dock"><div class="guess-zone"><form class="guess-form">${rankControlHTML()}<button class="primary-button guess-submit" type="submit">submit guess</button></form><p class="challenge-rule">${RULE_TEXT}</p><p class="challenge-error" hidden></p><div class="reveal-panel" hidden></div></div></div>
    </div>`;
  };

  bindChallengeVideo = function finalBindVideo(root) {
    const video = root.querySelector(".challenge-video");
    const sound = root.querySelector(".video-toggle");
    const play = root.querySelector(".video-play");
    if (!video) return;
    const sync = () => {
      if (play) play.textContent = video.paused ? "play" : "pause";
      if (sound) { sound.innerHTML = video.muted ? ICON_MUTED : ICON_SOUND; sound.classList.toggle("on", !video.muted); }
    };
    const toggle = () => { if (video.paused) autoplayVideo(video, !video.muted).catch(() => {}); else video.pause(); sync(); };
    video.addEventListener("click", toggle);
    play?.addEventListener("click", toggle);
    video.addEventListener("play", sync);
    video.addEventListener("pause", sync);
    sound?.addEventListener("click", (event) => { event.stopPropagation(); video.muted = !video.muted; if (!video.muted && video.paused) autoplayVideo(video, true).catch(() => {}); sync(); });
    autoplayVideo(video, true).finally(sync);
  };

  updateChallengeRound = function finalUpdateRound(round, mode, challengeDate) {
    originalUpdateChallengeRound(round, mode, challengeDate);
    const copy = round.root?.querySelector(".attempt-copy");
    if (!copy) return;
    const left = Math.max(0, MAX_ATTEMPTS - round.guesses.length);
    copy.textContent = round.revealed ? "answer revealed" : `${left} guess${left === 1 ? "" : "es"} left · adaptive window`;
  };

  document.addEventListener("visibilitychange", () => { if (document.hidden) pauseAllVideos(); });
  window.addEventListener("blur", () => pauseAllVideos());
  window.addEventListener("focus", restorePlayback);
  window.addEventListener("resize", scheduleFit, { passive: true });
  window.addEventListener("pagehide", () => pauseAllVideos({ unloadDialog: true }));
})();
