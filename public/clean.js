/* Minimal game layout + browser-side o!rdr traffic. Loaded last. */
(() => {
  const previousShowView = showView;
  const serverCreateRender = createRender;
  const serverWaitForRender = waitForRender;

  const ORDR_RENDER_URL = "https://apis.issou.best/ordr/renders";
  const ORDR_DYNLINK_URL = "https://apis.issou.best/dynlink/ordr/gen";

  const ratio = (left, right) => Math.max(left, right) / Math.max(1, Math.min(left, right));

  const ensureRound = (round) => {
    if (!Array.isArray(round.guesses)) round.guesses = [];
    if (!Array.isArray(round.botGuesses)) round.botGuesses = [];
    return round;
  };

  const feedback = (guess) => {
    if (!guess) return "—";
    if (guess.correct) return "hit";
    const heat = guess.closeness === "very_close" ? "hot" : guess.closeness === "close" ? "warm" : "cold";
    const direction = guess.direction === "better" ? "lower" : "higher";
    return `${heat} · ${direction}`;
  };

  const sharedRange = (round) => {
    let lower = 1;
    let upper = rankPopulation;
    for (const guess of [...(round.guesses || []), ...(round.botGuesses || [])]) {
      const value = clamp(Math.round(Number(guess.guessRank) || 1), 1, rankPopulation);
      if (guess.direction === "better") upper = Math.min(upper, Math.max(1, value - 1));
      if (guess.direction === "worse") lower = Math.max(lower, Math.min(rankPopulation, value + 1));
    }
    if (round.revealed && Number(round.actualRank) > 0) lower = upper = Number(round.actualRank);
    if (lower > upper) lower = upper = clamp(Math.round((lower + upper) / 2), 1, rankPopulation);
    return { lower, upper };
  };

  const bestRatio = (guesses, actualRank) => {
    if (!Number(actualRank) || !guesses?.length) return Infinity;
    return Math.min(...guesses.map((guess) => ratio(Number(guess.guessRank), Number(actualRank))));
  };

  const firstHit = (guesses) => guesses?.findIndex((guess) => guess.correct) ?? -1;

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
    if (!round.revealed || !Number(round.actualRank)) return null;
    const playerRatio = bestRatio(round.guesses, round.actualRank);
    const botRatio = bestRatio(round.botGuesses, round.actualRank);
    if (Math.abs(playerRatio - botRatio) < 1e-9) return "tie";
    return playerRatio < botRatio ? "player" : "bot";
  };

  rankControlHTML = function cleanRankControl(initialRank = 50_000) {
    const position = rankToSoftPosition(initialRank);
    const ranks = [...new Set([1, 1_000, 10_000, 100_000, 1_000_000, rankPopulation].filter((rank) => rank <= rankPopulation))];
    const ticks = ranks.map((rank) => `<button type="button" class="slider-tick" data-rank="${rank}" style="left:${(rankToSoftPosition(rank) / SLIDER_STEPS * 100).toFixed(3)}%">${compactRank(rank)}</button>`).join("");

    return `<div class="rank-control duel-rank-control">
      <div class="duel-guess-head">
        <div><span>your guess</span><strong class="live-rank">${formatRank(initialRank)}</strong></div>
        <label class="rank-number-label"><span>type rank</span><div class="rank-number-shell"><b>#</b><input class="rank-number-input" type="number" min="1" max="${rankPopulation}" inputmode="numeric" value="${initialRank}" required /></div></label>
      </div>
      <div class="known-range-copy"><span>possible</span><strong class="known-range-text">${formatRank(1)} – ${formatRank(rankPopulation)}</strong></div>
      <div class="rank-slider-shell game-slider-shell">
        <span class="rank-range-mask rank-range-mask-left" aria-hidden="true"></span>
        <span class="rank-known-range" aria-hidden="true"></span>
        <span class="rank-range-mask rank-range-mask-right" aria-hidden="true"></span>
        <span class="rank-slider-fill" aria-hidden="true"></span>
        <input class="rank-slider" type="range" min="0" max="${SLIDER_STEPS}" step="1" value="${position}" aria-label="Rank guess" />
      </div>
      <div class="slider-scale" aria-label="Rank shortcuts">${ticks}</div>
      <p class="range-callout">pick a rank</p>
    </div>`;
  };

  challengeCardHTML = function cleanChallengeCard(item, label) {
    const map = item.beatmap || {};
    const title = `${map.artist ? `${map.artist} — ` : ""}${map.title || "Unknown map"}`;
    const stats = `${map.version || "Unknown difficulty"} · ${Number(item.star || 0).toFixed(2)}★ · ${Number(item.accuracyPercent || 0).toFixed(2)}% · ${(item.mods || ["NM"]).join("")}`;
    const pips = Array.from({ length: MAX_ATTEMPTS }, () => "<i></i>").join("");

    return `<div class="challenge-shell game-shell duel-shell clean-shell">
      <div class="challenge-topline clean-topline">
        <span class="game-round">${escapeHTML(label)}</span>
        <strong class="duel-title">you vs rankbot</strong>
        <div class="attempt-stack"><span class="attempt-copy">turn 1 of ${MAX_ATTEMPTS}</span><div class="attempt-pips" aria-label="Turns">${pips}</div></div>
      </div>
      <div class="clean-mapline"><strong>${escapeHTML(title)}</strong><span>${escapeHTML(stats)}</span></div>
      <div class="challenge-content clean-content">
        <div class="video-column"><div class="video-wrap"><video class="challenge-video" src="${escapeHTML(item.videoURL)}" autoplay loop playsinline preload="auto"></video><button class="video-toggle on" type="button" aria-label="Toggle sound">${ICON_SOUND}</button><button class="video-play" type="button" aria-label="Play or pause replay">pause</button></div></div>
        <aside class="challenge-side clean-history" hidden><ol class="guess-list duel-turn-list" aria-label="Turn history"></ol></aside>
      </div>
      <div class="guess-dock clean-dock"><div class="guess-zone"><form class="guess-form">${rankControlHTML()}<button class="primary-button guess-submit" type="submit">lock guess</button></form><p class="challenge-error" hidden></p><div class="reveal-panel clean-reveal" hidden></div></div></div>
    </div>`;
  };

  const renderTurns = (round) => round.guesses.map((playerGuess, index) => {
    const botGuess = round.botGuesses[index];
    return `<li class="duel-turn ${playerGuess.correct ? "player-hit" : ""} ${botGuess?.correct ? "bot-hit" : ""}">
      <span class="duel-turn-number">${index + 1}</span>
      <div class="duel-guess duel-player"><i>you</i><strong>${formatRank(playerGuess.guessRank)}</strong><em>${escapeHTML(feedback(playerGuess))}</em></div>
      <div class="duel-guess duel-bot"><i>bot</i><strong>${formatRank(botGuess?.guessRank)}</strong><em>${escapeHTML(feedback(botGuess))}</em></div>
    </li>`;
  }).join("");

  const resultHTML = (round, mode) => {
    const winner = winnerFor(round) || "tie";
    const playerRatio = bestRatio(round.guesses, round.actualRank);
    const botRatio = bestRatio(round.botGuesses, round.actualRank);
    const botRank = round.botGuesses.at(-1)?.guessRank || round.predictedRank;
    const title = winner === "player" ? "you win" : winner === "bot" ? "rankbot wins" : "tie";
    return `<div class="duel-result-strip ${winner}">
      <div class="actual-block"><span>actual rank</span><strong>${formatRank(round.actualRank)}</strong><small>${escapeHTML(round.player || "player")}</small></div>
      <div class="outcome-block"><span>${title}</span><strong>bot ${formatRank(botRank)}</strong><small>you ${playerRatio.toFixed(2)}× · bot ${botRatio.toFixed(2)}×</small></div>
      <button class="primary-button next-challenge" type="button">${mode === "daily" ? "next" : "next replay"}</button>
    </div>`;
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

  updateChallengeRound = function cleanUpdateRound(round, mode, challengeDate) {
    ensureRound(round);
    const root = round.root;
    if (!root) return;
    const turns = round.guesses.length;
    const list = root.querySelector(".guess-list");
    const history = root.querySelector(".clean-history");
    if (list) list.innerHTML = renderTurns(round);
    if (history) history.hidden = turns === 0;

    const attemptCopy = root.querySelector(".attempt-copy");
    if (attemptCopy) attemptCopy.textContent = round.revealed ? "finished" : `turn ${turns + 1} of ${MAX_ATTEMPTS}`;
    [...root.querySelectorAll(".attempt-pips i")].forEach((pip, index) => {
      pip.classList.toggle("used", index < turns);
      pip.classList.toggle("hit", Boolean(round.guesses[index]?.correct || round.botGuesses[index]?.correct));
    });

    const lastPlayer = round.guesses.at(-1);
    const lastBot = round.botGuesses.at(-1);
    const callout = root.querySelector(".range-callout");
    if (callout) {
      if (!lastPlayer) callout.textContent = "pick a rank";
      else if (round.revealed) callout.textContent = "";
      else callout.textContent = `you ${feedback(lastPlayer)} · bot ${feedback(lastBot)}`;
    }

    paintRange(round);
    const form = root.querySelector(".guess-form");
    if (form) form.hidden = Boolean(round.revealed);
    const button = form?.querySelector(".guess-submit");
    if (button && !round.revealed) button.textContent = "lock guess";

    const panel = root.querySelector(".reveal-panel");
    if (panel) {
      panel.hidden = !round.revealed;
      if (round.revealed) {
        panel.innerHTML = resultHTML(round, mode);
        panel.querySelector(".next-challenge")?.addEventListener("click", () => mode === "daily" ? advanceDaily() : loadInfinite());
      }
    }
  };

  const directError = async (response) => {
    const payload = await response.json().catch(() => ({}));
    const error = new Error(payload.message || payload.error || `o!rdr request failed (${response.status})`);
    error.directHTTP = true;
    error.status = response.status;
    return error;
  };

  const directRender = async (file) => {
    const form = new FormData();
    form.append("skin", "whitecatCK1.0");
    form.append("resolution", "960x540");
    form.append("showPPCounter", "false");
    form.append("showScoreboard", "false");
    form.append("showResultScreen", "true");
    form.append("skip", "true");
    form.append("customSkin", "false");
    form.append("generateThumbnail", "true");
    form.append("replayFile", file, file.name);
    const response = await fetch(ORDR_RENDER_URL, { method: "POST", body: form, headers: { Accept: "application/json" } });
    if (!response.ok) throw await directError(response);
    const payload = await response.json();
    if (!payload.renderID) throw new Error("o!rdr did not return a render ID");
    return { ok: true, renderID: Number(payload.renderID), clientSubmitted: true };
  };

  createRender = async function clientCreateRender(file, replayHash, username) {
    try {
      return await directRender(file);
    } catch (error) {
      if (error?.directHTTP && ![401, 403].includes(Number(error.status))) throw error;
      return serverCreateRender(file, replayHash, username);
    }
  };

  const normalizeVideoURL = (value) => {
    let text = String(value || "").trim();
    if (!text) return null;
    if (text.startsWith("//")) text = `https:${text}`;
    if (text.startsWith("http://")) text = `https://${text.slice(7)}`;
    return text.startsWith("https://") ? text : null;
  };

  const extractStar = (...values) => {
    const text = values.filter(Boolean).join(" ");
    const match = text.match(/(?:\[|\()\s*(\d+(?:\.\d+)?)\s*(?:⭐|★|\*)(?:\]|\))/i)
      || text.match(/(?:^|\s)(\d+(?:\.\d+)?)\s*(?:⭐|★|stars?)/i);
    return match ? Number(match[1]) : null;
  };

  const directRenderStatus = async (renderID) => {
    const response = await fetch(`${ORDR_RENDER_URL}?renderID=${encodeURIComponent(renderID)}`, { cache: "no-store", headers: { Accept: "application/json" } });
    if (!response.ok) throw await directError(response);
    const payload = await response.json();
    const render = payload.renders?.[0];
    if (!render) return { ready: false, progress: "queued" };
    const errorCode = Number(render.errorCode) || 0;
    if (errorCode) return { ready: false, failed: true, errorCode, progress: render.progress || "failed" };

    let dynlink = null;
    try {
      const dynResponse = await fetch(`${ORDR_DYNLINK_URL}?id=${encodeURIComponent(renderID)}`, { cache: "no-store", headers: { Accept: "application/json" } });
      if (dynResponse.ok) dynlink = (await dynResponse.json())?.url;
    } catch {}

    const videoURL = [dynlink, render.videoUrl, render.videoURL, render.url].map(normalizeVideoURL).find(Boolean) || null;
    const description = String(render.description || "").trim() || null;
    const title = String(render.title || "").trim() || null;
    const star = extractStar(title, description);
    return {
      ok: true,
      renderID,
      ready: Boolean(videoURL && star !== null),
      progress: String(render.progress || "working"),
      description,
      title,
      videoURL,
      renderMetadata: {
        description,
        title,
        star,
        username: render.username,
        replayUsername: render.replayUsername,
        replayMods: render.replayMods,
        mapTitle: render.mapTitle,
        mapLength: render.mapLength,
        drainTime: render.drainTime,
        replayDifficulty: render.replayDifficulty,
        mapID: render.mapID,
        mapLink: render.mapLink,
      },
    };
  };

  waitForRender = async function clientWaitForRender(renderID, runID) {
    for (let attempt = 0; attempt < 300; attempt += 1) {
      if (runID !== activeRun) throw new Error("Analysis cancelled.");
      let payload;
      try {
        payload = await directRenderStatus(renderID);
      } catch (error) {
        if (error?.directHTTP) throw error;
        return serverWaitForRender(renderID, runID);
      }
      renderStatus.hidden = false;
      renderStatus.textContent = `o!rdr #${renderID} · ${payload.progress || "working"}`;
      if (payload.failed) throw new Error(`o!rdr failed with code ${payload.errorCode}`);
      if (payload.ready) return payload;
      await sleep(3000);
    }
    throw new Error("o!rdr did not finish within fifteen minutes.");
  };

  const startInfiniteNow = () => {
    const root = document.querySelector("#infiniteRoot");
    if (!root || root.querySelector(".challenge-shell, .generation-card")) return;
    if (typeof loadInfinite === "function") loadInfinite().catch(() => {});
  };

  showView = function cleanShowView(name) {
    previousShowView(name);
    if (name === "infinite") queueMicrotask(startInfiniteNow);
  };

  if (document.body.dataset.view === "infinite") queueMicrotask(startInfiniteNow);
})();
