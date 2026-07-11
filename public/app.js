const $ = (selector) => document.querySelector(selector);

const replayInput = $("#replayInput");
const dropzone = $("#dropzone");
const runButton = $("#runButton");
const errorBox = $("#errorBox");
const results = $("#results");
const renderStatus = $("#renderStatus");

let selectedFile = null;
let activeRun = 0;

const formatBytes = (bytes) => {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
};

const formatTopPercent = (value) => {
  if (value < 0.001) return `${value.toExponential(2)}%`;
  if (value < 0.1) return `${value.toFixed(3)}%`;
  if (value < 1) return `${value.toFixed(2)}%`;
  return `${value.toFixed(1)}%`;
};

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

const isValidHttpsVideoURL = (value) => {
  if (typeof value !== "string" || !value.trim()) return false;
  try {
    const url = new URL(value);
    return (
      url.protocol === "https:" &&
      (url.hostname === "issou.best" || url.hostname.endsWith(".issou.best"))
    );
  } catch {
    return false;
  }
};

const apiError = async (response) => {
  const payload = await response.json().catch(() => ({}));
  const detail = payload.detail || payload;
  const error = new Error(detail.message || `Request failed (${response.status})`);
  error.code = detail.code;
  error.payload = detail;
  return error;
};

const sha256File = async (file) => {
  const bytes = await file.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
};

const showError = (message) => {
  errorBox.textContent = message;
  errorBox.hidden = false;
};

const hideError = () => {
  errorBox.hidden = true;
};

const resetSteps = () => {
  document.querySelectorAll(".steps li").forEach((item) => {
    item.classList.remove("active", "done", "failed");
    const detail = item.querySelector("small");
    detail.textContent = item.dataset.defaultDetail || detail.textContent;
  });
  renderStatus.hidden = true;
  renderStatus.textContent = "";
};

const setStep = (index, state, detail = null) => {
  const steps = [...document.querySelectorAll(".steps li")];
  const item = steps[index];
  if (!item) return;
  item.classList.remove("active", "done", "failed");
  if (state) item.classList.add(state);
  if (detail) item.querySelector("small").textContent = detail;
};

const failCurrentStep = (message) => {
  const current = document.querySelector(".steps li.active");
  if (current) {
    current.classList.remove("active");
    current.classList.add("failed");
    current.querySelector("small").textContent = message;
  }
};

const setFile = (file) => {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".osr")) {
    showError("That is not a .osr replay file.");
    return;
  }
  if (file.size > 4_000_000) {
    showError("Replay exceeds the 4 MB upload limit.");
    return;
  }

  selectedFile = file;
  $("#dropTitle").textContent = "Replay selected";
  $("#dropSubtitle").textContent = "Ready to parse, render, and score";
  $("#fileName").textContent = file.name;
  $("#fileSize").textContent = formatBytes(file.size);
  $("#fileChip").hidden = false;
  runButton.disabled = false;
  results.hidden = true;
  hideError();
  resetSteps();
};

const cacheReplay = async (file, replayHash) => {
  const form = new FormData();
  form.append("replay", file, file.name);
  form.append("replay_hash", replayHash);

  const response = await fetch("/api/replay/cache", {
    method: "POST",
    body: form,
  });
  if (!response.ok) throw await apiError(response);
  return response.json();
};

const createRender = async (file, replayHash, username) => {
  const form = new FormData();
  form.append("replay", file, file.name);
  form.append("replay_hash", replayHash);
  form.append("username", username);

  const response = await fetch("/api/ordr/render", {
    method: "POST",
    body: form,
  });
  if (!response.ok) throw await apiError(response);
  return response.json();
};

const waitForRender = async (renderID, runID) => {
  const startedAt = Date.now();
  const timeoutMs = 15 * 60 * 1000;

  while (Date.now() - startedAt < timeoutMs) {
    if (runID !== activeRun) throw new Error("Analysis cancelled.");

    const response = await fetch(`/api/ordr/status?renderID=${encodeURIComponent(renderID)}`, {
      cache: "no-store",
    });
    if (!response.ok) throw await apiError(response);
    const payload = await response.json();

    const progress = payload.progress || "Queued";
    renderStatus.hidden = false;
    renderStatus.textContent = `o!rdr #${renderID} · ${progress}`;
    setStep(2, "active", progress);

    if (payload.failed) {
      throw new Error(`o!rdr render failed (error ${payload.errorCode}).`);
    }

    // Be defensive against stale deployments or transient o!rdr responses:
    // never advance to /api/predict until the URL is genuinely usable.
    if (payload.ready && isValidHttpsVideoURL(payload.videoURL)) {
      return payload;
    }

    await sleep(3000);
  }

  throw new Error("o!rdr did not finish within 15 minutes.");
};

const runPrediction = async ({ replayHash, cacheToken, renderID, description, renderMetadata, videoURL }) => {
  const response = await fetch("/api/predict", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      replayHash,
      cacheToken,
      renderID,
      description,
      renderMetadata,
      videoURL,
    }),
  });
  if (!response.ok) throw await apiError(response);
  return response.json();
};

const renderResult = (data) => {
  $("#resultPlayer").textContent = data.player || "PLAYER";
  $("#topPercent").textContent = formatTopPercent(data.topPercent);
  $("#rankContext").textContent = data.estimatedRank
    ? `Estimated global rank #${data.estimatedRank.toLocaleString()}`
    : `Roughly 1 in ${data.oneInPlayers.toLocaleString()} ranked players`;
  $("#skillValue").textContent = data.skill.toFixed(3);
  $("#baseValue").textContent = data.baseSkill.toFixed(3);
  $("#correctionValue").textContent = `${data.replayCorrection >= 0 ? "+" : ""}${data.replayCorrection.toFixed(3)}`;
  $("#uncertaintyValue").textContent = data.uncertainty.toFixed(3);
  $("#confidenceLabel").textContent = data.confidence.toUpperCase();

  const confidence = Math.max(10, Math.min(100, 100 * (1 - data.uncertainty / 0.35)));
  $("#confidenceBar").style.width = `${confidence}%`;

  $("#mapArtist").textContent = data.beatmap.artist || "Beatmap";
  $("#mapTitle").textContent = data.beatmap.title || "Unknown title";
  $("#mapVersion").textContent = `${data.beatmap.version || ""} · ${data.beatmap.star.toFixed(2)}★ · ${Math.round(data.beatmap.lengthSeconds)}s`;

  $("#accuracyValue").textContent = `${data.accuracyPercent.toFixed(2)}%`;
  $("#modsValue").textContent = data.mods.join("");
  $("#eventsValue").textContent = data.eventCount.toLocaleString();
  $("#renderIdValue").textContent = data.renderID ? `#${data.renderID}` : "—";

  const video = $("#replayVideo");
  video.src = data.videoURL;
  video.load();
  const videoLink = $("#videoLink");
  videoLink.href = data.videoURL;

  const description = $("#renderDescription");
  description.textContent = data.renderDescription || "";
  description.hidden = !data.renderDescription;

  results.hidden = false;
  results.scrollIntoView({ behavior: "smooth", block: "start" });
};

const analyze = async () => {
  if (!selectedFile) return;

  const runID = ++activeRun;
  hideError();
  resetSteps();
  results.hidden = true;
  runButton.disabled = true;
  runButton.querySelector("span:first-child").textContent = "Building replay pipeline…";

  try {
    setStep(0, "active", "Computing SHA-256 and decoding .osr");
    const replayHash = await sha256File(selectedFile);
    const cached = await cacheReplay(selectedFile, replayHash);
    if (runID !== activeRun) return;
    setStep(0, "done", `${cached.eventCount.toLocaleString()} events · ${cached.player}`);

    setStep(1, "active", `Submitting replay as ${cached.player}`);
    const render = await createRender(selectedFile, replayHash, cached.player);
    if (runID !== activeRun) return;
    setStep(1, "done", `Render #${render.renderID} accepted`);

    setStep(2, "active", "Waiting in o!rdr queue");
    const rendered = await waitForRender(render.renderID, runID);
    if (runID !== activeRun) return;
    setStep(2, "done", "Description and render are ready");

    setStep(3, "active", "Reading structured o!rdr render metadata");
    if (!isValidHttpsVideoURL(rendered.videoURL)) {
      throw new Error("o!rdr has not produced a usable HTTPS video URL yet.");
    }
    setStep(3, "done", "Recovered star, length, map, and render metadata");

    setStep(4, "active", "Running five-fold ONNX ensemble");
    const prediction = await runPrediction({
      replayHash,
      cacheToken: cached.cacheToken,
      renderID: render.renderID,
      description: rendered.description || rendered.title || "",
      renderMetadata: rendered.renderMetadata || {},
      videoURL: rendered.videoURL,
    });
    if (runID !== activeRun) return;
    setStep(4, "done", "Prediction complete");

    renderStatus.textContent = `o!rdr #${render.renderID} · complete`;
    renderResult(prediction);
  } catch (error) {
    failCurrentStep(error instanceof Error ? error.message : "Pipeline failed.");
    showError(error instanceof Error ? error.message : "Pipeline failed.");
  } finally {
    if (runID === activeRun) {
      runButton.disabled = false;
      runButton.querySelector("span:first-child").textContent = "Analyze replay";
    }
  }
};

replayInput.addEventListener("change", () => setFile(replayInput.files?.[0]));

["dragenter", "dragover"].forEach((name) => {
  dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.add("dragging");
  });
});

["dragleave", "drop"].forEach((name) => {
  dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.remove("dragging");
  });
});

dropzone.addEventListener("drop", (event) => setFile(event.dataTransfer?.files?.[0]));
runButton.addEventListener("click", analyze);

$("#resetButton").addEventListener("click", () => {
  activeRun += 1;
  selectedFile = null;
  replayInput.value = "";
  results.hidden = true;
  $("#fileChip").hidden = true;
  $("#dropTitle").textContent = "Drop a .osr replay";
  $("#dropSubtitle").textContent = "or click to choose a file";
  $("#replayVideo").removeAttribute("src");
  runButton.disabled = true;
  hideError();
  resetSteps();
  window.scrollTo({ top: 0, behavior: "smooth" });
});
