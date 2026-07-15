/* Clean routes, route-specific metadata, sharing, and required osu! sign-in for replay uploads. */
(() => {
  const routeForView = {
    daily: "/daily",
    infinite: "/infinite",
    analyze: "/submit",
    gallery: "/gallery",
  };
  const viewForRoute = Object.fromEntries(
    Object.entries(routeForView).map(([view, route]) => [route, view]),
  );
  const metadata = {
    daily: {
      title: "osu!rankguess Daily | Guess the osu! Player Rank",
      description: "Play the daily osu! rank guessing game. Watch three osu!standard replay clips, guess each player's global rank, and compete against the rank prediction model.",
    },
    infinite: {
      title: "Infinite osu! Rank Guessing Game | osu!rankguess",
      description: "Play unlimited osu! guess-the-rank rounds using fresh osu!standard replay clips and compare every guess with an ML rank predictor.",
    },
    analyze: {
      title: "osu! Replay Rank Predictor | Submit an .osr Replay",
      description: "Sign in with osu!, upload an osu!standard .osr replay, and estimate the player's global rank from replay telemetry, beatmap difficulty, score context, and an ONNX ensemble.",
    },
    gallery: {
      title: "osu! Replay Rank Prediction Gallery | osu!rankguess",
      description: "Browse osu!standard replay clips with actual global ranks and machine-learning rank predictions. Compare model errors across maps, mods, and skill levels.",
    },
  };

  const setMeta = (selector, attribute, value) => {
    const node = document.querySelector(selector);
    if (node) node.setAttribute(attribute, value);
  };

  const updateMetadata = (view) => {
    const entry = metadata[view] || metadata.daily;
    const path = routeForView[view] || routeForView.daily;
    const canonical = `${location.origin}${path}`;
    document.title = entry.title;
    setMeta('meta[name="description"]', "content", entry.description);
    setMeta('meta[property="og:title"]', "content", entry.title);
    setMeta('meta[property="og:description"]', "content", entry.description);
    setMeta('meta[property="og:url"]', "content", canonical);
    setMeta('meta[name="twitter:title"]', "content", entry.title);
    setMeta('meta[name="twitter:description"]', "content", entry.description);
    setMeta('link[rel="canonical"]', "href", canonical);
  };

  const pauseAllMedia = () => {
    document.querySelectorAll("video, audio").forEach((media) => {
      try { media.pause(); } catch {}
    });
  };

  const resetStaleDaily = (view) => {
    if (view !== "daily" || typeof dailyPayload === "undefined" || !dailyPayload?.date) return;
    const todayUTC = new Date().toISOString().slice(0, 10);
    if (dailyPayload.date === todayUTC) return;
    dailyPayload = null;
    if (typeof dailyState !== "undefined") dailyState = null;
  };

  const viewFromHash = () => {
    const value = location.hash.replace(/^#/, "");
    return value === "submit" ? "analyze" : value;
  };

  const currentView = () => viewForRoute[location.pathname]
    || (routeForView[viewFromHash()] ? viewFromHash() : null)
    || "daily";

  const cleanLocation = (view, mode = "replace") => {
    const route = routeForView[view] || routeForView.daily;
    if (location.pathname === route && !location.hash) return;
    history[mode === "push" ? "pushState" : "replaceState"]({ view }, "", route);
  };

  const originalShowView = showView;
  const renderView = (name, mode = "replace") => {
    const view = routeForView[name] ? name : "daily";
    pauseAllMedia();
    resetStaleDaily(view);
    originalShowView(view);
    cleanLocation(view, mode);
    updateMetadata(view);
  };

  showView = function cleanRouteShowView(name) {
    renderView(name, "replace");
  };

  document.querySelectorAll("[data-view-link]").forEach((link) => {
    const route = routeForView[link.dataset.viewLink];
    if (route) link.setAttribute("href", route);
  });

  document.addEventListener("click", (event) => {
    const link = event.target instanceof Element
      ? event.target.closest("[data-view-link]")
      : null;
    if (!link) return;
    const view = link.dataset.viewLink;
    if (!routeForView[view]) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    renderView(view, location.pathname === routeForView[view] ? "replace" : "push");
    window.scrollTo({ top: 0, behavior: "auto" });
  }, true);

  const initialView = currentView();
  cleanLocation(initialView, "replace");
  updateMetadata(initialView);
  if (document.body.dataset.view !== initialView) originalShowView(initialView);

  window.addEventListener("popstate", () => {
    const view = currentView();
    renderView(view, "replace");
  });

  window.addEventListener("pageshow", () => {
    const view = currentView();
    cleanLocation(view, "replace");
    updateMetadata(view);
  });

  window.addEventListener("click", async (event) => {
    const button = event.target instanceof Element ? event.target.closest("#shareDaily") : null;
    if (!button) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const grid = document.querySelector(".share-grid")?.innerText?.trim() || "";
    const date = document.querySelector(".daily-summary .kicker")?.textContent?.trim() || "";
    const text = `osu!rankguess ${date}\n${grid}\n${location.origin}/daily`;
    try {
      await copyText(text);
      button.textContent = "copied";
    } catch {
      button.textContent = "copy failed";
    }
  }, true);

  const authLink = document.querySelector("#osuAuthLink");
  if (!authLink) return;

  const replayInput = document.querySelector("#replayInput");
  const dropzone = document.querySelector("#dropzone");
  const runButton = document.querySelector("#runButton");
  const dropTitle = document.querySelector("#dropTitle");
  const dropSubtitle = document.querySelector("#dropSubtitle");
  const fileChip = document.querySelector("#fileChip");

  const applySubmitAuthentication = (status) => {
    const authenticated = Boolean(status?.authenticated);
    document.body.dataset.osuAuthenticated = authenticated ? "true" : "false";

    if (replayInput) replayInput.disabled = !authenticated;
    if (dropzone) {
      dropzone.setAttribute("aria-disabled", String(!authenticated));
      dropzone.classList.toggle("auth-required", !authenticated);
    }

    if (!authenticated) {
      if (runButton) runButton.disabled = true;
      if (dropTitle) dropTitle.textContent = status?.configured === false
        ? "osu! sign-in is unavailable"
        : "sign in with osu! to submit";
      if (dropSubtitle) dropSubtitle.textContent = "login is required before .osr parsing";
      return;
    }

    if (fileChip?.hidden !== false) {
      if (dropTitle) dropTitle.textContent = "choose .osr file";
      if (dropSubtitle) dropSubtitle.textContent = "tap here or drop it";
    }
  };

  authLink.hidden = false;
  authLink.textContent = "sign in with osu!";
  authLink.href = "/api/auth/osu";
  applySubmitAuthentication(null);

  fetch("/api/auth/status", { cache: "no-store" })
    .then((response) => response.ok ? response.json() : null)
    .then((status) => {
      applySubmitAuthentication(status);
      if (status?.authenticated && status.user?.username) {
        authLink.textContent = status.user.username;
        authLink.href = "/api/auth/logout";
        authLink.title = "Sign out of osu!";
      }
    })
    .catch(() => applySubmitAuthentication(null));
})();
