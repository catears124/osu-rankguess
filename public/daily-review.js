/* Review completed daily rounds without reopening guesses. */
(() => {
  const COMMUNITY_TARGET = 24;
  const COMMUNITY_BINS = 12;
  const communityRequests = new WeakMap();
  const baseUpdateChallengeRound = updateChallengeRound;
  const baseRenderDaily = renderDaily;
  const baseRenderDailySummary = renderDailySummary;

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
      edges.push(Math.max(edges.at(-1) + 1, Math.min(maximum, edge)));
    }
    edges[edges.length - 1] = maximum;
    return edges;
  };

  const binForRank = (rank) => {
    const maximum = Math.max(2, Number(rankPopulation) || 5_500_000);
    const clipped = Math.max(1, Math.min(maximum, Number(rank) || 1));
    return Math.min(
      COMMUNITY_BINS - 1,
      Math.max(0, Math.floor(Math.log10(clipped) / Math.log10(maximum) * COMMUNITY_BINS)),
    );
  };

  const normalizeDistribution = (source, round, challengeDate) => {
    const edges = distributionEdges();
    const bins = Array.from({ length: COMMUNITY_BINS }, (_, index) => ({
      lower: edges[index],
      upper: edges[index + 1],
      count: 0,
      observedCount: 0,
      baselineCount: 0,
    }));

    const sourceHasSplitCounts = source?.observedCount !== undefined || source?.baselineCount !== undefined;
    for (const item of Array.isArray(source?.bins) ? source.bins : []) {
      const lower = Math.max(1, Number(item?.lower) || 1);
      const upper = Math.max(lower, Number(item?.upper) || lower);
      const target = bins[binForRank(Math.sqrt(lower * upper))];
      const count = Math.max(0, Number(item?.count) || 0);
      const observed = sourceHasSplitCounts
        ? Math.max(0, Number(item?.observedCount) || 0)
        : count;
      const baseline = sourceHasSplitCounts
        ? Math.max(0, Number(item?.baselineCount) || Math.max(0, count - observed))
        : 0;
      target.count += count;
      target.observedCount += observed;
      target.baselineCount += baseline;
    }

    const observedCount = Math.max(
      0,
      Number(source?.observedCount ?? source?.count)
        || bins.reduce((sum, item) => sum + item.observedCount, 0),
    );
    let baselineCount = Math.max(
      0,
      Number(source?.baselineCount)
        || bins.reduce((sum, item) => sum + item.baselineCount, 0),
    );
    const displayed = bins.reduce((sum, item) => sum + item.count, 0);
    const needed = Math.max(0, COMMUNITY_TARGET - displayed);
    const maximum = Math.max(2, Number(rankPopulation) || 5_500_000);
    const actual = Math.max(1, Math.min(maximum, Number(round.actualRank) || Number(round.predictedRank) || 50_000));
    const predicted = Math.max(1, Math.min(maximum, Number(round.predictedRank) || actual));
    const actualLog = Math.log10(actual);
    const predictedLog = Math.log10(predicted);
    const logMaximum = Math.log10(maximum);
    const random = seededRandom(`${round.item?.id || "daily"}:${challengeDate || "today"}:community-v4`);

    for (let index = 0; index < needed; index += 1) {
      const draw = random();
      let logRank;
      if (draw < 0.58) logRank = actualLog + gaussian(random) * 0.30;
      else if (draw < 0.88) logRank = predictedLog + gaussian(random) * 0.36;
      else logRank = random() * logMaximum;
      const rank = Math.max(1, Math.min(maximum, Math.round(10 ** logRank)));
      const bin = bins[binForRank(rank)];
      bin.count += 1;
      bin.baselineCount += 1;
    }
    baselineCount += needed;

    return {
      ...(source || {}),
      count: observedCount,
      observedCount,
      baselineCount,
      baselineTarget: COMMUNITY_TARGET,
      smoothed: baselineCount > 0,
      bins,
    };
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
      const title = `${formatRank(lower)}–${formatRank(upper)} · ${count} ${count === 1 ? "guess" : "guesses"}`;
      return `<span class="community-bar ${classes}" title="${escapeHTML(title)}"><i style="height:${height.toFixed(2)}%"></i></span>`;
    }).join("");
    const displayed = bins.reduce((sum, item) => sum + Math.max(0, Number(item.count) || 0), 0);
    const countLabel = `${displayed.toLocaleString()} ${displayed === 1 ? "guess" : "guesses"}`;

    return `<section class="community-distribution" data-community-distribution>
      <div class="community-distribution-head"><span>community distribution</span><small>${countLabel}</small></div>
      <div class="community-chart" aria-label="Distribution of first guesses from the community">
        <div class="community-bars">${bars}</div>
      </div>
      <div class="community-axis"><span>${formatRank(1)}</span><span>${formatRank(rankPopulation)}</span></div>
      <p><i></i> actual range <b></b> your first-guess range</p>
    </section>`;
  };

  const replaceCommunity = (panel, round) => {
    const host = panel?.querySelector("[data-community-distribution]");
    if (host) host.outerHTML = communityHTML(round, round.distribution);
  };

  const refreshCommunity = (round, mode, challengeDate, panel) => {
    if (mode !== "daily" || !panel) return;

    round.distribution = normalizeDistribution(round.distribution, round, challengeDate);
    replaceCommunity(panel, round);
    if (typeof saveDailyState === "function") saveDailyState();
    if (communityRequests.has(round)) return;

    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 10_000);
    const request = (async () => {
      try {
        const query = new URLSearchParams({ mode: "daily" });
        if (challengeDate) query.set("challengeDate", challengeDate);
        const payload = await requestJSON(
          `/api/challenge/${encodeURIComponent(round.item.id)}/distribution?${query}`,
          { signal: controller.signal },
        );
        round.distribution = normalizeDistribution(payload.distribution, round, challengeDate);
        replaceCommunity(panel, round);
        if (typeof saveDailyState === "function") saveDailyState();
      } catch {
        replaceCommunity(panel, round);
      } finally {
        window.clearTimeout(timeout);
        communityRequests.delete(round);
      }
    })();
    communityRequests.set(round, request);
  };

  const attachResultDismiss = (round, mode, panel) => {
    const backdrop = panel?.querySelector(".result-backdrop");
    const dialog = backdrop?.querySelector(".result-dialog");
    const originalNext = backdrop?.querySelector(".next-challenge");
    if (!backdrop || !dialog || !originalNext || backdrop.dataset.dismissReady === "1") return;
    backdrop.dataset.dismissReady = "1";

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
    };

    backdrop.addEventListener("click", (event) => {
      if (dialog.contains(event.target)) return;
      event.preventDefault();
      event.stopPropagation();
      dismiss();
    }, true);

    dialog.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      dismiss();
    });
  };

  const highestUnlockedDailyIndex = () => {
    const firstUnrevealed = dailyState?.rounds?.findIndex((round) => !round.revealed) ?? -1;
    return firstUnrevealed < 0 ? Math.max(0, (dailyState?.rounds?.length || 1) - 1) : firstUnrevealed;
  };

  updateChallengeRound = function reviewableUpdateChallengeRound(round, mode, challengeDate) {
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

  renderDaily = function reviewableDaily() {
    baseRenderDaily();
    const maximum = highestUnlockedDailyIndex();
    document.querySelectorAll("#dailyRoot [data-daily-index]").forEach((button) => {
      button.disabled = Number(button.dataset.dailyIndex) > maximum;
    });
  };

  renderDailySummary = function reviewableDailySummary() {
    baseRenderDailySummary();
    const summary = document.querySelector("#dailyRoot .daily-summary");
    if (!summary || !dailyState?.rounds?.length) return;

    const review = document.createElement("div");
    review.className = "daily-review-nav";
    review.innerHTML = `<span>review replays</span><div>${dailyState.rounds.map((round, index) => `<button class="secondary-button" type="button" data-review-daily="${index}">${index + 1}</button>`).join("")}</div>`;
    summary.appendChild(review);
    review.querySelectorAll("[data-review-daily]").forEach((button) => {
      button.addEventListener("click", () => {
        dailyState.current = Number(button.dataset.reviewDaily);
        saveDailyState();
        renderDaily();
        document.querySelector("#dailyRoot")?.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
      });
    });
  };
})();
