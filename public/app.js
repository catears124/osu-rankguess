const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const DEFAULT_RANK_POPULATION = 5_500_000;
const SOFT_LOG_SOFTNESS = 2_500;
const SLIDER_STEPS = 10_000;
const MAX_ATTEMPTS = 5;
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

let rankPopulation = DEFAULT_RANK_POPULATION;
let selectedFile = null;
let activeRun = 0;
let galleryOffset = 0;
let galleryLoaded = false;
let galleryItems = [];
let galleryFilter = "all";
let dailyPayload = null;
let dailyState = null;
let infiniteRound = null;

const replayInput = $("#replayInput");
const dropzone = $("#dropzone");
const runButton = $("#runButton");
const errorBox = $("#errorBox");
const results = $("#results");
const renderStatus = $("#renderStatus");

const clamp = (value, minimum, maximum) => Math.min(maximum, Math.max(minimum, value));
const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
const escapeHTML = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

const formatRank = (value) => Number(value) > 0
  ? `#${Math.round(Number(value)).toLocaleString()}`
  : "—";

const formatBytes = (bytes) => bytes < 1024
  ? `${bytes} B`
  : bytes < 1024 * 1024
    ? `${(bytes / 1024).toFixed(1)} KB`
    : `${(bytes / 1024 / 1024).toFixed(2)} MB`;

const formatTopPercent = (value) => {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  if (number < 0.001) return `${number.toExponential(2)}%`;
  if (number < 0.1) return `${number.toFixed(3)}%`;
  if (number < 1) return `${number.toFixed(2)}%`;
  return `${number.toFixed(1)}%`;
};

const compactRank = (value) => {
  const number = Number(value) || 0;
  if (number >= 1_000_000) return `${(number / 1_000_000).toFixed(number % 1_000_000 ? 1 : 0)}m`;
  if (number >= 1_000) return `${Math.round(number / 1_000)}k`;
  return String(number);
};

const memoryStorage = new Map();
const storage = {
  get(key) {
    try { return globalThis.localStorage?.getItem(key) ?? memoryStorage.get(key) ?? null; }
    catch { return memoryStorage.get(key) ?? null; }
  },
  set(key, value) {
    memoryStorage.set(key, value);
    try { globalThis.localStorage?.setItem(key, value); } catch {}
  },
};

function getVisitorID() {
  const key = "osu-rankguess-visitor-v1";
  let value = storage.get(key);
  if (!value) {
    value = globalThis.crypto?.randomUUID?.()
      || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
    storage.set(key, value);
  }
  return value;
}
const visitorID = getVisitorID();

async function copyText(text) {
  try {
    if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    area.readOnly = true;
    area.style.cssText = "position:fixed;left:-9999px;opacity:0";
    document.body.appendChild(area);
    area.select();
    const copied = document.execCommand("copy");
    area.remove();
    return copied;
  }
}

function softPositionToRank(position, maximum = rankPopulation) {
  const unit = clamp(Number(position) / SLIDER_STEPS, 0, 1);
  const scale = Math.log1p((maximum - 1) / SOFT_LOG_SOFTNESS);
  return Math.round(1 + SOFT_LOG_SOFTNESS * Math.expm1(unit * scale));
}

function rankToSoftPosition(rank, maximum = rankPopulation) {
  const clipped = clamp(Number(rank) || 1, 1, maximum);
  const denominator = Math.log1p((maximum - 1) / SOFT_LOG_SOFTNESS);
  const unit = Math.log1p((clipped - 1) / SOFT_LOG_SOFTNESS) / denominator;
  return Math.round(clamp(unit, 0, 1) * SLIDER_STEPS);
}

async function apiError(response) {
  const payload = await response.json().catch(() => ({}));
  const detail = payload.detail || payload;
  const message = typeof detail === "string" ? detail : detail.message;
  return new Error(message || `Request failed (${response.status})`);
}

async function requestJSON(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  if (!response.ok) throw await apiError(response);
  return response.json();
}

function showView(name) {
  document.body.dataset.view = name;
  $$(".view").forEach((view) => {
    const active = view.dataset.view === name;
    view.hidden = !active;
    view.classList.toggle("active", active);
  });
  $$('[data-view-link]').forEach((link) => link.classList.toggle("active", link.dataset.viewLink === name));
  history.replaceState(null, "", `#${name}`);
  window.scrollTo({ top: 0, behavior: "auto" });
  if (name === "gallery" && !galleryLoaded) loadGallery(true);
  if (name === "daily" && !dailyPayload) loadDaily();
}

document.addEventListener("click", (event) => {
  const link = event.target.closest("[data-view-link]");
  if (!link) return;
  event.preventDefault();
  showView(link.dataset.viewLink);
});

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
}
function hideError() { errorBox.hidden = true; }

function resetSteps() {
  $$(".steps li").forEach((item) => {
    item.classList.remove("active", "done", "failed");
    $("small", item).textContent = item.dataset.defaultDetail;
  });
  renderStatus.hidden = true;
  renderStatus.textContent = "";
}

function setStep(index, state, detail) {
  const item = $$(".steps li")[index];
  if (!item) return;
  item.classList.remove("active", "done", "failed");
  if (state) item.classList.add(state);
  if (detail) $("small", item).textContent = detail;
}

function failCurrentStep(message) {
  const item = $(".steps li.active");
  if (!item) return;
  item.classList.remove("active");
  item.classList.add("failed");
  $("small", item).textContent = message;
}

function setFile(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".osr")) return showError("Choose an .osr replay file.");
  if (file.size > 4_000_000) return showError("Replay exceeds the 4 MB upload limit.");
  selectedFile = file;
  $("#dropTitle").textContent = "replay selected";
  $("#dropSubtitle").textContent = "ready to analyze";
  $("#fileName").textContent = file.name;
  $("#fileSize").textContent = formatBytes(file.size);
  $("#fileChip").hidden = false;
  runButton.disabled = false;
  results.hidden = true;
  hideError();
  resetSteps();
}

replayInput.addEventListener("change", () => setFile(replayInput.files?.[0]));
["dragenter", "dragover"].forEach((name) => dropzone.addEventListener(name, (event) => {
  event.preventDefault();
  dropzone.classList.add("dragging");
}));
["dragleave", "drop"].forEach((name) => dropzone.addEventListener(name, (event) => {
  event.preventDefault();
  dropzone.classList.remove("dragging");
}));
dropzone.addEventListener("drop", (event) => setFile(event.dataTransfer?.files?.[0]));

async function sha256File(file) {
  if (!globalThis.crypto?.subtle) throw new Error("This browser cannot hash replay files. Open the site in Safari, Chrome, or Firefox.");
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function cacheReplay(file, replayHash) {
  const form = new FormData();
  form.append("replay", file, file.name);
  form.append("replay_hash", replayHash);
  return requestJSON("/api/replay/cache", { method: "POST", body: form });
}

async function createRender(file, replayHash, username) {
  const form = new FormData();
  form.append("replay", file, file.name);
  form.append("replay_hash", replayHash);
  form.append("username", username);
  return requestJSON("/api/ordr/render", { method: "POST", body: form });
}

async function waitForRender(renderID, runID) {
  for (let attempt = 0; attempt < 300; attempt += 1) {
    if (runID !== activeRun) throw new Error("Analysis cancelled.");
    const payload = await requestJSON(`/api/ordr/status?renderID=${encodeURIComponent(renderID)}`);
    renderStatus.hidden = false;
    renderStatus.textContent = `o!rdr #${renderID} · ${payload.progress || "working"}`;
    if (payload.failed) throw new Error(`o!rdr failed with code ${payload.errorCode}`);
    if (payload.ready) return payload;
    await sleep(3000);
  }
  throw new Error("o!rdr did not finish within fifteen minutes.");
}

async function predictReplay(cache, render, renderID) {
  return requestJSON("/api/predict", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      replayHash: cache.replayHash,
      cacheToken: cache.cacheToken,
      renderID,
      description: render.description || render.title || "",
      renderMetadata: render.renderMetadata || {},
      videoURL: render.videoURL,
      publish: $("#publishToggle").checked,
    }),
  });
}

function animateRank(element, target) {
  if (reduceMotion) {
    element.textContent = formatRank(target);
    return;
  }
  const duration = 650;
  const started = performance.now();
  const initial = Math.max(1, Math.round(target * 1.7));
  const tick = (now) => {
    const progress = clamp((now - started) / duration, 0, 1);
    const eased = 1 - Math.pow(1 - progress, 4);
    element.textContent = formatRank(Math.round(initial + (target - initial) * eased));
    if (progress < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

function renderPrediction(payload) {
  rankPopulation = Number(payload.rankPopulation) || rankPopulation;
  $("#resultPlayer").textContent = payload.player || "player";
  animateRank($("#predictedRank"), Number(payload.predictedRank));
  $("#rankContext").textContent = `${formatTopPercent(payload.topPercent)} of ranked players · ${payload.modelVersion || "rank model"}`;
  $("#topPercent").textContent = formatTopPercent(payload.topPercent);
  $("#accuracyValue").textContent = `${Number(payload.accuracyPercent).toFixed(2)}%`;
  $("#modsValue").textContent = (payload.mods || ["NM"]).join("");
  $("#confidenceLabel").textContent = payload.confidence || "—";
  $("#starValue").textContent = `${Number(payload.beatmap?.star || 0).toFixed(2)}★`;
  $("#ppValue").textContent = Number(payload.scorePP) > 0 ? `${Number(payload.scorePP).toFixed(1)}pp` : "not matched";
  $("#mapTitle").textContent = `${payload.beatmap?.artist ? `${payload.beatmap.artist} — ` : ""}${payload.beatmap?.title || "Unknown map"}`;
  $("#mapMeta").textContent = `${payload.beatmap?.version || "Unknown difficulty"} · ${(payload.mods || ["NM"]).join("")}`;
  $("#replayVideo").src = payload.videoURL;
  $("#videoLink").href = payload.videoURL;
  const comparison = $("#rankComparison");
  comparison.hidden = !payload.actualRank;
  if (payload.actualRank) $("#actualRank").textContent = formatRank(payload.actualRank);
  $("#galleryStatus").textContent = payload.gallerySaved
    ? "Saved to the public gallery."
    : $("#publishToggle").checked
      ? "Prediction complete. Gallery storage was unavailable."
      : "Prediction complete. Kept private.";
  results.hidden = false;
  results.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
}

runButton.addEventListener("click", async () => {
  if (!selectedFile) return;
  const runID = ++activeRun;
  runButton.disabled = true;
  runButton.textContent = "working…";
  hideError();
  resetSteps();
  results.hidden = true;
  try {
    setStep(0, "active", "Hashing and parsing replay");
    const replayHash = await sha256File(selectedFile);
    const cache = await cacheReplay(selectedFile, replayHash);
    if (runID !== activeRun) return;
    setStep(0, "done", `${cache.eventCount.toLocaleString()} events · ${cache.player}`);

    setStep(1, "active", "Submitting replay render");
    const created = await createRender(selectedFile, replayHash, cache.player);
    if (runID !== activeRun) return;
    setStep(1, "done", `o!rdr #${created.renderID}`);

    setStep(2, "active", "Waiting in the o!rdr queue");
    const render = await waitForRender(created.renderID, runID);
    if (runID !== activeRun) return;
    setStep(2, "done", "Video ready");

    setStep(3, "active", "Resolving beatmap and score PP");
    setStep(3, "done", "Metadata ready");
    setStep(4, "active", "Running rank model");
    const prediction = await predictReplay(cache, render, created.renderID);
    if (runID !== activeRun) return;
    setStep(4, "done", "Prediction complete");
    renderStatus.textContent = `o!rdr #${created.renderID} · done`;
    renderPrediction(prediction);
    galleryLoaded = false;
  } catch (error) {
    failCurrentStep(error.message || "Analysis failed");
    showError(error.message || "Analysis failed");
  } finally {
    if (runID === activeRun) {
      runButton.disabled = false;
      runButton.textContent = "analyze replay";
    }
  }
});

$("#resetButton").addEventListener("click", () => {
  activeRun += 1;
  selectedFile = null;
  replayInput.value = "";
  $("#fileChip").hidden = true;
  $("#dropTitle").textContent = "choose .osr file";
  $("#dropSubtitle").textContent = "tap here or drop it";
  $("#replayVideo").removeAttribute("src");
  runButton.disabled = true;
  results.hidden = true;
  hideError();
  resetSteps();
  window.scrollTo({ top: 0, behavior: reduceMotion ? "auto" : "smooth" });
});

function rankControlHTML(initialRank = 50_000) {
  const position = rankToSoftPosition(initialRank);
  const tickRanks = [...new Set([1, 1_000, 10_000, 100_000, 1_000_000, rankPopulation].filter((rank) => rank <= rankPopulation))];
  const ticks = tickRanks.map((rank) => {
    const left = (rankToSoftPosition(rank) / SLIDER_STEPS) * 100;
    return `<button type="button" class="slider-tick" data-rank="${rank}" style="left:${left.toFixed(3)}%">${compactRank(rank)}</button>`;
  }).join("");
  return `
    <div class="rank-control">
      <div class="guess-readout"><span>your guess</span><strong class="live-rank">${formatRank(initialRank)}</strong></div>
      <div class="rank-slider-shell">
        <span class="rank-slider-fill" aria-hidden="true"></span>
        <input class="rank-slider" type="range" min="0" max="${SLIDER_STEPS}" step="1" value="${position}" aria-label="Soft logarithmic rank guess slider" />
      </div>
      <div class="slider-scale" aria-label="Rank shortcuts">${ticks}</div>
      <label class="rank-number-label"><span>exact rank</span><div class="rank-number-shell"><b>#</b><input class="rank-number-input" type="number" min="1" max="${rankPopulation}" inputmode="numeric" value="${initialRank}" required /></div></label>
    </div>`;
}

function bindRankControl(root) {
  const slider = $(".rank-slider", root);
  const number = $(".rank-number-input", root);
  const live = $(".live-rank", root);
  const fill = $(".rank-slider-fill", root);
  let settleTimer = null;

  const setRank = (rank, source, animate = false) => {
    const clipped = clamp(Math.round(Number(rank) || 1), 1, rankPopulation);
    if (source !== "number") number.value = clipped;
    if (source !== "slider") slider.value = rankToSoftPosition(clipped);
    live.textContent = formatRank(clipped);
    fill.style.width = `${(Number(slider.value) / SLIDER_STEPS * 100).toFixed(2)}%`;
    slider.setAttribute("aria-valuetext", formatRank(clipped));
    if (animate && !reduceMotion) {
      live.classList.add("sliding");
      clearTimeout(settleTimer);
      settleTimer = setTimeout(() => live.classList.remove("sliding"), 160);
    }
  };

  slider.addEventListener("input", () => setRank(softPositionToRank(slider.value), "slider", true), { passive: true });
  number.addEventListener("input", () => setRank(number.value, "number", true));
  number.addEventListener("blur", () => setRank(number.value, null));
  $$(".slider-tick", root).forEach((tick) => tick.addEventListener("click", () => {
    setRank(Number(tick.dataset.rank), null, true);
    slider.focus({ preventScroll: true });
  }));
  setRank(number.value, null);
  return {
    value: () => clamp(Math.round(Number(number.value) || 1), 1, rankPopulation),
    setRank: (rank) => setRank(rank, null, true),
  };
}

const ICON_MUTED = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"></polygon><line x1="22" y1="9" x2="16" y2="15"></line><line x1="16" y1="9" x2="22" y2="15"></line></svg>';
const ICON_SOUND = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"></polygon><path d="M15.54 8.46a5 5 0 0 1 0 7.07"></path><path d="M19.07 4.93a10 10 0 0 1 0 14.14"></path></svg>';

function challengeCardHTML(item, label) {
  const map = item.beatmap || {};
  const mapTitle = `${map.artist ? `${map.artist} — ` : ""}${map.title || "Unknown map"}`;
  const mapStats = `${map.version || "Unknown difficulty"} · ${Number(item.star || 0).toFixed(2)}★ · ${Number(item.accuracyPercent || 0).toFixed(2)}% · ${(item.mods || ["NM"]).join("")}`;
  return `
    <div class="challenge-shell">
      <div class="challenge-topline"><span>${escapeHTML(label)}</span><span class="attempt-copy">five guesses · within 10%</span></div>
      <div class="challenge-stage">
        <aside class="stage-left"><ol class="guess-list" aria-label="Previous guesses"></ol></aside>
        <div class="video-wrap">
          <video class="challenge-video" src="${escapeHTML(item.videoURL)}" autoplay muted loop playsinline preload="auto"></video>
          <button class="video-toggle" type="button" aria-label="Toggle sound">${ICON_MUTED}</button>
          <button class="video-play" type="button" aria-label="Play or pause replay">pause</button>
        </div>
        <aside class="stage-info"><strong>${escapeHTML(mapTitle)}</strong><span>${escapeHTML(mapStats)}</span></aside>
      </div>
      <div class="guess-zone">
        <form class="guess-form">
          ${rankControlHTML()}
          <button class="primary-button guess-submit" type="submit">submit guess</button>
        </form>
        <p class="challenge-error" hidden></p>
        <div class="reveal-panel" hidden></div>
      </div>
    </div>`;
}

function bindChallengeVideo(root) {
  const video = $(".challenge-video", root);
  const sound = $(".video-toggle", root);
  const play = $(".video-play", root);
  if (!video) return;
  video.muted = true;
  video.play?.().catch(() => {});

  const syncPlayLabel = () => { play.textContent = video.paused ? "play" : "pause"; };
  const togglePlay = () => {
    if (video.paused) video.play?.().catch(() => {});
    else video.pause();
    syncPlayLabel();
  };
  video.addEventListener("click", togglePlay);
  play.addEventListener("click", togglePlay);
  video.addEventListener("play", syncPlayLabel);
  video.addEventListener("pause", syncPlayLabel);
  sound.addEventListener("click", () => {
    video.muted = !video.muted;
    if (!video.muted && video.paused) video.play?.().catch(() => {});
    sound.innerHTML = video.muted ? ICON_MUTED : ICON_SOUND;
    sound.classList.toggle("on", !video.muted);
  });
}

function feedbackText(result) {
  if (result.correct) return "correct";
  if (result.direction === "better") return "actual is better";
  return "actual is worse";
}

function renderDistribution(distribution, round) {
  if (!distribution?.count) {
    return `<section class="distribution"><h3>community first guesses</h3><p>No other first guesses recorded yet.</p></section>`;
  }
  const bins = distribution.bins || [];
  const maximum = Math.max(1, ...bins.map((bin) => Number(bin.count) || 0));
  const bars = bins.map((bin) => {
    const count = Number(bin.count) || 0;
    const height = count ? Math.max(3, Math.round(count / maximum * 76)) : 2;
    const title = `${formatRank(bin.lower)}–${formatRank(bin.upper)}: ${count}`;
    return `<i style="height:${height}px" title="${escapeHTML(title)}"><span>${count || ""}</span></i>`;
  }).join("");
  return `
    <section class="distribution">
      <div class="distribution-head"><h3>community first guesses</h3><span>${Number(distribution.count).toLocaleString()} total</span></div>
      <div class="histogram" aria-label="Community guess histogram">${bars}</div>
      <div class="distribution-stats">
        <span>median <b>${formatRank(distribution.medianRank)}</b></span>
        <span>middle 50% <b>${formatRank(distribution.q25Rank)}–${formatRank(distribution.q75Rank)}</b></span>
        <span>you <b>${formatRank(round.guesses[0]?.guessRank)}</b></span>
      </div>
    </section>`;
}

async function fetchDistribution(round, mode, challengeDate) {
  const query = new URLSearchParams({ mode });
  if (challengeDate) query.set("challengeDate", challengeDate);
  const payload = await requestJSON(`/api/challenge/${encodeURIComponent(round.item.id)}/distribution?${query}`);
  round.distribution = payload.distribution;
  updateChallengeRound(round, mode, challengeDate);
  if (mode === "daily") saveDailyState();
}

async function submitChallengeGuess(round, mode, challengeDate) {
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
}

function updateChallengeRound(round, mode, challengeDate) {
  if (!round.root) return;
  const root = round.root;
  $(".guess-list", root).innerHTML = round.guesses.map((guess, index) => `
    <li class="${guess.correct ? "hit" : guess.closeness || "far"}">
      <span>${String(index + 1).padStart(2, "0")}</span>
      <strong>${formatRank(guess.guessRank)}</strong>
      <em>${escapeHTML(feedbackText(guess))}</em>
    </li>`).join("");

  const attemptCopy = $(".attempt-copy", root);
  const attemptsLeft = Math.max(0, MAX_ATTEMPTS - round.guesses.length);
  attemptCopy.textContent = round.revealed ? "answer revealed" : `${attemptsLeft} guess${attemptsLeft === 1 ? "" : "es"} left · within 10%`;

  const form = $(".guess-form", root);
  form.hidden = round.revealed;
  const panel = $(".reveal-panel", root);
  panel.hidden = !round.revealed;
  if (!round.revealed) return;

  const ratio = Math.max(round.actualRank, round.predictedRank) / Math.max(1, Math.min(round.actualRank, round.predictedRank));
  const community = mode === "daily"
    ? `<div class="reveal-community">${renderDistribution(round.distribution, round)}</div>`
    : "";
  panel.innerHTML = `
    <div class="reveal-stats">
      <div class="reveal-answer">
        <span>actual rank</span>
        <strong>${formatRank(round.actualRank)}</strong>
        <small>${escapeHTML(round.player || "player")} · model ${formatRank(round.predictedRank)} · ${ratio.toFixed(2)}×</small>
      </div>
      ${community}
    </div>
    <button class="secondary-button next-challenge" type="button">${mode === "daily" ? "next replay" : "generate another"}</button>`;
  $(".next-challenge", panel).addEventListener("click", () => mode === "daily" ? advanceDaily() : loadInfinite());
  if (mode === "daily" && !round.distribution) fetchDistribution(round, mode, challengeDate).catch(() => {});
}

function mountChallenge(rootElement, item, round, label, mode, challengeDate = null) {
  rootElement.innerHTML = challengeCardHTML(item, label);
  round.root = rootElement;
  round.rankControl = bindRankControl(rootElement);
  bindChallengeVideo(rootElement);
  const form = $(".guess-form", rootElement);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $(".guess-submit", form);
    const error = $(".challenge-error", rootElement);
    error.hidden = true;
    button.disabled = true;
    button.textContent = "checking…";
    try { await submitChallengeGuess(round, mode, challengeDate); }
    catch (failure) {
      error.textContent = failure.message || "Guess failed.";
      error.hidden = false;
    } finally {
      button.disabled = false;
      button.textContent = "submit guess";
    }
  });
  updateChallengeRound(round, mode, challengeDate);
}

function dailyStorageKey(date = dailyPayload?.date) { return date ? `osu-rankguess-daily-v3-${date}` : ""; }
function saveDailyState() {
  if (!dailyPayload || !dailyState) return;
  const serializable = {
    current: dailyState.current,
    rounds: dailyState.rounds.map(({ item, guesses, revealed, actualRank, predictedRank, player, distribution }) => ({
      id: item.id, guesses, revealed, actualRank, predictedRank, player, distribution,
    })),
  };
  storage.set(dailyStorageKey(), JSON.stringify(serializable));
}

function restoreDailyState(payload) {
  let saved = null;
  try { saved = JSON.parse(storage.get(dailyStorageKey(payload.date)) || "null"); } catch { saved = null; }
  const rounds = payload.replays.map((item) => {
    const previous = saved?.rounds?.find((round) => round.id === item.id) || {};
    return {
      item,
      guesses: previous.guesses || [],
      revealed: Boolean(previous.revealed),
      actualRank: previous.actualRank,
      predictedRank: previous.predictedRank,
      player: previous.player,
      distribution: previous.distribution || null,
    };
  });
  return { current: clamp(Number(saved?.current) || 0, 0, rounds.length), rounds };
}

async function loadDaily() {
  const root = $("#dailyRoot");
  root.innerHTML = '<p class="empty-state">Loading today\'s set…</p>';
  try {
    dailyPayload = await requestJSON("/api/challenge/daily");
    rankPopulation = Number(dailyPayload.rankPopulation) || rankPopulation;
    if (!dailyPayload.available) {
      root.innerHTML = `<section class="mode-intro"><p class="kicker">daily</p><h1>daily is warming up.</h1><p>Three eligible public replays are required. Available now: ${Number(dailyPayload.eligibleReplays || 0)}.</p><button class="secondary-button narrow" data-view-link="analyze" type="button">submit a replay</button></section>`;
      return;
    }
    dailyState = restoreDailyState(dailyPayload);
    renderDaily();
  } catch (error) {
    root.innerHTML = `<p class="empty-state">${escapeHTML(error.message)}</p>`;
  }
}

function renderDaily() {
  const root = $("#dailyRoot");
  if (dailyState.current >= dailyState.rounds.length) return renderDailySummary();
  const progress = `
    <div class="daily-progress">
      <div>${dailyState.rounds.map((round, index) => `<button type="button" data-daily-index="${index}" class="${round.revealed ? "done" : index === dailyState.current ? "current" : ""}" ${index > dailyState.current ? "disabled" : ""}>${index + 1}</button>`).join("")}</div>
      <span>${escapeHTML(dailyPayload.date)}</span>
    </div>`;
  root.innerHTML = `${progress}<div id="dailyChallengeMount"></div>`;
  $$('[data-daily-index]', root).forEach((button) => button.addEventListener("click", () => {
    dailyState.current = Number(button.dataset.dailyIndex);
    saveDailyState();
    renderDaily();
  }));
  const round = dailyState.rounds[dailyState.current];
  mountChallenge($("#dailyChallengeMount"), round.item, round, `daily ${dailyState.current + 1} / 3`, "daily", dailyPayload.date);
}

function advanceDaily() {
  dailyState.current += 1;
  saveDailyState();
  renderDaily();
  $("#dailyRoot").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
}

function shareGrid() {
  return dailyState.rounds.map((round) => {
    const solvedAt = round.guesses.findIndex((guess) => guess.correct);
    if (solvedAt < 0) return "⬛⬛⬛⬛⬛";
    return `${"⬛".repeat(solvedAt)}🟩${"⬜".repeat(4 - solvedAt)}`;
  }).join("\n");
}

function renderDailySummary() {
  const root = $("#dailyRoot");
  const grid = shareGrid();
  root.innerHTML = `
    <section class="daily-summary">
      <p class="kicker">${escapeHTML(dailyPayload.date)}</p>
      <h1>daily complete.</h1>
      <div class="share-grid" aria-label="Daily result">${grid.replaceAll("\n", "<br>")}</div>
      <div class="summary-table">${dailyState.rounds.map((round, index) => `<div><span>${index + 1}</span><b>${formatRank(round.actualRank)}</b><em>${round.guesses.length} guess${round.guesses.length === 1 ? "" : "es"}</em></div>`).join("")}</div>
      <button class="primary-button narrow" id="shareDaily" type="button">share result</button>
    </section>`;
  $("#shareDaily").addEventListener("click", async () => {
    const text = `osu!rankguess ${dailyPayload.date}\n${grid}\nhttps://osu-rankguess.vercel.app/#daily`;
    try {
      if (navigator.share) await navigator.share({ text });
      else await copyText(text);
      $("#shareDaily").textContent = navigator.share ? "shared" : "copied";
    } catch {
      await copyText(text);
      $("#shareDaily").textContent = "copied";
    }
  });
}

async function loadInfinite() {
  const root = $("#infiniteRoot");
  const started = Date.now();
  root.innerHTML = `
    <section class="generation-card">
      <div class="busy-line"><i></i><span>selecting and rendering a fresh replay</span></div>
      <strong id="generationTime">0:00</strong>
      <p>This request creates a new clip. Keep the tab open.</p>
    </section>`;
  const timer = setInterval(() => {
    const elapsed = Math.floor((Date.now() - started) / 1000);
    const element = $("#generationTime");
    if (element) element.textContent = `${Math.floor(elapsed / 60)}:${String(elapsed % 60).padStart(2, "0")}`;
  }, 1000);
  try {
    const payload = await requestJSON("/api/challenge/infinite", { method: "POST" });
    rankPopulation = Number(payload.rankPopulation) || rankPopulation;
    if (!payload.available) throw new Error("Infinite mode is not configured.");
    infiniteRound = { item: payload.replay, guesses: [], revealed: false };
    mountChallenge(root, payload.replay, infiniteRound, "infinite · fresh", "infinite");
  } catch (error) {
    root.innerHTML = `<section class="mode-intro"><p class="kicker">infinite</p><h1>could not prepare a replay.</h1><p>${escapeHTML(error.message)}</p><button class="primary-button narrow" id="retryInfinite" type="button">try again</button></section>`;
    $("#retryInfinite")?.addEventListener("click", loadInfinite);
  } finally {
    clearInterval(timer);
  }
}
$("#startInfinite").addEventListener("click", loadInfinite);

function predictionRatio(item) {
  if (!item.actualRank || !item.predictedRank) return Infinity;
  return Math.max(item.actualRank, item.predictedRank) / Math.max(1, Math.min(item.actualRank, item.predictedRank));
}

function galleryCard(item) {
  const map = item.beatmap || {};
  const thumbnail = item.thumbnailURL || `/api/gallery/${encodeURIComponent(item.id)}/thumbnail`;
  const sourceLabel = item.source === "cron" ? "automatic sample" : "community upload";
  const ratio = predictionRatio(item);
  const errorLabel = Number.isFinite(ratio) ? `${ratio.toFixed(2)}× rank ratio` : "rank unavailable";
  const errorWidth = Number.isFinite(ratio) ? Math.min(100, Math.max(4, Math.log10(Math.max(1, ratio)) / 2 * 100)) : 4;
  return `
    <article class="gallery-card" data-gallery-id="${escapeHTML(item.id)}" tabindex="0" role="button" aria-label="Open replay by ${escapeHTML(item.player || "unknown player")}">
      <button class="gallery-thumb" type="button" aria-label="Open replay">
        <img src="${escapeHTML(thumbnail)}" alt="" loading="lazy" decoding="async" />
        <span>watch</span>
      </button>
      <div class="gallery-copy">
        <p class="gallery-source">${escapeHTML(sourceLabel)}</p>
        <h2>${escapeHTML(item.player || "Unknown player")}</h2>
        <p>${escapeHTML(`${map.artist ? `${map.artist} — ` : ""}${map.title || "Unknown map"}`)}</p>
        <small>${escapeHTML(`${map.version || "Unknown difficulty"} · ${Number(item.star || 0).toFixed(2)}★ · ${(item.mods || ["NM"]).join("")}`)}</small>
      </div>
      <div class="gallery-ranks"><div><span>actual</span><strong>${formatRank(item.actualRank)}</strong></div><div><span>model</span><strong>${formatRank(item.predictedRank)}</strong></div></div>
      <div class="gallery-error"><i style="width:${errorWidth}%"></i><span>${escapeHTML(errorLabel)}</span></div>
    </article>`;
}

function openGalleryDialog(item) {
  if (!item) return;
  const map = item.beatmap || {};
  const ratio = predictionRatio(item);
  $("#galleryDialogBody").innerHTML = `
    <video src="${escapeHTML(item.videoURL)}" controls autoplay playsinline preload="metadata"></video>
    <div class="dialog-copy">
      <p class="kicker">${escapeHTML(item.source === "cron" ? "automatic sample" : "community upload")}</p>
      <h1>${escapeHTML(item.player || "Unknown player")}</h1>
      <p>${escapeHTML(`${map.artist ? `${map.artist} — ` : ""}${map.title || "Unknown map"} [${map.version || "?"}]`)}</p>
      <div class="dialog-ranks"><div><span>actual</span><strong>${formatRank(item.actualRank)}</strong></div><div><span>model</span><strong>${formatRank(item.predictedRank)}</strong></div><div><span>ratio</span><strong>${Number.isFinite(ratio) ? `${ratio.toFixed(2)}×` : "—"}</strong></div></div>
      <a href="${escapeHTML(item.videoURL)}" target="_blank" rel="noreferrer">open video in a new tab</a>
    </div>`;
  const dialog = $("#galleryDialog");
  if (dialog.showModal) dialog.showModal();
  else dialog.setAttribute("open", "");
}

function renderGallery() {
  let items = galleryItems.filter((item) => galleryFilter === "all" || item.source === galleryFilter);
  const sort = $("#gallerySort").value;
  if (sort === "error") items = [...items].sort((a, b) => predictionRatio(b) - predictionRatio(a));
  if (sort === "closest") items = [...items].sort((a, b) => predictionRatio(a) - predictionRatio(b));
  $("#galleryGrid").innerHTML = items.map(galleryCard).join("");
  $$(".gallery-card").forEach((card) => {
    const item = galleryItems.find((candidate) => candidate.id === card.dataset.galleryId);
    $(".gallery-thumb", card).addEventListener("click", () => openGalleryDialog(item));
    card.addEventListener("click", (event) => { if (!event.target.closest("button, a, input, select")) openGalleryDialog(item); });
    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openGalleryDialog(item);
      }
    });
  });
  $("#galleryEmpty").hidden = items.length !== 0;
}

async function loadGallery(reset = false) {
  const empty = $("#galleryEmpty");
  const more = $("#loadMoreGallery");
  if (reset) {
    galleryOffset = 0;
    galleryItems = [];
    $("#galleryGrid").innerHTML = '<p class="empty-state">Loading gallery…</p>';
  }
  try {
    const payload = await requestJSON(`/api/gallery?limit=24&offset=${galleryOffset}`);
    if (!payload.configured) {
      empty.textContent = "Gallery storage is not configured.";
      empty.hidden = false;
      more.hidden = true;
      galleryLoaded = true;
      $("#galleryGrid").innerHTML = "";
      return;
    }
    galleryItems.push(...payload.items.filter((item) => !galleryItems.some((existing) => existing.id === item.id)));
    galleryOffset += payload.items.length;
    more.hidden = galleryOffset >= payload.total;
    galleryLoaded = true;
    renderGallery();
  } catch (error) {
    empty.textContent = error.message;
    empty.hidden = false;
  }
}

$$('[data-gallery-filter]').forEach((button) => button.addEventListener("click", () => {
  galleryFilter = button.dataset.galleryFilter;
  $$('[data-gallery-filter]').forEach((candidate) => candidate.classList.toggle("active", candidate === button));
  renderGallery();
}));
$("#gallerySort").addEventListener("change", renderGallery);
$("#randomGallery").addEventListener("click", () => {
  const visible = galleryItems.filter((item) => galleryFilter === "all" || item.source === galleryFilter);
  if (visible.length) openGalleryDialog(visible[Math.floor(Math.random() * visible.length)]);
});
$("#loadMoreGallery").addEventListener("click", () => loadGallery(false));
$("#closeGalleryDialog").addEventListener("click", () => $("#galleryDialog").close());
$("#galleryDialog").addEventListener("click", (event) => { if (event.target === $("#galleryDialog")) $("#galleryDialog").close(); });

window.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && $("#galleryDialog").open) $("#galleryDialog").close();
  if (event.altKey && ["1", "2", "3", "4"].includes(event.key)) {
    showView({ "1": "daily", "2": "infinite", "3": "analyze", "4": "gallery" }[event.key]);
  }
});

requestJSON("/api/health").then((health) => {
  rankPopulation = Number(health.rankPopulation) || rankPopulation;
  $("#modelFooter").textContent = `${health.modelVersion || "rank model"} · estimates are approximate`;
}).catch(() => {});

const initialView = location.hash.replace("#", "");
showView(["daily", "infinite", "analyze", "gallery"].includes(initialView) ? initialView : "daily");
