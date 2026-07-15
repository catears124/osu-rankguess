/* Clean routes, route-specific metadata, and optional osu! sign-in. */
(() => {
  const routeForView = {
    daily: "/daily",
    infinite: "/infinite",
    analyze: "/submit",
    gallery: "/gallery",
  };
  const viewForRoute = Object.fromEntries(Object.entries(routeForView).map(([view, route]) => [route, view]));
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

  const originalShowView = showView;
  showView = function cleanRouteShowView(name) {
    const view = routeForView[name] ? name : "daily";
    originalShowView(view);
    history.replaceState({ view }, "", routeForView[view]);
    updateMetadata(view);
  };

  document.querySelectorAll("[data-view-link]").forEach((link) => {
    const route = routeForView[link.dataset.viewLink];
    if (route) link.setAttribute("href", route);
  });

  const initialView = viewForRoute[location.pathname]
    || (location.hash ? ({ submit: "analyze" }[location.hash.slice(1)] || location.hash.slice(1)) : null)
    || "daily";
  showView(initialView);

  window.addEventListener("popstate", () => {
    const view = viewForRoute[location.pathname] || "daily";
    originalShowView(view);
    history.replaceState({ view }, "", routeForView[view]);
    updateMetadata(view);
  });

  const authLink = document.querySelector("#osuAuthLink");
  if (!authLink) return;

  fetch("/api/auth/status", { cache: "no-store" })
    .then((response) => response.ok ? response.json() : null)
    .then((status) => {
      if (!status?.configured) return;
      authLink.hidden = false;
      if (status.authenticated && status.user?.username) {
        authLink.textContent = status.user.username;
        authLink.href = "/api/auth/logout";
        authLink.title = "Sign out of osu!";
      } else {
        authLink.textContent = "sign in with osu!";
        authLink.href = "/api/auth/osu";
      }
    })
    .catch(() => {});
})();
