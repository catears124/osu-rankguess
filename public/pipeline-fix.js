/* Submit the replay from the browser without requiring o!rdr CORS response access. */
(() => {
  const ORDR_API_ROOT = "https://apis.issou.best";
  const ORDR_RENDER_URL = `${ORDR_API_ROOT}/ordr/renders`;
  const ORDR_SOCKET_PATH = "/ordr/ws";
  const STATUS_FALLBACK_MS = 30_000;
  const RATE_LIMIT_BACKOFF_MS = 60_000;
  const IDENTIFY_TIMEOUT_MS = 120_000;
  const RENDER_TIMEOUT_MS = 30 * 60_000;
  const MAX_CANDIDATES = 12;

  const baseCacheReplay = cacheReplay;
  let latestCache = null;
  let activeSession = null;

  const renderStorageKey = (replayHash) => `osu-rankguess-ordr-v3:${String(replayHash || "").toLowerCase()}`;

  cacheReplay = async function cacheReplayWithIdentity(file, replayHash) {
    latestCache = await baseCacheReplay(file, replayHash);
    return latestCache;
  };

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

  const normalizePlayer = (value) => String(value || "").trim().normalize("NFKC").toLowerCase();

  const descriptionIdentity = (value) => {
    const text = String(value || "");
    const playerMatch = text.match(/Player:\s*(.*?)\s*,\s*Map:/i);
    const accuracyMatches = [...text.matchAll(/(\d+(?:\.\d+)?)\s*%/g)];
    const accuracy = accuracyMatches.length ? Number(accuracyMatches.at(-1)[1]) : null;
    return {
      player: playerMatch ? normalizePlayer(playerMatch[1]) : "",
      accuracy: Number.isFinite(accuracy) ? accuracy : null,
    };
  };

  const expectedIdentity = (username) => ({
    player: normalizePlayer(latestCache?.player || username),
    accuracy: Number.isFinite(Number(latestCache?.accuracyPercent))
      ? Number(latestCache.accuracyPercent)
      : null,
  });

  const identityMatches = (expected, payload) => {
    const replayPlayer = normalizePlayer(payload?.replayUsername);
    const parsed = descriptionIdentity(payload?.description || payload?.title);
    const player = replayPlayer || parsed.player;
    if (!expected.player || player !== expected.player) return false;
    if (expected.accuracy === null || parsed.accuracy === null) return true;
    return Math.abs(expected.accuracy - parsed.accuracy) <= 0.025;
  };

  const savedRenderID = (replayHash) => {
    const value = Number(storage.get(renderStorageKey(replayHash)) || 0);
    return Number.isInteger(value) && value > 0 ? value : null;
  };

  const saveRenderID = (replayHash, renderID) => {
    const numeric = Number(renderID);
    if (Number.isInteger(numeric) && numeric > 0) {
      storage.set(renderStorageKey(replayHash), String(numeric));
    }
  };

  const safeUploadName = (username, replayHash) => {
    const player = String(username || "player")
      .normalize("NFKD")
      .replace(/[^a-zA-Z0-9._-]+/g, "_")
      .replace(/^_+|_+$/g, "")
      .slice(0, 32) || "player";
    return `${player}-${String(replayHash || "").slice(0, 12)}.osr`;
  };

  const isRateLimit = (error) => /too many requests|http\s*429|rate limit/i.test(String(error?.message || error || ""));

  const progressState = (renderID, progress, phase = "progress") => {
    const text = String(progress || "working").trim();
    const folded = text.toLowerCase();
    const explicitMatch = text.match(/(\d{1,3}(?:\.\d+)?)\s*%/);
    const explicit = explicitMatch ? Math.max(0, Math.min(100, Number(explicitMatch[1]))) : null;
    let percent = 48;
    if (phase === "done") percent = 60;
    else if (folded.includes("queue") || folded.includes("waiting")) percent = 44;
    else if (explicit !== null) percent = 49 + explicit * 0.1;
    else if (folded.includes("upload") || folded.includes("final")) percent = 58;
    else if (folded.includes("render") || folded.includes("encode") || folded.includes("process")) percent = 53;
    return { percent, detail: `o!rdr #${renderID} · ${text}` };
  };

  const statusSnapshot = (renderID) => requestJSON(`/api/ordr/status?renderID=${encodeURIComponent(renderID)}`);

  const createSession = (expected, replayHash, knownRenderID = null) => {
    activeSession?.close?.();

    const listeners = {
      progress: new Set(),
      done: new Set(),
      failed: new Set(),
    };
    const candidateIDs = new Set();
    const checkedIDs = new Set();
    let renderID = Number(knownRenderID) || null;
    let submitted = false;
    let closed = false;
    let identifyResolve = null;
    let identifyReject = null;
    let identifyTimer = null;
    let socket = null;

    const identifyPromise = new Promise((resolve, reject) => {
      identifyResolve = resolve;
      identifyReject = reject;
    });

    const acceptRender = (value) => {
      const numeric = Number(value);
      if (closed || !(numeric > 0)) return;
      if (renderID && renderID !== numeric) return;
      renderID = numeric;
      saveRenderID(replayHash, numeric);
      if (identifyTimer) clearTimeout(identifyTimer);
      identifyResolve?.(numeric);
      identifyResolve = null;
      identifyReject = null;
    };

    const emit = (type, payload) => {
      for (const listener of listeners[type]) listener(payload);
    };

    const verifyCandidate = async (candidateID) => {
      const numeric = Number(candidateID);
      if (closed || renderID || checkedIDs.has(numeric) || checkedIDs.size >= MAX_CANDIDATES) return;
      checkedIDs.add(numeric);
      try {
        const snapshot = await statusSnapshot(numeric);
        if (identityMatches(expected, snapshot)) {
          acceptRender(numeric);
          emit("progress", snapshot);
        }
      } catch {
        // The websocket description will still identify the render when rendering begins.
      }
    };

    const connected = new Promise((resolve, reject) => {
      if (typeof globalThis.io !== "function") {
        reject(new Error("o!rdr websocket client did not load."));
        return;
      }
      socket = globalThis.io(ORDR_API_ROOT, {
        path: ORDR_SOCKET_PATH,
        transports: ["websocket", "polling"],
        reconnection: true,
        reconnectionDelay: 1_000,
        reconnectionDelayMax: 10_000,
        timeout: 20_000,
      });
      const timer = setTimeout(() => reject(new Error("Could not connect to the o!rdr websocket.")), 20_000);
      socket.once("connect", () => {
        clearTimeout(timer);
        resolve();
      });
      socket.once("connect_error", (error) => {
        if (!socket.connected) {
          clearTimeout(timer);
          reject(new Error(error?.message || "Could not connect to the o!rdr websocket."));
        }
      });

      socket.on("render_added_json", (data) => {
        if (closed || !submitted || renderID) return;
        const candidateID = Number(data?.renderID);
        if (!(candidateID > 0) || candidateIDs.size >= MAX_CANDIDATES) return;
        candidateIDs.add(candidateID);
        setTimeout(() => verifyCandidate(candidateID), 350);
      });

      socket.on("render_progress_json", (data) => {
        if (closed) return;
        const candidateID = Number(data?.renderID);
        if (!renderID && submitted && identityMatches(expected, data)) acceptRender(candidateID);
        if (renderID && candidateID === renderID) emit("progress", data);
      });

      socket.on("render_done_json", (data) => {
        if (closed) return;
        const candidateID = Number(data?.renderID);
        if (renderID && candidateID === renderID) emit("done", data);
      });

      socket.on("render_failed_json", (data) => {
        if (closed) return;
        const candidateID = Number(data?.renderID);
        if (renderID && candidateID === renderID) emit("failed", data);
      });
    });

    if (renderID) acceptRender(renderID);

    const session = {
      get renderID() { return renderID; },
      connected,
      identify() {
        if (renderID) return Promise.resolve(renderID);
        if (!identifyTimer) {
          identifyTimer = setTimeout(() => {
            identifyReject?.(new Error("o!rdr received the browser upload but did not expose its render ID in time."));
            identifyResolve = null;
            identifyReject = null;
          }, IDENTIFY_TIMEOUT_MS);
        }
        return identifyPromise;
      },
      markSubmitted() { submitted = true; },
      on(type, listener) {
        listeners[type].add(listener);
        return () => listeners[type].delete(listener);
      },
      close() {
        if (closed) return;
        closed = true;
        if (identifyTimer) clearTimeout(identifyTimer);
        socket?.disconnect();
        Object.values(listeners).forEach((set) => set.clear());
      },
    };
    activeSession = session;
    return session;
  };

  createRender = async function browserCreateRender(file, replayHash, username) {
    const expected = expectedIdentity(username);
    const existing = savedRenderID(replayHash);
    const session = createSession(expected, replayHash, existing);
    await session.connected;

    if (existing) {
      setPipelineProgress(39, `reusing browser render #${existing}`);
      return { ok: true, renderID: existing, replayHash, player: username, reused: true };
    }

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

    setPipelineProgress(35, "sending replay from your browser");
    session.markSubmitted();

    // o!rdr does not expose CORS response headers for third-party sites. no-cors
    // still sends the multipart request from the user's IP; the documented
    // Socket.IO events and our read-only status endpoint provide the render ID.
    await fetch(ORDR_RENDER_URL, {
      method: "POST",
      mode: "no-cors",
      credentials: "omit",
      body: form,
    });

    setPipelineProgress(38, "upload sent · waiting for o!rdr acknowledgment");
    const renderID = await session.identify();
    setPipelineProgress(40, `o!rdr #${renderID} · added to queue`);
    return { ok: true, renderID, replayHash, player: username };
  };

  waitForRender = function browserWaitForRender(renderID, runID) {
    return new Promise(async (resolve, reject) => {
      const numericRenderID = Number(renderID);
      const session = activeSession?.renderID === numericRenderID
        ? activeSession
        : createSession({ player: "", accuracy: null }, "", numericRenderID);
      let settled = false;
      let checking = false;
      let fallbackTimer = null;
      let timeoutTimer = null;
      let latestDescription = null;
      let latestProgress = "Queued";
      let videoHint = null;
      const unsubscribers = [];

      const cleanup = () => {
        if (fallbackTimer) clearTimeout(fallbackTimer);
        if (timeoutTimer) clearTimeout(timeoutTimer);
        unsubscribers.forEach((unsubscribe) => unsubscribe());
        session.close();
        if (activeSession === session) activeSession = null;
      };

      const finish = (callback, value) => {
        if (settled) return;
        settled = true;
        cleanup();
        callback(value);
      };

      const usableSocketResult = () => {
        const url = typeof videoHint === "string" ? videoHint : null;
        if (!url || !latestDescription) return null;
        return {
          ok: true,
          renderID: numericRenderID,
          ready: true,
          failed: false,
          progress: "Done.",
          description: latestDescription,
          videoURL: url,
          renderMetadata: { description: latestDescription },
        };
      };

      const scheduleFallback = (delay = STATUS_FALLBACK_MS) => {
        if (settled) return;
        if (fallbackTimer) clearTimeout(fallbackTimer);
        fallbackTimer = setTimeout(checkStatus, delay);
      };

      const checkStatus = async (finalCheck = false) => {
        if (settled || checking) return;
        if (runID !== activeRun) {
          finish(reject, new Error("Analysis cancelled."));
          return;
        }
        checking = true;
        let nextDelay = finalCheck ? 3_000 : STATUS_FALLBACK_MS;
        try {
          const snapshot = await statusSnapshot(numericRenderID);
          latestProgress = snapshot.progress || latestProgress;
          latestDescription = snapshot.description || snapshot.title || latestDescription;
          const state = progressState(numericRenderID, latestProgress, snapshot.ready ? "done" : "progress");
          setPipelineProgress(state.percent, state.detail);
          if (snapshot.failed) {
            finish(reject, new Error(`o!rdr failed with code ${snapshot.errorCode}`));
            return;
          }
          if (snapshot.ready) {
            finish(resolve, snapshot);
            return;
          }
          const socketResult = usableSocketResult();
          if (finalCheck && socketResult) {
            finish(resolve, socketResult);
            return;
          }
        } catch (error) {
          nextDelay = isRateLimit(error) ? RATE_LIMIT_BACKOFF_MS : STATUS_FALLBACK_MS;
          const socketResult = usableSocketResult();
          if (finalCheck && socketResult) {
            finish(resolve, socketResult);
            return;
          }
          const detail = isRateLimit(error)
            ? "status endpoint cooling down · websocket still tracking"
            : "status check interrupted · websocket still tracking";
          setPipelineProgress(47, `o!rdr #${numericRenderID} · ${detail}`);
        } finally {
          checking = false;
        }
        scheduleFallback(nextDelay);
      };

      unsubscribers.push(session.on("progress", (data) => {
        latestProgress = data?.progress || latestProgress;
        latestDescription = data?.description || data?.title || latestDescription;
        const state = progressState(numericRenderID, latestProgress, "progress");
        setPipelineProgress(state.percent, state.detail);
      }));

      unsubscribers.push(session.on("done", (data) => {
        videoHint = data?.videoUrl || data?.videoURL || null;
        latestProgress = "render complete · resolving metadata";
        const state = progressState(numericRenderID, latestProgress, "done");
        setPipelineProgress(state.percent, state.detail);
        setTimeout(() => checkStatus(true), 900);
      }));

      unsubscribers.push(session.on("failed", (data) => {
        const message = data?.errorMessage || `o!rdr failed with code ${data?.errorCode}`;
        finish(reject, new Error(message));
      }));

      timeoutTimer = setTimeout(() => {
        finish(reject, new Error(`o!rdr did not finish within thirty minutes (last state: ${latestProgress}).`));
      }, RENDER_TIMEOUT_MS);

      try {
        await session.connected;
      } catch (error) {
        finish(reject, error);
        return;
      }
      checkStatus();
    });
  };
})();
