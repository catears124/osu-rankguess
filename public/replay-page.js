(() => {
  const dataNode = document.querySelector("#replayData");
  if (!dataNode) return;
  const data = JSON.parse(dataNode.textContent);
  const video = document.querySelector("#replayVideo");
  const gate = document.querySelector("#soundGate");
  const formatRank = (value) => Number(value) > 0 ? `#${Math.round(Number(value)).toLocaleString()}` : "-";

  const trySound = async () => {
    video.muted = false;
    video.volume = 1;
    try {
      await video.play();
      gate.hidden = true;
    } catch {
      gate.hidden = false;
    }
  };

  gate.addEventListener("click", trySound);
  window.addEventListener("load", trySound, { once: true });

  document.querySelector("#reveal").addEventListener("click", () => {
    const actual = Number(data.actualRank);
    const predicted = Number(data.predictedRank);
    const ratio = actual > 0 && predicted > 0
      ? Math.max(actual, predicted) / Math.max(1, Math.min(actual, predicted))
      : NaN;
    const map = data.beatmap || {};
    document.querySelector("#player").textContent = data.player || "Unknown player";
    document.querySelector("#map").textContent = `${map.artist ? `${map.artist} - ` : ""}${map.title || "Unknown map"}${map.version ? ` [${map.version}]` : ""}`;
    document.querySelector("#actual").textContent = formatRank(actual);
    document.querySelector("#model").textContent = formatRank(predicted);
    document.querySelector("#ratio").textContent = Number.isFinite(ratio) ? `${ratio.toFixed(2)}x` : "-";
    document.querySelector("#hiddenState").hidden = true;
    document.querySelector("#resultState").classList.add("visible");
  });

  document.querySelector("#copyLink").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    try {
      await navigator.clipboard.writeText(data.canonicalURL);
    } catch {
      const input = document.createElement("textarea");
      input.value = data.canonicalURL;
      input.style.position = "fixed";
      input.style.opacity = "0";
      document.body.appendChild(input);
      input.select();
      document.execCommand("copy");
      input.remove();
    }
    button.textContent = "copied";
    setTimeout(() => {
      if (button.isConnected) button.textContent = "copy link";
    }, 1400);
  });
})();
