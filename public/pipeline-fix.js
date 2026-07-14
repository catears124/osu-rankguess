/* Follow o!rdr through its documented Socket.IO events instead of hammering GET. */
(() => {
  const baseCreateRender = createRender;
  const ORDR_SOCKET_URL = "https://apis.issou.best";
  const ORDR_SOCKET_PATH = "/ordr/ws";
  const STATUS_FALLBACK_MS = 30_000;
  const RATE_LIMIT_BACKOFF_MS = 60_000;
  const TIMEOUT_MS = 30 * 60_000;

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

  const reportedPercent = (progress) => {
    const match = String(progress || "").match(/(\d{1,3}(?:\.\d+)?)\s*%/);
    return match ? Math.max(0, Math.min(100, Number(match[1]))) : null;
  };

  const progressState = (renderID, progress, phase = "progress") => {
    const text = String(progress || "working").trim();
    const folded = text.toLowerCase();
    const explicit = reportedPercent(text);
    let percent = 48;

    if (phase === "done") percent = 60;
    else if (folded.includes("queue") || folded.includes("waiting")) percent = 44;
    else if (explicit !== null) percent = 49 + explicit * 0.1;
    else if (folded.includes("upload") || folded.includes("final")) percent = 58;
    else if (folded.includes("render") || folded.includes("encode") || folded.includes("process")) percent = 53;

    return {
      percent,
      detail: `o!rdr #${renderID} · ${text}`,
    };
  };

  const isRateLimit = (error) => /too many requests|http\s*429|rate limit/i.test(String(error?.message || error || ""));

  createRender = async function documentedCreateRender(file, replayHash, username) {
    try {
      return await baseCreateRender(file, replayHash, username);
    } catch (error) {
      if (isRateLimit(error)) {
        throw new Error("o!rdr rate limit reached. Configure ORDR_API_KEY; unauthenticated clients are limited to one render request every five minutes.");
      }
      throw error;
    }
  };

  waitForRender = function websocketWaitForRender(renderID, runID) {
    return new Promise((resolve, reject) => {
      const numericRenderID = Number(renderID);
      const startedAt = Date.now();
      let socket = null;
      let settled = false;
      let checking = false;
      let fallbackTimer = null;
      let timeoutTimer = null;
      let lastProgress = "Queued";
      let consecutiveStatusFailures = 0;

      const matches = (data) => Number(data?.renderID) === numericRenderID;

      const cleanup = () => {
        if (fallbackTimer) clearTimeout(fallbackTimer);
        if (timeoutTimer) clearTimeout(timeoutTimer);
        if (socket) {
          socket.off("render_added_json");
          socket.off("render_progress_json");
          socket.off("render_done_json");
          socket.off("render_failed_json");
          socket.disconnect();
        }
      };

      const finish = (callback, value) => {
        if (settled) return;
        settled = true;
        cleanup();
        callback(value);
      };

      const scheduleFallback = (delay = STATUS_FALLBACK_MS) => {
        if (settled) return;
        if (fallbackTimer) clearTimeout(fallbackTimer);
        fallbackTimer = setTimeout(() => checkStatus("fallback"), delay);
      };

      const checkStatus = async (reason = "initial") => {
        if (settled || checking) return;
        if (runID !== activeRun) {
          finish(reject, new Error("Analysis cancelled."));
          return;
        }
        checking = true;
        let nextDelay = reason === "done" ? 4_000 : STATUS_FALLBACK_MS;
        try {
          const payload = await requestJSON(`/api/ordr/status?renderID=${encodeURIComponent(numericRenderID)}`);
          consecutiveStatusFailures = 0;
          lastProgress = payload.progress || lastProgress;
          const state = progressState(numericRenderID, lastProgress, payload.ready ? "done" : "progress");
          setPipelineProgress(state.percent, state.detail);
          if (payload.failed) {
            finish(reject, new Error(`o!rdr failed with code ${payload.errorCode}`));
            return;
          }
          if (payload.ready) {
            finish(resolve, payload);
            return;
          }
        } catch (error) {
          consecutiveStatusFailures += 1;
          const rateLimited = isRateLimit(error);
          if (!rateLimited && consecutiveStatusFailures >= 6 && !socket?.connected) {
            finish(reject, error);
            return;
          }
          nextDelay = rateLimited ? RATE_LIMIT_BACKOFF_MS : STATUS_FALLBACK_MS;
          const retryText = rateLimited
            ? "status API rate-limited · websocket still connected"
            : "status check interrupted · websocket still connected";
          setPipelineProgress(47, `o!rdr #${numericRenderID} · ${retryText}`);
        } finally {
          checking = false;
        }
        scheduleFallback(nextDelay);
      };

      const onProgress = (data) => {
        if (!matches(data) || settled) return;
        lastProgress = data.progress || lastProgress;
        const state = progressState(numericRenderID, lastProgress, "progress");
        setPipelineProgress(state.percent, state.detail);
      };

      const onDone = (data) => {
        if (!matches(data) || settled) return;
        lastProgress = "render complete · finalizing metadata";
        const state = progressState(numericRenderID, lastProgress, "done");
        setPipelineProgress(state.percent, state.detail);
        if (checking) scheduleFallback(4_000);
        else checkStatus("done");
      };

      const onFailed = (data) => {
        if (!matches(data) || settled) return;
        const message = data.errorMessage || `o!rdr failed with code ${data.errorCode}`;
        finish(reject, new Error(message));
      };

      if (typeof globalThis.io === "function") {
        socket = globalThis.io(ORDR_SOCKET_URL, {
          path: ORDR_SOCKET_PATH,
          transports: ["websocket", "polling"],
          reconnection: true,
          reconnectionDelay: 1_000,
          reconnectionDelayMax: 10_000,
          timeout: 20_000,
        });
        socket.on("render_added_json", (data) => {
          if (!matches(data) || settled) return;
          lastProgress = "added to queue";
          const state = progressState(numericRenderID, lastProgress, "progress");
          setPipelineProgress(state.percent, state.detail);
        });
        socket.on("render_progress_json", onProgress);
        socket.on("render_done_json", onDone);
        socket.on("render_failed_json", onFailed);
        socket.on("connect_error", () => {
          setPipelineProgress(47, `o!rdr #${numericRenderID} · websocket reconnecting`);
          scheduleFallback(10_000);
        });
      } else {
        setPipelineProgress(47, `o!rdr #${numericRenderID} · websocket unavailable · using slow fallback`);
      }

      timeoutTimer = setTimeout(() => {
        finish(reject, new Error(`o!rdr did not finish within thirty minutes (last state: ${lastProgress}).`));
      }, Math.max(1_000, TIMEOUT_MS - (Date.now() - startedAt)));

      checkStatus("initial");
    });
  };
})();
