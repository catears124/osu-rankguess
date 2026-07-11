const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const replayInput = $("#replayInput");
const dropzone = $("#dropzone");
const runButton = $("#runButton");
const errorBox = $("#errorBox");
const results = $("#results");
const renderStatus = $("#renderStatus");

let selectedFile = null;
let activeRun = 0;
let galleryOffset = 0;
let galleryLoaded = false;
let dailyPayload = null;
let dailyState = null;
let infiniteRound = null;

const escapeHTML = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

const formatBytes = (bytes) => {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
};

const formatRank = (value) => value ? `#${Number(value).toLocaleString()}` : "—";
const formatTopPercent = (value) => {
  if (value < 0.001) return `${value.toExponential(2)}%`;
  if (value < 0.1) return `${value.toFixed(3)}%`;
  if (value < 1) return `${value.toFixed(2)}%`;
  return `${value.toFixed(1)}%`;
};
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const apiError = async (response) => {
  const payload = await response.json().catch(() => ({}));
  const detail = payload.detail || payload;
  return new Error(detail.message || `Request failed (${response.status})`);
};

const requestJSON = async (url, options = {}) => {
  const response = await fetch(url, options);
  if (!response.ok) throw await apiError(response);
  return response.json();
};

const sha256File = async (file) => {
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
};

const isValidHttpsVideoURL = (value) => {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && (url.hostname === "issou.best" || url.hostname.endsWith(".issou.best"));
  } catch {
    return false;
  }
};

function showView(name) {
  $$(".view").forEach((view) => {
    const active = view.dataset.view === name;
    view.hidden = !active;
    view.classList.toggle("active", active);
  });
  $$("[data-view-link]").forEach((link) => link.classList.toggle("active", link.dataset.viewLink === name));
  history.replaceState(null, "", `#${name}`);
  if (name === "gallery" && !galleryLoaded) loadGallery(true);
  if (name === "daily" && !dailyPayload) loadDaily();
}

$$("[data-view-link]").forEach((link) => link.addEventListener("click", (event) => {
  event.preventDefault();
  showView(link.dataset.viewLink);
}));

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
  if (!file.name.toLowerCase().endsWith(".osr")) return showError("That is not a .osr replay file.");
  if (file.size > 4_000_000) return showError("Replay exceeds the 4 MB upload limit.");
  selectedFile = file;
  $("#dropTitle").textContent = "Replay selected";
  $("#dropSubtitle").textContent = "Ready to parse and render";
  $("#fileName").textContent = file.name;
  $("#fileSize").textContent = formatBytes(file.size);
  $("#fileChip").hidden = false;
  runButton.disabled = false;
  results.hidden = true;
  hideError();
  resetSteps();
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
    if (runID !== activeRun) throw new Error("Cancelled");
    const payload = await requestJSON(`/api/ordr/status?renderID=${encodeURIComponent(renderID)}`);
    renderStatus.hidden = false;
    renderStatus.textContent = `o!rdr #${renderID} · ${payload.progress || "working"}`;
    if (payload.failed) throw new Error(`o!rdr failed with code ${payload.errorCode}`);
    if (payload.ready) return payload;
    await sleep(3000);
  }
  throw new Error("o!rdr did not finish within 15 minutes.");
}

async function runPrediction(body) {
  return requestJSON("/api/predict", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function renderResult(data) {
  $("#resultPlayer").textContent = data.player || "PLAYER";
  $("#predictedRank").textContent = formatRank(data.predictedRank);
  $("#rankContext").textContent = `${formatTopPercent(data.topPercent)} of ranked osu!standard players`;
  $("#topPercent").textContent = formatTopPercent(data.topPercent);
  $("#accuracyValue").textContent = `${data.accuracyPercent.toFixed(2)}%`;
  $("#modsValue").textContent = data.mods.join("");
  $("#confidenceLabel").textContent = data.confidence;
  $("#starValue").textContent = `${data.beatmap.star.toFixed(2)}★`;
  $("#eventsValue").textContent = data.eventCount.toLocaleString();
  $("#mapTitle").textContent = `${data.beatmap.artist ? `${data.beatmap.artist} — ` : ""}${data.beatmap.title}`;
  $("#mapMeta").textContent = `${data.beatmap.version || "Unknown difficulty"} · ${Math.round(data.beatmap.lengthSeconds)}s`;

  const comparison = $("#rankComparison");
  comparison.hidden = !data.actualRank;
  if (data.actualRank) $("#actualRank").textContent = formatRank(data.actualRank);

  const video = $("#replayVideo");
  video.src = data.videoURL;
  video.load();
  $("#videoLink").href = data.videoURL;
  $("#galleryStatus").textContent = data.gallerySaved
    ? "Saved to the public gallery."
    : ($("#publishToggle").checked ? "Prediction complete. Gallery storage is not configured or could not save." : "Kept private.");

  results.hidden = false;
  results.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function analyze() {
  if (!selectedFile) return;
  const runID = ++activeRun;
  hideError();
  resetSteps();
  results.hidden = true;
  runButton.disabled = true;
  runButton.textContent = "CLANKING…";
  try {
    setStep(0, "active", "Computing hash and decoding replay");
    const replayHash = await sha256File(selectedFile);
    const cached = await cacheReplay(selectedFile, replayHash);
    if (runID !== activeRun) return;
    setStep(0, "done", `${cached.eventCount.toLocaleString()} events · ${cached.player}`);

    setStep(1, "active", `Submitting as ${cached.player}`);
    const render = await createRender(selectedFile, replayHash, cached.player);
    if (runID !== activeRun) return;
    setStep(1, "done", `Render #${render.renderID} accepted`);

    setStep(2, "active", "Waiting in o!rdr queue");
    const rendered = await waitForRender(render.renderID, runID);
    if (runID !== activeRun) return;
    setStep(2, "done", "Video is ready");

    setStep(3, "active", "Reading structured render metadata");
    if (!isValidHttpsVideoURL(rendered.videoURL)) throw new Error("o!rdr has not produced a usable HTTPS video URL.");
    setStep(3, "done", "Map metadata recovered");

    setStep(4, "active", "Running ONNX ensemble");
    const prediction = await runPrediction({
      replayHash,
      cacheToken: cached.cacheToken,
      renderID: render.renderID,
      description: rendered.description || rendered.title || "",
      renderMetadata: rendered.renderMetadata || {},
      videoURL: rendered.videoURL,
      publish: $("#publishToggle").checked,
    });
    if (runID !== activeRun) return;
    setStep(4, "done", "Prediction complete");
    renderStatus.textContent = `o!rdr #${render.renderID} · Done`;
    renderResult(prediction);
    galleryLoaded = false;
  } catch (error) {
    failCurrentStep(error.message || "Pipeline failed");
    showError(error.message || "Pipeline failed");
  } finally {
    if (runID === activeRun) {
      runButton.disabled = false;
      runButton.textContent = "RUN THE CONTRAPTION";
    }
  }
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
runButton.addEventListener("click", analyze);
$("#resetButton").addEventListener("click", () => {
  activeRun += 1;
  selectedFile = null;
  replayInput.value = "";
  results.hidden = true;
  $("#fileChip").hidden = true;
  $("#dropTitle").textContent = "Choose a .osr replay";
  $("#dropSubtitle").textContent = "Drop it here or click to browse";
  $("#replayVideo").removeAttribute("src");
  runButton.disabled = true;
  hideError();
  resetSteps();
  window.scrollTo({ top: 0, behavior: "smooth" });
});

function challengeCardHTML(item, label) {
  const map = item.beatmap || {};
  return `
    <div class="challenge-shell">
      <section class="card challenge-panel">
        <div class="challenge-head"><span>${escapeHTML(label)}</span><span>${Number(item.star).toFixed(2)}★ · ${Number(item.accuracyPercent).toFixed(2)}%</span></div>
        <video class="challenge-video" src="${escapeHTML(item.videoURL)}" controls playsinline preload="metadata"></video>
        <div class="challenge-map"><strong>${escapeHTML(`${map.artist ? `${map.artist} — ` : ""}${map.title}`)}</strong><span>${escapeHTML(`${map.version || "Unknown difficulty"} · ${(item.mods || ["NM"]).join("")}`)}</span></div>
      </section>
      <section class="card guess-panel">
        <h2>Guess the global rank</h2>
        <p>Within 10% counts as correct. Smaller rank numbers are better.</p>
        <form class="guess-form"><input type="number" min="1" max="100000000" placeholder="e.g. 25000" required /><button class="primary-button" type="submit">Guess</button></form>
        <ol class="guess-list"></ol>
        <div class="answer-box" hidden></div>
        <div class="challenge-actions" hidden></div>
      </section>
    </div>`;
}

function feedbackText(result) {
  if (result.correct) return "Correct — within 10%";
  if (result.direction === "better") return "Actual rank is better (smaller)";
  return "Actual rank is worse (larger)";
}

async function submitChallengeGuess(round, mode, challengeDate) {
  const root = round.root;
  const input = $(".guess-form input", root);
  const guessRank = Number(input.value);
  if (!Number.isInteger(guessRank) || guessRank < 1) return;
  const attempt = round.guesses.length + 1;
  const result = await requestJSON("/api/challenge/guess", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ replayID: round.item.id, guessRank, attempt, mode, challengeDate }),
  });
  round.guesses.push({ guessRank, ...result });
  if (result.revealed) {
    round.revealed = true;
    round.actualRank = result.actualRank;
    round.predictedRank = result.predictedRank;
    round.player = result.player;
  }
  input.value = "";
  updateChallengeRound(round, mode, challengeDate);
  if (mode === "daily") saveDailyState();
}

function updateChallengeRound(round, mode, challengeDate) {
  const root = round.root;
  const list = $(".guess-list", root);
  list.innerHTML = round.guesses.map((guess) => `<li><span>${formatRank(guess.guessRank)}</span><span>${escapeHTML(feedbackText(guess))}</span></li>`).join("");
  const form = $(".guess-form", root);
  form.hidden = round.revealed;
  const answer = $(".answer-box", root);
  answer.hidden = !round.revealed;
  if (round.revealed) {
    answer.innerHTML = `<span>${escapeHTML(round.player || "Player")}</span><strong>${formatRank(round.actualRank)}</strong><small>AI predicted ${formatRank(round.predictedRank)}</small>`;
    const actions = $(".challenge-actions", root);
    actions.hidden = false;
    actions.innerHTML = `<button class="secondary-button next-challenge" type="button">${mode === "daily" ? "Next replay" : "Another replay"}</button>`;
    $(".next-challenge", actions).addEventListener("click", () => {
      if (mode === "daily") advanceDaily(); else loadInfinite(round.item.id);
    });
  }
}

function mountChallenge(rootElement, item, round, label, mode, challengeDate = null) {
  rootElement.innerHTML = challengeCardHTML(item, label);
  round.root = rootElement;
  const form = $(".guess-form", rootElement);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("button", form);
    button.disabled = true;
    try { await submitChallengeGuess(round, mode, challengeDate); }
    catch (error) { alert(error.message || "Guess failed"); }
    finally { button.disabled = false; }
  });
  updateChallengeRound(round, mode, challengeDate);
}

function dailyStorageKey() { return dailyPayload ? `osu-rankguess-daily-${dailyPayload.date}` : ""; }
function saveDailyState() {
  if (!dailyPayload || !dailyState) return;
  const serializable = {
    current: dailyState.current,
    rounds: dailyState.rounds.map(({ item, guesses, revealed, actualRank, predictedRank, player }) => ({ id: item.id, guesses, revealed, actualRank, predictedRank, player })),
  };
  localStorage.setItem(dailyStorageKey(), JSON.stringify(serializable));
}

function restoreDailyState(payload) {
  const saved = JSON.parse(localStorage.getItem(`osu-rankguess-daily-${payload.date}`) || "null");
  const rounds = payload.replays.map((item) => {
    const old = saved?.rounds?.find((round) => round.id === item.id) || {};
    return { item, guesses: old.guesses || [], revealed: old.revealed || false, actualRank: old.actualRank, predictedRank: old.predictedRank, player: old.player };
  });
  return { current: Math.min(saved?.current || 0, 2), rounds };
}

async function loadDaily() {
  const root = $("#dailyRoot");
  root.innerHTML = '<p class="empty-state">Loading today\'s challenge…</p>';
  try {
    dailyPayload = await requestJSON("/api/challenge/daily");
    if (!dailyPayload.available) {
      root.innerHTML = `<p class="empty-state">Daily challenge needs three public submissions with known osu! ranks. Eligible now: ${dailyPayload.eligibleReplays || 0}.</p>`;
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
  const progress = `<div class="daily-progress">${dailyState.rounds.map((round, index) => `<span class="${round.revealed ? "done" : index === dailyState.current ? "current" : ""}">${index + 1}</span>`).join("")}</div>`;
  if (dailyState.current >= dailyState.rounds.length) return renderDailySummary();
  root.innerHTML = progress + '<div id="dailyChallengeMount"></div>';
  const round = dailyState.rounds[dailyState.current];
  mountChallenge($("#dailyChallengeMount"), round.item, round, `Daily ${dailyState.current + 1} / 3`, "daily", dailyPayload.date);
}

function advanceDaily() {
  dailyState.current += 1;
  saveDailyState();
  renderDaily();
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
  root.innerHTML = `<section class="card daily-summary"><p class="kicker">Complete · ${escapeHTML(dailyPayload.date)}</p><h2>Daily finished</h2><div class="share-grid">${grid}</div><div class="challenge-actions"><button class="primary-button narrow" id="shareDaily">Copy result</button></div></section>`;
  $("#shareDaily").addEventListener("click", async () => {
    await navigator.clipboard.writeText(`osu!rankguess ${dailyPayload.date}\n${grid}\nhttps://osu-rankguess.vercel.app/#daily`);
    $("#shareDaily").textContent = "Copied";
  });
}

async function loadInfinite(exclude = null) {
  const root = $("#infiniteRoot");
  root.innerHTML = '<p class="empty-state">Loading replay…</p>';
  try {
    const payload = await requestJSON(`/api/challenge/infinite${exclude ? `?exclude=${encodeURIComponent(exclude)}` : ""}`);
    if (!payload.available) {
      root.innerHTML = '<p class="empty-state">Infinite mode needs at least one public submission with a known osu! rank.</p>';
      return;
    }
    infiniteRound = { item: payload.replay, guesses: [], revealed: false };
    mountChallenge(root, payload.replay, infiniteRound, "Infinite", "infinite");
  } catch (error) {
    root.innerHTML = `<p class="empty-state">${escapeHTML(error.message)}</p>`;
  }
}
$("#startInfinite").addEventListener("click", () => loadInfinite());

function galleryCard(item) {
  const map = item.beatmap || {};
  const thumbnail = item.thumbnailURL || `/api/gallery/${encodeURIComponent(item.id)}/thumbnail`;
  const sourceLabel = item.source === "cron" ? "found by the replay goblin" : "donated by a visitor";
  const mapLabel = `${map.artist ? `${map.artist} — ` : ""}${map.title || "Unknown map"}`;
  const detailLabel = `${map.version || "mystery diff"} · ${Number(item.star || 0).toFixed(2)}★ · ${(item.mods || ["NM"]).join("")}`;

  return `
    <article class="gallery-card">
      <a class="gallery-thumb" href="${escapeHTML(item.videoURL)}" target="_blank" rel="noreferrer" aria-label="Watch ${escapeHTML(item.player || "this replay")}">
        <img src="${escapeHTML(thumbnail)}" alt="" loading="lazy" decoding="async" onerror="this.hidden=true">
        <span>WATCH REPLAY ↗</span>
      </a>
      <div class="gallery-copy">
        <p class="gallery-source">${escapeHTML(sourceLabel)}</p>
        <h3>${escapeHTML(item.player || "Unknown player")}</h3>
        <p>${escapeHTML(mapLabel)}<br>${escapeHTML(detailLabel)}</p>
      </div>
      <div class="gallery-ranks">
        <div><span>REAL RANK</span><strong>${formatRank(item.actualRank)}</strong></div>
        <div><span>ROBOT SAID</span><strong>${formatRank(item.predictedRank)}</strong></div>
      </div>
    </article>`;
}

async function loadGallery(reset = false) {
  const grid = $("#galleryGrid");
  const empty = $("#galleryEmpty");
  const more = $("#loadMoreGallery");
  if (reset) { galleryOffset = 0; grid.innerHTML = ""; }
  try {
    const payload = await requestJSON(`/api/gallery?limit=24&offset=${galleryOffset}`);
    if (!payload.configured) {
      empty.textContent = "the public pile has nowhere to live. connect postgres.";
      empty.hidden = false;
      more.hidden = true;
      galleryLoaded = true;
      return;
    }
    grid.insertAdjacentHTML("beforeend", payload.items.map(galleryCard).join(""));
    galleryOffset += payload.items.length;
    empty.hidden = payload.total !== 0;
    more.hidden = galleryOffset >= payload.total;
    galleryLoaded = true;
  } catch (error) {
    empty.textContent = error.message;
    empty.hidden = false;
  }
}
$("#loadMoreGallery").addEventListener("click", () => loadGallery(false));

const initialView = location.hash.replace("#", "");
showView(["analyze", "daily", "infinite", "gallery"].includes(initialView) ? initialView : "analyze");
