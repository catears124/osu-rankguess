/* Submit and track o!rdr entirely from the browser, matching client flow.txt. */
(() => {
  const ORDR_API_ROOT = "https://apis.issou.best";
  const ORDR_RENDER_URL = `${ORDR_API_ROOT}/ordr/renders`;
  const ORDR_DYNLINK_URL = `${ORDR_API_ROOT}/dynlink/ordr/gen`;
  const ORDR_SOCKET_PATH = "/ordr/ws";
  const STATUS_FALLBACK_MS = 30_000;
  const RATE_LIMIT_BACKOFF_MS = 60_000;
  const TIMEOUT_MS = 30 * 60_000;

  const renderStorageKey = (replayHash) => `osu-rankguess-ordr-v2:${String(replayHash || "").toLowerCase()}`;

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

  const responsePayload = async (response) => {
    const payload = await response.json().catch(() => ({}));
    if (response.ok) return payload;
    const message = payload?.message || payload?.detail?.message || `o!rdr request failed (${response.status})`;
    const error = new Error(message);
    error.status = response.status;
    error.errorCode = Number(payload?.errorCode ?? payload?.detail?.errorCode);
    error.payload = payload;
    throw error;
  };

  const directJSON = async (url, options = {}) => {
    const response = await fetch(url, {
      cache: "no-store",
      credentials: "omit",
      mode: "cors",
      ...options,
    });
    return responsePayload(response);
  };

  const isRateLimit = (error) => Number(error?.status) === 429
    || /too many requests|http\s*429|rate limit/i.test(String(error?.message || error || ""));

  const savedRenderID = (replayHash) => {
    const value = Number(storage.get(renderStorageKey(replayHash)) || 0);
    return Number.isInteger(value) && value > 0 ? value : null;
  };

  const saveRenderID = (replayHash, renderID) => {
    const numeric = Number(renderID);
    if (Number.isInteger(numeric) && numeric > 0) storage.set(renderStorageKey(replayHash), String(numeric));
  };

  const safeUploadName = (username, replayHash) => {
    const player = String(username || "player")
      .normalize("NFKD")
      .replace(/[^a-zA-Z0-9._-]+/g, "_")
      .replace(/^_+|_+$/g, "")
      .slice(0, 32) || "player";
    return `${player}-${String(replayHash || "").slice(0, 12)}.osr`;
  };

  const renderMetadata = (render) => ({
    description: render?.description || null,
    title: render?.title || null,
    username: render?.username,
    replayUsername: render?.replayUsername,
    replayMods: render?.replayMods,
    mapTitle: render?.mapTitle,
    mapLength: render?.mapLength,
    drainTime: render?.drainTime,
    replayDifficulty: render?.replayDifficulty,
    mapID: render?.mapID,
    mapLink: render?.mapLink,
  });

  const directRenderSnapshot = async (renderID, videoHint = null) => {
    const numericRenderID = Number(renderID);
    const payload = await directJSON(`${ORDR_RENDER_URL}?renderID=${encodeURIComponent(numericRenderID)}`);
    const render = Array.isArray(payload?.renders) ? payload.renders[0] : null;
    if (!render) {
      return {
        ready: false,
        failed: false,
        progress: "Queued",
        renderID: numericRenderID,
      };
    }

    const errorCode = Number(render.errorCode || 0);
    const progress = String(render.progress || "Queued");
    if (errorCode) {
      return {
        ready: false,
        failed: true,
        errorCode,
        progress,
        renderID: numericRenderID,
      };
    }

    const done = /done|complete|finished/i.test(progress) || Boolean(videoHint || render.videoUrl || render.videoURL);
    let videoURL = null;
    if (done) {
      try {
        const dynlink = await directJSON(`${ORDR_DYNLINK_URL}?id=${encodeURIComponent(numericRenderID)}`);
        videoURL = typeof dynlink?.url === "string" ? dynlink.url : null;
      } catch (error) {
        if (Number(error?.status) !== 404) throw error;
      }
    }

    return {
      ready: Boolean(videoURL && (render.description || render.title)),
      failed: false,
      progress: done && !videoURL ? "Finalizing video link" : progress,
      renderID: numericRenderID,
      description: render.description || null,
      title: render.title || null,
      videoURL,
      renderMetadata: renderMetadata(render),
    };
  };

  const recoverExistingRender = async (replayHash, username) => {
    const stored = savedRenderID(replayHash);
    if (stored) return stored;

    // Error 29 does not reliably return the existing render ID. One client-side
    // list request is enough to recover the newest active render for this replay's player.
    const payload = await directJSON(`${ORDR_RENDER_URL}?pageSize=100&page=1`);
    const target = String(username || "").trim().toLowerCase();
    const prefix = String(replayHash || "").slice(0, 12).toLowerCase();
    const candidates = (Array.isArray(payload?.renders) ? payload.renders : [])
      .filter((render) => Number(render?.renderID) > 0)
      .map((render) => {
        const serialized = JSON.stringify(render).toLowerCase();
        const replayUsername = String(render?.replayUsername || "").trim().toLowerCase();
        const progress = String(render?.progress || "").toLowerCase();
        let score = 0;
        if (prefix && serialized.includes(prefix)) score += 100;
        if (target && replayUsername === target) score += 40;
        if (target && serialized.includes(target)) score += 20;
        if (!/failed|error/.test(progress) && Number(render?.errorCode || 0) === 0) score += 10;
        if (!/done|complete|finished/.test(progress)) score += 10;
        return { renderID: Number(render.renderID), score };
      })
      .filter((candidate) => candidate.score >= 50)
      .sort((left, right) => right.score - left.score || right.renderID - left.renderID);

    const renderID = candidates[0]?.renderID || null;
    if (renderID) saveRenderID(replayHash, renderID);
    return renderID;
  };

  createRender = async function clientSideCreateRender(file, replayHash, username) {
    const existing = savedRenderID(replayHash);
    if (existing) {
      setPipelineProgress(39, `reusing browser render #${existing}`);
      return { ok: true, renderID: existing, replayHash, player: username, reused: true };
    }

    setPipelineProgress(35, "submitting replay directly from your browser");
    const form = new FormData();
    form.append("replayFile", file, safeUploadName(username, replayHash));
    form.append("skin", "whitecatCK1.0");
    form.append("resolution", "960x540");
    form.append("showPPCounter", "false");
    form.append("showScoreboard", "false");
    form.append("showResultScreen", "true");
    form.append("skip", "true");
    form.append("customSkin", "false");
    form.append("generateThumbnail", "true");

    try {
      const payload = await directJSON(ORDR_RENDER_URL, { method: "POST", body: form });
      const renderID = Number(payload?.renderID);
      if (!(renderID > 0)) throw new Error("o!rdr did not return a render ID.");
      saveRenderID(replayHash, renderID);
      return { ok: true, renderID, replayHash, player: username };
    } catch (error) {
      if (Number(error?.errorCode) === 29) {
        const renderID = await recoverExistingRender(replayHash, username);
        if (renderID) {
          setPipelineProgress(39, `reattached to existing render #${renderID}`);
          return { ok: true, renderID, replayHash, player: username, reused: true };
        }
      }

      if (isRateLimit(error)) {
        const renderID = await recoverExistingRender(replayHash, username).catch(() => null);
        if (renderID) {
          setPipelineProgress(39, `reattached to existing render #${renderID}`);
          return { ok: true, renderID, replayHash, player: username, reused: true };
        }
        throw new Error("o!rdr rate-limited this browser's IP. The upload is no longer going through the shared server; wait for the per-client cooldown and retry.");
      }
      throw error;
    }
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

    return { percent, detail: `o!rdr #${renderID} · ${text}` };
  };

  waitForRender = function clientSideWaitForRender(renderID, runID) {
    return new Promise((resolve, reject) => {
      const numericRenderID = Number(renderID);
      const startedAt = Date.now();
      let socket = null;
      let settled = false;
      let checking = false;
      let fallbackTimer = null;
      let timeoutTimer = null;
      let lastProgress = "Queued";
      let videoHint = null;
      let nextFallback = STATUS_FALLBACK_MS;

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

      const scheduleFallback = (delay = nextFallback) => {
        if (settled) return;
        if (fallbackTimer) clearTimeout(fallbackTimer);
        fallbackTimer = setTimeout(checkSnapshot, delay);
      };

      const checkSnapshot = async () => {
        if (settled || checking) return;
        if (runID !== activeRun) {
          finish(reject, new Error("Analysis cancelled."));
          return;
        }
        checking = true;
        try {
          const snapshot = await directRenderSnapshot(numericRenderID, videoHint);
          nextFallback = STATUS_FALLBACK_MS;
          lastProgress = snapshot.progress || lastProgress;
          const state = progressState(numericRenderID, lastProgress, snapshot.ready ? "done" : "progress");
          setPipelineProgress(state.percent, state.detail);
          if (snapshot.failed) {
            finish(reject, new Error(`o!rdr failed with code ${snapshot.errorCode}`));
            return;
          }
          if (snapshot.ready) {
            finish(resolve, snapshot);
            return;
          }
          if (videoHint) nextFallback = 3_000;
        } catch (error) {
          nextFallback = isRateLimit(error) ? RATE_LIMIT_BACKOFF_MS : STATUS_FALLBACK_MS;
          const detail = isRateLimit(error)
            ? "public status endpoint rate-limited · websocket still tracking"
            : "status check interrupted · websocket still tracking";
          setPipelineProgress(47, `o!rdr #${numericRenderID} · ${detail}`);
        } finally {
          checking = false;
        }
        scheduleFallback();
      };

      const onProgress = (data) => {
        if (!matches(data) || settled) return;
        lastProgress = data.progress || lastProgress;
        const state = progressState(numericRenderID, lastProgress, "progress");
        setPipelineProgress(state.percent, state.detail);
      };

      const onDone = (data) => {
        if (!matches(data) || settled) return;
        videoHint = data?.videoUrl || data?.videoURL || true;
        lastProgress = "render complete · resolving metadata and video";
        const state = progressState(numericRenderID, lastProgress, "done");
        setPipelineProgress(state.percent, state.detail);
        if (checking) scheduleFallback(3_000);
        else checkSnapshot();
      };

      const onFailed = (data) => {
        if (!matches(data) || settled) return;
        const message = data.errorMessage || `o!rdr failed with code ${data.errorCode}`;
        finish(reject, new Error(message));
      };

      if (typeof globalThis.io === "function") {
        socket = globalThis.io(ORDR_API_ROOT, {
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
          scheduleFallback(5_000);
        });
      } else {
        setPipelineProgress(47, `o!rdr #${numericRenderID} · websocket unavailable · using direct status fallback`);
      }

      timeoutTimer = setTimeout(() => {
        finish(reject, new Error(`o!rdr did not finish within thirty minutes (last state: ${lastProgress}).`));
      }, Math.max(1_000, TIMEOUT_MS - (Date.now() - startedAt)));

      checkSnapshot();
    });
  };
})();
