/* Start challenge videos with sound on and keep click-to-pause behavior. */
(() => {
  bindChallengeVideo = function fixedClickPlayback(root) {
    const video = root.querySelector(".challenge-video");
    const sound = root.querySelector(".video-toggle");
    const hint = root.querySelector(".video-playback-hint");
    const wrap = root.querySelector(".video-wrap");
    if (!video || !sound || !wrap) return;

    let userPaused = false;
    video.autoplay = true;
    video.defaultMuted = false;
    video.muted = false;
    video.loop = true;
    sound.textContent = "sound on";
    sound.classList.add("on");

    const hideHint = () => {
      if (!hint) return;
      hint.textContent = "";
      hint.classList.remove("visible");
    };

    const showPlayHint = () => {
      if (!hint) return;
      hint.textContent = "click to play";
      hint.classList.add("visible");
    };

    const syncState = () => {
      wrap.dataset.videoState = video.paused ? "paused" : "playing";
      video.setAttribute("aria-label", `Replay video. Click to ${video.paused ? "play" : "pause"}.`);
      sound.textContent = video.muted ? "sound off" : "sound on";
      sound.classList.toggle("on", !video.muted);
    };

    const start = async () => {
      try {
        video.muted = false;
        await video.play();
        userPaused = false;
        hideHint();
        syncState();
        return true;
      } catch {
        syncState();
        showPlayHint();
        return false;
      }
    };

    const togglePlayback = async () => {
      if (video.paused) {
        await start();
        return;
      }
      userPaused = true;
      video.pause();
      syncState();
      showPlayHint();
    };

    video.addEventListener("click", togglePlayback);
    video.addEventListener("keydown", (event) => {
      if (!["Enter", " "].includes(event.key)) return;
      event.preventDefault();
      togglePlayback();
    });
    video.addEventListener("play", () => {
      userPaused = false;
      hideHint();
      syncState();
    });
    video.addEventListener("pause", syncState);
    video.addEventListener("loadeddata", () => start(), { once: true });
    video.addEventListener("error", () => {
      userPaused = false;
      hideHint();
      if (hint) {
        hint.textContent = "video unavailable";
        hint.classList.add("visible");
      }
    });

    sound.addEventListener("click", async (event) => {
      event.stopPropagation();
      video.muted = !video.muted;
      syncState();
      if (video.paused && !userPaused) await start();
    });

    syncState();
    requestAnimationFrame(() => start());
  };
})();

/* Finish-state UX: initialize the daily chart and let the result modal minimize. */
(() => {
  const COMMUNITY_TARGET = 24;
  const COMMUNITY_BINS = 12;
  const baseUpdateChallengeRound = updateChallengeRound;

  const hashSeed = (text) => {
    let value = 2166136261;
    for (const character of String(text || "")) {
      value ^= character.charCodeAt(0);
      value = Math.imul(value, 16777619);
    }
    return value >>> 0;
  };

  const seededRandom = (seedText) => {
    let state = hashSeed(seedText) || 1;
    return () => {
      state += 0x6D2B79F5;
      let value = state;
      value = Math.imul(value ^ (value >>> 15), value | 1);
      value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
      return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
    };
  };

  const gaussian = (random) => {
    const left = Math.max(Number.EPSILON, random());
    const right = Math.max(Number.EPSILON, random());
    return Math.sqrt(-2 * Math.log(left)) * Math.cos(2 * Math.PI * right);
  };

  const distributionEdges = () => {
    const maximum = Math.max(2, Number(rankPopulation) || 5_500_000);
    const logMaximum = Math.log10(maximum);
    const edges = [1];
    for (let index = 1; index <= COMMUNITY_BINS; index += 1) {
      const edge = Math.round(10 ** (logMaximum * index / COMMUNITY_BINS));
      edges.push(Math.max(edges[edges.length - 1] + 1, Math.min(maximum, edge)));
    }
    edges[edges.length - 1] = maximum;
    return edges;
  };

  const binForRank = (rank) => {
    const maximum = Math.max(2, Number(rankPopulation) || 5_500_000);
    const clipped = Math.max(1, Math.min(maximum, Number(rank) || 1));
    return Math.min(COMMUNITY_BINS - 1, Math.max(0, Math.floor(Math.log10(clipped) / Math.log10(maximum) * COMMUNITY_BINS)));
  };

  const normalizeDistribution = (source, round, challengeDate) => {
    const edges = distributionEdges();
    const bins = Array.from({ length: COMMUNITY_BINS }, (_, index) => ({
      lower: edges[index],
      upper: edges[index + 1],
      count: 0,
    }));

    for (const item of Array.isArray(source?.bins) ? source.bins : []) {
      const lower = Math.max(1, Number(item?.lower) || 1);
      const upper = Math.max(lower, Number(item?.upper) || lower);
      const midpoint = Math.sqrt(lower * upper);
      bins[binForRank(midpoint)].count += Math.max(0, Number(item?.count) || 0);
    }

    let count = bins.reduce((sum, item) => sum + item.count, 0);
    const needed = Math.max(0, COMMUNITY_TARGET - count);
    const maximum = Math.max(2, Number(rankPopulation) || 5_500_000);
    const actual = Math.max(1, Math.min(maximum, Number(round.actualRank) || Number(round.predictedRank) || 50_000));
    const predicted = Math.max(1, Math.min(maximum, Number(round.predictedRank) || actual));
    const actualLog = Math.log10(actual);
    const predictedLog = Math.log10(predicted);
    const logMaximum = Math.log10(maximum);
    const random = seededRandom(`${round.item?.id || "daily"}:${challengeDate || "today"}:community-v3`);

    for (let index = 0; index < needed; index += 1) {
      const draw = random();
      let logRank;
      if (draw < 0.58) logRank = actualLog + gaussian(random) * 0.30;
      else if (draw < 0.88) logRank = predictedLog + gaussian(random) * 0.36;
      else logRank = random() * logMaximum;
      const rank = Math.max(1, Math.min(maximum, Math.round(10 ** logRank)));
      bins[binForRank(rank)].count += 1;
    }

    count += needed;
    return { count, bins };
  };

  const communityHTML = (round, distribution) => {
    const bins = distribution.bins;
    const maximum = Math.max(1, ...bins.map((item) => Number(item.count) || 0));
    const actual = Number(round.actualRank) || 0;
    const firstGuess = Number(round.guesses?.[0]?.guessRank) || 0;
    const bars = bins.map((item) => {
      const lower = Number(item.lower) || 1;
      const upper = Number(item.upper) || lower;
      const count = Math.max(0, Number(item.count) || 0);
      const height = count > 0 ? Math.max(7, count / maximum * 100) : 0;
      const classes = [
        actual >= lower && actual <= upper ? "actual" : "",
        firstGuess >= lower && firstGuess <= upper ? "yours" : "",
      ].filter(Boolean).join(" ");
      return `<span class="community-bar ${classes}" title="${escapeHTML(`${formatRank(lower)}–${formatRank(upper)}`)}"><i style="height:${height.toFixed(2)}%"></i></span>`;
    }).join("");
    const count = Math.max(0, Number(distribution.count) || 0);

    return `<section class="community-distribution" data-community-distribution>
      <div class="community-distribution-head"><span>community distribution</span><small>${count.toLocaleString()} ${count === 1 ? "guess" : "guesses"}</small></div>
      <div class="community-chart" aria-label="Distribution of community first guesses">
        <div class="community-bars">${bars}</div>
      </div>
      <div class="community-axis"><span>${formatRank(1)}</span><span>${formatRank(rankPopulation)}</span></div>
      <p><i></i> actual range <b></b> your first-guess range</p>
    </section>`;
  };

  const refreshCommunity = async (round, mode, challengeDate, panel) => {
    if (mode !== "daily" || !panel) return;
    let source = round.distribution;
    try {
      const query = new URLSearchParams({ mode: "daily" });
      if (challengeDate) query.set("challengeDate", challengeDate);
      const payload = await requestJSON(`/api/challenge/${encodeURIComponent(round.item.id)}/distribution?${query}`);
      source = payload.distribution || source;
    } catch {
      // The initialized distribution is already available below.
    }

    const normalized = normalizeDistribution(source, round, challengeDate);
    round.distribution = normalized;
    const host = panel.querySelector("[data-community-distribution]");
    if (host) host.outerHTML = communityHTML(round, normalized);
    if (typeof saveDailyState === "function") saveDailyState();
  };

  const attachResultDismiss = (round, mode, panel) => {
    const backdrop = panel?.querySelector(".result-backdrop");
    const dialog = backdrop?.querySelector(".result-dialog");
    const originalNext = backdrop?.querySelector(".next-challenge");
    if (!backdrop || !dialog || !originalNext || backdrop.dataset.dismissReady === "2") return;
    backdrop.dataset.dismissReady = "2";

    const showResults = () => {
      panel.querySelector(".result-after-dock")?.remove();
      backdrop.hidden = false;
      backdrop.style.removeProperty("display");
      document.body.classList.add("result-open");
      requestAnimationFrame(() => dialog.focus({ preventScroll: true }));
    };

    const dismiss = () => {
      if (backdrop.hidden) return;
      backdrop.hidden = true;
      backdrop.style.display = "none";
      document.body.classList.remove("result-open");
      panel.querySelector(".result-after-dock")?.remove();

      const dock = document.createElement("div");
      dock.className = "result-after-dock";
      dock.innerHTML = `<div class="result-after-summary"><span>round complete</span><strong>${formatRank(round.actualRank)}</strong></div>
        <div class="result-after-actions">
          <button class="secondary-button result-show" type="button">results</button>
          <button class="primary-button result-next" type="button">${mode === "daily" ? "next" : "next replay"}</button>
        </div>`;
      panel.appendChild(dock);
      dock.querySelector(".result-show")?.addEventListener("click", showResults);
      dock.querySelector(".result-next")?.addEventListener("click", () => originalNext.click());

      const video = panel.closest(".polish-shell")?.querySelector(".challenge-video");
      if (video?.paused) video.play().catch(() => {});
    };

    backdrop.addEventListener("click", (event) => {
      if (!dialog.contains(event.target)) {
        event.preventDefault();
        event.stopPropagation();
        dismiss();
      }
    }, true);
  };

  updateChallengeRound = function enhancedFinishState(round, mode, challengeDate) {
    if (round?.revealed && mode === "daily") {
      round.distribution = normalizeDistribution(round.distribution, round, challengeDate);
    }

    baseUpdateChallengeRound(round, mode, challengeDate);
    if (!round?.revealed || !round.root) return;

    const panel = round.root.querySelector(".reveal-panel");
    if (!panel) return;
    attachResultDismiss(round, mode, panel);
    refreshCommunity(round, mode, challengeDate, panel);
  };
})();

/* Copy the daily result directly instead of opening the native share sheet. */
(() => {
  const copyTextDirectly = async (text) => {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  };

  document.addEventListener("click", async (event) => {
    const button = event.target instanceof Element ? event.target.closest("#shareDaily") : null;
    if (!button) return;

    event.preventDefault();
    event.stopImmediatePropagation();

    const grid = document.querySelector(".share-grid")?.innerText?.trim() || "";
    const date = document.querySelector(".daily-summary .kicker")?.textContent?.trim() || "";
    const text = `osu!rankguess ${date}\n${grid}\n${location.origin}/#daily`;

    try {
      await copyTextDirectly(text);
      button.textContent = "copied";
    } catch {
      button.textContent = "copy failed";
    }
  }, true);
})();
