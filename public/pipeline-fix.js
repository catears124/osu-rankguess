/* Keep duplicate o!rdr jobs attached and make queue/render progress visible. */
(() => {
  const baseCreateRender = createRender;
  const duplicatePattern = /already (?:rendering|rendered|in queue)|rendering or in queue/i;

  const setPipelineProgress = (percent, detail) => {
    const bounded = Math.max(0, Math.min(100, Math.round(percent)));
    const bar = document.querySelector("#analysisProgressBar");
    const percentNode = document.querySelector("#analysisPercent");
    const detailNode = document.querySelector("#analysisProgressDetail");
    if (bar) bar.style.width = `${bounded}%`;
    if (percentNode) percentNode.textContent = `${bounded}%`;
    if (detailNode) detailNode.textContent = detail;
    renderStatus.hidden = false;
    renderStatus.textContent = detail;
  };

  const queueData = (progress) => {
    const text = String(progress || "");
    const match = text.match(/#\s*(\d+)(?:\s*(?:of|\/)\s*(\d+))?/i);
    return {
      position: match ? Number(match[1]) : null,
      total: match?.[2] ? Number(match[2]) : null,
    };
  };

  const renderPercent = (progress) => {
    const match = String(progress || "").match(/(\d{1,3}(?:\.\d+)?)\s*%/);
    return match ? Math.max(0, Math.min(100, Number(match[1]))) : null;
  };

  const progressState = (renderID, payload) => {
    const progress = String(payload?.progress || "working").trim();
    const folded = progress.toLowerCase();
    const queue = queueData(progress);
    const reportedPercent = renderPercent(progress);

    if (payload?.ready) {
      return { percent: 60, detail: `o!rdr #${renderID} · video ready` };
    }
    if (folded.includes("finalizing") || folded.includes("waiting for client")) {
      return { percent: 59, detail: `o!rdr #${renderID} · ${progress}` };
    }
    if (folded.includes("queue")) {
      let percent = 43;
      if (queue.position && queue.total) {
        const completion = 1 - Math.min(1, Math.max(0, (queue.position - 1) / Math.max(1, queue.total)));
        percent = 43 + completion * 6;
      } else if (queue.position) {
        percent = 43 + Math.max(0, 6 - Math.min(6, queue.position - 1));
      }
      return { percent, detail: `o!rdr #${renderID} · ${progress}` };
    }
    if (reportedPercent !== null) {
      return { percent: 50 + reportedPercent * 0.09, detail: `o!rdr #${renderID} · ${progress}` };
    }
    if (folded.includes("render") || folded.includes("process") || folded.includes("encode")) {
      return { percent: 53, detail: `o!rdr #${renderID} · ${progress}` };
    }
    return { percent: 47, detail: `o!rdr #${renderID} · ${progress}` };
  };

  createRender = async function persistentCreateRender(file, replayHash, username) {
    let lastError = null;
    for (let attempt = 0; attempt < 4; attempt += 1) {
      try {
        return await baseCreateRender(file, replayHash, username);
      } catch (error) {
        lastError = error;
        if (!duplicatePattern.test(String(error?.message || error)) || attempt >= 3) throw error;
        setPipelineProgress(39, `existing o!rdr job found · reconnecting${attempt ? ` (${attempt + 1})` : ""}`);
        await sleep(3500);
      }
    }
    throw lastError || new Error("Could not reconnect to the existing o!rdr job.");
  };

  waitForRender = async function persistentWaitForRender(renderID, runID) {
    let transientFailures = 0;
    let lastProgress = "Queued";

    for (let attempt = 0; attempt < 720; attempt += 1) {
      if (runID !== activeRun) throw new Error("Analysis cancelled.");
      try {
        const payload = await requestJSON(`/api/ordr/status?renderID=${encodeURIComponent(renderID)}`);
        transientFailures = 0;
        lastProgress = payload.progress || lastProgress;
        const state = progressState(renderID, payload);
        setPipelineProgress(state.percent, state.detail);
        if (payload.failed) throw new Error(`o!rdr failed with code ${payload.errorCode}`);
        if (payload.ready) return payload;
      } catch (error) {
        if (String(error?.message || "").includes("failed with code")) throw error;
        transientFailures += 1;
        if (transientFailures >= 12) throw error;
        setPipelineProgress(47, `o!rdr #${renderID} · connection interrupted · retrying`);
      }
      await sleep(2500);
    }

    throw new Error(`o!rdr did not finish within thirty minutes (last state: ${lastProgress}).`);
  };
})();
