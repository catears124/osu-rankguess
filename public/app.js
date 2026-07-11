const $ = (selector) => document.querySelector(selector);
const replayInput = $("#replayInput");
const dropzone = $("#dropzone");
const runButton = $("#runButton");
const manualPanel = $("#manualPanel");
const starInput = $("#starInput");
const lengthInput = $("#lengthInput");
const errorBox = $("#errorBox");
const results = $("#results");
let selectedFile = null;
let manualRequired = false;
let animationTimer = null;

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

const setFile = (file) => {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".osr")) {
    showError("That is not a .osr replay file.");
    return;
  }
  selectedFile = file;
  $("#dropTitle").textContent = "Replay selected";
  $("#dropSubtitle").textContent = "Ready for whole-replay inference";
  $("#fileName").textContent = file.name;
  $("#fileSize").textContent = formatBytes(file.size);
  $("#fileChip").hidden = false;
  runButton.disabled = false;
  hideError();
};

const showError = (message) => {
  errorBox.textContent = message;
  errorBox.hidden = false;
};
const hideError = () => { errorBox.hidden = true; };

const resetSteps = () => {
  document.querySelectorAll(".steps li").forEach((item) => item.classList.remove("active", "done"));
};

const animateSteps = () => {
  resetSteps();
  const steps = [...document.querySelectorAll(".steps li")];
  let index = 0;
  steps[0].classList.add("active");
  animationTimer = setInterval(() => {
    steps[index].classList.remove("active");
    steps[index].classList.add("done");
    index = Math.min(index + 1, steps.length - 1);
    steps[index].classList.add("active");
    if (index === steps.length - 1) clearInterval(animationTimer);
  }, 650);
};

const finishSteps = () => {
  if (animationTimer) clearInterval(animationTimer);
  document.querySelectorAll(".steps li").forEach((item) => {
    item.classList.remove("active");
    item.classList.add("done");
  });
};

const renderResult = (data) => {
  $("#resultPlayer").textContent = data.player || "UNKNOWN PLAYER";
  $("#topPercent").textContent = formatTopPercent(data.topPercent);
  const rankPart = data.estimatedRank ? `Estimated global rank ≈ #${data.estimatedRank.toLocaleString()} · ` : "";
  $("#rankContext").textContent = `${rankPart}roughly 1 in ${data.oneInPlayers.toLocaleString()} ranked players`;
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
  const cover = $("#mapCover");
  cover.style.backgroundImage = data.beatmap.cover ? `linear-gradient(rgba(8,9,12,.18),rgba(8,9,12,.3)), url(${JSON.stringify(data.beatmap.cover)})` : "";
  const link = $("#mapLink");
  link.hidden = !data.beatmap.url;
  if (data.beatmap.url) link.href = data.beatmap.url;

  $("#accuracyValue").textContent = `${data.accuracyPercent.toFixed(2)}%`;
  $("#modsValue").textContent = data.mods.join("");
  $("#eventsValue").textContent = data.eventCount.toLocaleString();
  results.hidden = false;
  results.scrollIntoView({ behavior: "smooth", block: "start" });
};

const analyze = async () => {
  if (!selectedFile) return;
  hideError();
  runButton.disabled = true;
  runButton.querySelector("span:first-child").textContent = "Analyzing replay…";
  animateSteps();

  const form = new FormData();
  form.append("replay", selectedFile);
  if (manualRequired) {
    form.append("star", starInput.value);
    form.append("length_seconds", lengthInput.value);
  }

  try {
    const response = await fetch("/api/predict", { method: "POST", body: form });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = payload.detail || payload;
      if (detail.code === "metadata_required") {
        manualRequired = true;
        manualPanel.hidden = false;
        showError(`${detail.message}${detail.reason ? ` (${detail.reason})` : ""}`);
        starInput.focus();
        return;
      }
      throw new Error(detail.message || `Request failed (${response.status})`);
    }
    finishSteps();
    renderResult(payload);
  } catch (error) {
    resetSteps();
    showError(error instanceof Error ? error.message : "Inference failed.");
  } finally {
    runButton.disabled = false;
    runButton.querySelector("span:first-child").textContent = manualRequired ? "Analyze with manual metadata" : "Analyze replay";
  }
};

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
  selectedFile = null;
  manualRequired = false;
  replayInput.value = "";
  starInput.value = "";
  lengthInput.value = "";
  manualPanel.hidden = true;
  results.hidden = true;
  $("#fileChip").hidden = true;
  $("#dropTitle").textContent = "Drop a .osr replay";
  $("#dropSubtitle").textContent = "or click to choose a file";
  runButton.disabled = true;
  hideError();
  resetSteps();
  window.scrollTo({ top: 0, behavior: "smooth" });
});
