/* Clean routes, route-specific metadata, sharing, and optional osu! sign-in. */
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
      description: "Upload an osu!standard .osr replay and estimate the player's global rank from replay telemetry, beatmap difficulty, score context, and an ONNX ensemble.",
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
    originalShowView(view);
    cleanLocation(view, "replace");
    updateMetadata(view);
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

  authLink.hidden = false;
  authLink.textContent = "sign in with osu!";
  authLink.href = "/api/auth/osu";

  fetch("/api/auth/status", { cache: "no-store" })
    .then((response) => response.ok ? response.json() : null)
    .then((status) => {
      if (status?.authenticated && status.user?.username) {
        authLink.textContent = status.user.username;
        authLink.href = "/api/auth/logout";
        authLink.title = "Sign out of osu!";
      }
    })
    .catch(() => {});
})();
