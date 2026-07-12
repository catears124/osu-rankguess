/* Post-redesign behavior refinements. Loaded after app.js. */
(() => {
  const RULE_TEXT = "adaptive window · generous near the top, tighter in the long tail";
  const originalShowView = showView;
  const originalRenderPrediction = renderPrediction;
  const originalReset = document.querySelector("#resetButton");
  let gallerySpoilersHidden = storage.get("osu-rankguess-gallery-spoilers") !== "shown";
  let infinitePrefetchPromise = null;
  let infinitePrefetchedPayload = null;
  let infinitePrefetchError = null;

  function pauseAllVideos({ unloadDialog = false } = {}) {
    document.querySelectorAll("video").forEach((video) => {
      try { video.pause(); } catch {}
    });
    if (unloadDialog) {
      const dialogVideo = document.querySelector("#galleryDialog video");
      if (dialogVideo) {
        dialogVideo.removeAttribute("src");
        dialogVideo.load?.();
      }
    }
  }

  showView = function refinedShowView(name) {
    pauseAllVideos({ unloadDialog: true });
    document.body.classList.remove("analyze-results");
    originalShowView(name);
  };

  renderPrediction = function refinedPrediction(payload) {
    originalRenderPrediction(payload);
    document.body.classList.add("analyze-results");
    const video = document.querySelector("#replayVideo");
    if (video) {
      video.muted = false;
      video.play?.().catch(() => {});
    }
  };

  originalReset?.addEventListener("click", () => {
    document.body.classList.remove("analyze-results");
    pauseAllVideos();
  });

  challengeCardHTML = function refinedChallengeCard(item, label) {
    const map = item.beatmap || {};
    const mapTitle = `${map.artist ? `${map.artist} — ` : ""}${map.title || "Unknown map"}`;
    const mapStats = `${map.version || "Unknown difficulty"} · ${Number(item.star || 0).toFixed(2)}★ · ${Number(item.accuracyPercent || 0).toFixed(2)}% · ${(item.mods || ["NM"]).join("")}`;
    const prefetch = String(label).startsWith("infinite")
      ? '<span class="infinite-prefetch-status" data-state="loading">preparing next clip</span>'
      : "";
    return `
      <div class="challenge-shell">
        <div class="challenge-stage">
          <div class="video-column">
            <div class="video-wrap">
              <video class="challenge-video" src="${escapeHTML(item.videoURL)}" autoplay loop playsinline preload="auto"></video>
              <button class="video-toggle on" type="button" aria-label="Toggle sound">${ICON_SOUND}</button>
              <button class="video-play" type="button" aria-label="Play or pause replay">pause</button>
            </div>
          </div>
          <aside class="challenge-panel">
            <div class="challenge-topline"><span>${escapeHTML(label)}</span><span class="attempt-copy">five guesses · adaptive window</span></div>
            <div class="stage-info"><strong>${escapeHTML(mapTitle)}</strong><span>${escapeHTML(mapStats)}</span>${prefetch}</div>
            <ol class="guess-list" aria-label="Previous guesses"></ol>
            <div class="guess-zone">
              <form class="guess-form">
                ${rankControlHTML()}
                <button class="primary-button guess-submit" type="submit">submit guess</button>
              </form>
              <p class="challenge-rule">${RULE_TEXT}</p>
              <p class="challenge-error" hidden></p>
              <div class="reveal-panel" hidden></div>
            </div>
          </aside>
        </div>
      </div>`;
  };

  bindChallengeVideo = function refinedBindVideo(root) {
    const video = root.querySelector(".challenge-video");
    const sound = root.querySelector(".video-toggle");
    const play = root.querySelector(".video-play");
    if (!video) return;
    video.muted = false;

    const sync = () => {
      if (play) play.textContent = video.paused ? "play" : "pause";
      if (sound) {
        sound.innerHTML = video.muted ? ICON_MUTED : ICON_SOUND;
        sound.classList.toggle("on", !video.muted);
      }
    };
    const togglePlay = () => {
      if (video.paused) video.play?.().catch(() => {});
      else video.pause();
      sync();
    };
    video.addEventListener("click", togglePlay);
    play?.addEventListener("click", togglePlay);
    video.addEventListener("play", sync);
    video.addEventListener("pause", sync);
    sound?.addEventListener("click", (event) => {
      event.stopPropagation();
      video.muted = !video.muted;
      if (!video.muted && video.paused) video.play?.().catch(() => {});
      sync();
    });
    video.play?.().catch(() => sync());
    sync();
  };

  function inferredActualRank(guessRank, result) {
    if (Number(result.actualRank) > 0) return Number(result.actualRank);
    const factor = 10 ** Math.max(0, Number(result.logError) || 0);
    return result.direction === "better" ? guessRank / factor : guessRank * factor;
  }

  function adaptiveAllowance(actualRank) {
    return 0.022 + 0.075 / Math.sqrt(1 + Math.max(1, actualRank) / 1000);
  }

  function adaptiveCloseness(logError, allowance, correct) {
    if (correct) return "exact";
    if (logError <= allowance * 1.8) return "very_close";
    if (logError <= allowance * 3.5) return "close";
    return "far";
  }

  submitChallengeGuess = async function refinedSubmitGuess(round, mode, challengeDate) {
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

    const actualEstimate = inferredActualRank(guessRank, result);
    const allowance = adaptiveAllowance(actualEstimate);
    const logError = Math.max(0, Number(result.logError) || 0);
    const adaptiveCorrect = Math.abs(guessRank - actualEstimate) <= 100 || logError <= allowance;

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
        correct: false,
        revealed: false,
        actualRank: undefined,
        predictedRank: undefined,
        player: undefined,
        distribution: undefined,
      };
    } else {
      result.correct = adaptiveCorrect;
    }
    result.closeness = adaptiveCloseness(logError, allowance, adaptiveCorrect);

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

  const originalUpdateChallengeRound = updateChallengeRound;
  updateChallengeRound = function refinedUpdateRound(round, mode, challengeDate) {
    originalUpdateChallengeRound(round, mode, challengeDate);
    if (!round.root) return;
    const copy = round.root.querySelector(".attempt-copy");
    if (copy) {
      const left = Math.max(0, MAX_ATTEMPTS - round.guesses.length);
      copy.textContent = round.revealed ? "answer revealed" : `${left} guess${left === 1 ? "" : "es"} left · adaptive window`;
    }
  };

  function setPrefetchStatus(state, text) {
    document.querySelectorAll(".infinite-prefetch-status").forEach((node) => {
      node.dataset.state = state;
      node.textContent = text;
    });
  }

  function startInfinitePrefetch() {
    if (infinitePrefetchedPayload || infinitePrefetchPromise) return infinitePrefetchPromise;
    infinitePrefetchError = null;
    setPrefetchStatus("loading", "preparing next clip");
    infinitePrefetchPromise = requestJSON("/api/challenge/infinite", { method: "POST" })
      .then((payload) => {
        if (!payload.available) throw new Error("Infinite mode is not configured.");
        infinitePrefetchedPayload = payload;
        setPrefetchStatus("ready", "next clip ready");
        return payload;
      })
      .catch((error) => {
        infinitePrefetchError = error;
        setPrefetchStatus("error", "next clip retrying");
        return null;
      })
      .finally(() => { infinitePrefetchPromise = null; });
    return infinitePrefetchPromise;
  }

  loadInfinite = async function refinedLoadInfinite() {
    pauseAllVideos();
    const root = document.querySelector("#infiniteRoot");
    const started = Date.now();
    let payload = infinitePrefetchedPayload;
    infinitePrefetchedPayload = null;

    if (!payload) {
      root.innerHTML = `
        <section class="generation-card">
          <div class="busy-line"><i></i><span>selecting and rendering a fresh replay</span></div>
          <strong id="generationTime">0:00</strong>
          <p>The next clip will begin rendering as soon as this one opens.</p>
        </section>`;
      const timer = setInterval(() => {
        const elapsed = Math.floor((Date.now() - started) / 1000);
        const element = document.querySelector("#generationTime");
        if (element) element.textContent = `${Math.floor(elapsed / 60)}:${String(elapsed % 60).padStart(2, "0")}`;
      }, 1000);
      try {
        payload = await (infinitePrefetchPromise || startInfinitePrefetch());
        if (!payload && infinitePrefetchError) throw infinitePrefetchError;
      } finally {
        clearInterval(timer);
      }
    }

    if (!payload?.available) {
      root.innerHTML = `<section class="mode-intro"><p class="kicker">infinite</p><h1>could not prepare a replay.</h1><p>${escapeHTML(infinitePrefetchError?.message || "Try again in a moment.")}</p><button class="primary-button narrow" id="retryInfinite" type="button">try again</button></section>`;
      document.querySelector("#retryInfinite")?.addEventListener("click", loadInfinite);
      return;
    }

    rankPopulation = Number(payload.rankPopulation) || rankPopulation;
    infiniteRound = { item: payload.replay, guesses: [], revealed: false };
    mountChallenge(root, payload.replay, infiniteRound, "infinite · fresh", "infinite");
    startInfinitePrefetch();
  };

  const oldStart = document.querySelector("#startInfinite");
  if (oldStart) {
    const cleanStart = oldStart.cloneNode(true);
    oldStart.replaceWith(cleanStart);
    cleanStart.addEventListener("click", loadInfinite);
  }

  function predictionRatio(item) {
    if (!item.actualRank || !item.predictedRank) return Infinity;
    return Math.max(item.actualRank, item.predictedRank) / Math.max(1, Math.min(item.actualRank, item.predictedRank));
  }

  galleryCard = function refinedGalleryCard(item) {
    const map = item.beatmap || {};
    const thumbnail = item.thumbnailURL || `/api/gallery/${encodeURIComponent(item.id)}/thumbnail`;
    const ratio = predictionRatio(item);
    const errorLabel = Number.isFinite(ratio) ? `${ratio.toFixed(2)}× rank ratio` : "rank unavailable";
    const errorWidth = Number.isFinite(ratio) ? Math.min(100, Math.max(4, Math.log10(Math.max(1, ratio)) / 2 * 100)) : 4;
    const hidden = gallerySpoilersHidden;
    return `
      <article class="gallery-card ${hidden ? "spoiler" : ""}" data-gallery-id="${escapeHTML(item.id)}" tabindex="0" role="button" aria-label="Open replay">
        <button class="gallery-thumb" type="button" aria-label="Open replay"><img src="${escapeHTML(thumbnail)}" alt="" loading="lazy" decoding="async" /><span>watch</span></button>
        <div class="gallery-copy">
          <p class="gallery-eyebrow">replay</p>
          <h2>${hidden ? "mystery player" : escapeHTML(item.player || "Unknown player")}</h2>
          <p>${hidden ? "open to reveal map and ranks" : escapeHTML(`${map.artist ? `${map.artist} — ` : ""}${map.title || "Unknown map"}`)}</p>
          ${hidden ? "" : `<small>${escapeHTML(`${map.version || "Unknown difficulty"} · ${Number(item.star || 0).toFixed(2)}★ · ${(item.mods || ["NM"]).join("")}`)}</small>`}
        </div>
        ${hidden
          ? '<div class="spoiler-strip">ranks hidden until opened</div>'
          : `<div class="gallery-ranks"><div><span>actual</span><strong>${formatRank(item.actualRank)}</strong></div><div><span>model</span><strong>${formatRank(item.predictedRank)}</strong></div></div><div class="gallery-error"><i style="width:${errorWidth}%"></i><span>${escapeHTML(errorLabel)}</span></div>`}
      </article>`;
  };

  openGalleryDialog = function refinedOpenGallery(item) {
    if (!item) return;
    pauseAllVideos({ unloadDialog: true });
    const map = item.beatmap || {};
    const ratio = predictionRatio(item);
    document.querySelector("#galleryDialogBody").innerHTML = `
      <video src="${escapeHTML(item.videoURL)}" controls autoplay playsinline preload="metadata"></video>
      <div class="dialog-copy">
        <p class="kicker">replay result</p>
        <h1>${escapeHTML(item.player || "Unknown player")}</h1>
        <p>${escapeHTML(`${map.artist ? `${map.artist} — ` : ""}${map.title || "Unknown map"} [${map.version || "?"}]`)}</p>
        <div class="dialog-ranks"><div><span>actual</span><strong>${formatRank(item.actualRank)}</strong></div><div><span>model</span><strong>${formatRank(item.predictedRank)}</strong></div><div><span>ratio</span><strong>${Number.isFinite(ratio) ? `${ratio.toFixed(2)}×` : "—"}</strong></div></div>
        <a href="${escapeHTML(item.videoURL)}" target="_blank" rel="noreferrer">open video in a new tab</a>
      </div>`;
    const video = document.querySelector("#galleryDialog video");
    if (video) video.muted = false;
    const dialog = document.querySelector("#galleryDialog");
    if (dialog.showModal) dialog.showModal(); else dialog.setAttribute("open", "");
    video?.play?.().catch(() => {});
  };

  renderGallery = function refinedRenderGallery() {
    let items = [...galleryItems];
    const sort = document.querySelector("#gallerySort")?.value || "newest";
    if (sort === "error") items.sort((a, b) => predictionRatio(b) - predictionRatio(a));
    if (sort === "closest") items.sort((a, b) => predictionRatio(a) - predictionRatio(b));
    document.querySelector("#galleryGrid").innerHTML = items.map(galleryCard).join("");
    document.querySelectorAll(".gallery-card").forEach((card) => {
      const item = galleryItems.find((candidate) => candidate.id === card.dataset.galleryId);
      card.querySelector(".gallery-thumb")?.addEventListener("click", () => openGalleryDialog(item));
      card.addEventListener("click", (event) => { if (!event.target.closest("button, a, input, select")) openGalleryDialog(item); });
      card.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openGalleryDialog(item); }
      });
    });
    document.querySelector("#galleryEmpty").hidden = items.length !== 0;
  };

  document.querySelectorAll("[data-gallery-filter]").forEach((button) => button.remove());
  galleryFilter = "all";
  const controls = document.querySelector(".gallery-controls");
  let spoilerToggle = document.querySelector("#gallerySpoilerToggle");
  if (!spoilerToggle && controls) {
    spoilerToggle = document.createElement("button");
    spoilerToggle.id = "gallerySpoilerToggle";
    spoilerToggle.type = "button";
    spoilerToggle.className = "filter-button active";
    controls.prepend(spoilerToggle);
  }
  const syncSpoilerToggle = () => {
    if (!spoilerToggle) return;
    spoilerToggle.textContent = gallerySpoilersHidden ? "spoilers hidden" : "spoilers shown";
    spoilerToggle.classList.toggle("active", gallerySpoilersHidden);
    spoilerToggle.setAttribute("aria-pressed", String(gallerySpoilersHidden));
  };
  spoilerToggle?.addEventListener("click", () => {
    gallerySpoilersHidden = !gallerySpoilersHidden;
    storage.set("osu-rankguess-gallery-spoilers", gallerySpoilersHidden ? "hidden" : "shown");
    syncSpoilerToggle();
    renderGallery();
  });
  syncSpoilerToggle();

  const dialog = document.querySelector("#galleryDialog");
  dialog?.addEventListener("close", () => pauseAllVideos({ unloadDialog: true }));
  document.querySelector("#closeGalleryDialog")?.addEventListener("click", () => pauseAllVideos({ unloadDialog: true }));

  document.addEventListener("visibilitychange", () => { if (document.hidden) pauseAllVideos(); });
  window.addEventListener("blur", () => pauseAllVideos());
  window.addEventListener("pagehide", () => pauseAllVideos({ unloadDialog: true }));
})();
