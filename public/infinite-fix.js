/* Reliable gallery-backed Infinite without hidden video preloading. */
(() => {
  let nextItem = null;
  let nextPromise = null;
  let nextError = null;
  let recent = [];
  let loadSequence = 0;

  const freshURL = (path) => `${path}${path.includes("?") ? "&" : "?"}_=${Date.now()}`;

  async function timedJSON(path, timeoutMilliseconds = 12_000) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMilliseconds);
    try {
      return await requestJSON(freshURL(path), { signal: controller.signal });
    } catch (error) {
      if (error?.name === "AbortError") throw new Error("The gallery request timed out. Try again.");
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }

  async function galleryBatch() {
    const first = await timedJSON("/api/gallery?limit=60&offset=0");
    if (!first.configured || !first.items?.length) {
      throw new Error("The gallery has no playable replays yet.");
    }
    if (Number(first.total) <= 60) return first.items;

    const maximumOffset = Math.max(0, Number(first.total) - 60);
    const offset = Math.floor(Math.random() * (maximumOffset + 1));
    const page = await timedJSON(`/api/gallery?limit=60&offset=${offset}`);
    return page.items?.length ? page.items : first.items;
  }

  async function pullReplay() {
    const items = (await galleryBatch()).filter(
      (item) => item?.id && item?.videoURL && Number(item.actualRank) > 0,
    );
    if (!items.length) throw new Error("The gallery has no challenge-ready replays.");

    let candidates = items.filter((item) => !recent.includes(item.id));
    if (!candidates.length) candidates = items;
    return candidates[Math.floor(Math.random() * candidates.length)];
  }

  function prefetchStatus(state, text) {
    document.querySelectorAll(".infinite-prefetch-status").forEach((node) => {
      node.dataset.state = state;
      node.textContent = text;
    });
  }

  function prefetch() {
    if (nextItem) return Promise.resolve(nextItem);
    if (nextPromise) return nextPromise;

    nextError = null;
    prefetchStatus("loading", "selecting next replay");
    nextPromise = pullReplay()
      .then((item) => {
        nextItem = item;
        prefetchStatus("ready", "next replay ready");
        return item;
      })
      .catch((error) => {
        nextError = error;
        nextItem = null;
        prefetchStatus("error", "next replay unavailable");
        return null;
      })
      .finally(() => {
        nextPromise = null;
      });
    return nextPromise;
  }

  async function reliableInfinite() {
    const sequence = ++loadSequence;
    window.rankguessUI?.pauseAllVideos?.();
    const root = document.querySelector("#infiniteRoot");
    if (!root) return;

    let item = nextItem;
    nextItem = null;

    if (!item) {
      root.innerHTML = '<section class="generation-card"><div class="busy-line"><i></i><span>pulling a replay from the gallery</span></div><p>Selecting a playable replay.</p></section>';
      await (nextPromise || prefetch());
      if (sequence !== loadSequence) return;
      item = nextItem;
      nextItem = null;
    }

    if (!item) {
      root.innerHTML = `<section class="mode-intro"><p class="kicker">infinite</p><h1>could not load a replay.</h1><p>${escapeHTML(nextError?.message || "Try again in a moment.")}</p><button class="primary-button narrow" id="retryInfinite" type="button">try again</button></section>`;
      document.querySelector("#retryInfinite")?.addEventListener("click", reliableInfinite);
      return;
    }

    recent = [...recent.filter((id) => id !== item.id), item.id].slice(-12);
    infiniteRound = { item, guesses: [], revealed: false };
    mountChallenge(root, item, infiniteRound, "infinite", "infinite");

    // Prefetch only JSON for the next round. Creating a hidden video here was
    // what caused audio to start before the visible Infinite player mounted.
    prefetch();
  }

  loadInfinite = reliableInfinite;

  const oldStart = document.querySelector("#startInfinite");
  if (oldStart) {
    const button = oldStart.cloneNode(true);
    oldStart.replaceWith(button);
    button.addEventListener("click", reliableInfinite);
  }
})();

/* Final user-facing copy and community-count cleanup. */
(() => {
  const roundByPanel = new WeakMap();

  const genericText = (value) => String(value || "")
    .replace(/o!rdr\s*#(\d+)/gi, "render #$1")
    .replace(/o!rdr/gi, "render service")
    .replace(/legacy-replay-ensemble/gi, "rank model");

  const sanitizeTextNode = (node) => {
    const next = genericText(node.nodeValue);
    if (next !== node.nodeValue) node.nodeValue = next;
  };

  const sanitizeTextWithin = (root) => {
    if (root instanceof Text) {
      sanitizeTextNode(root);
      return;
    }
    if (!(root instanceof Element || root instanceof Document)) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node = walker.nextNode();
    while (node) {
      sanitizeTextNode(node);
      node = walker.nextNode();
    }
  };

  const removeGalleryVideoLinks = (root = document) => {
    const links = root instanceof HTMLAnchorElement
      ? [root]
      : [...(root.querySelectorAll?.("a") || [])];
    links.forEach((link) => {
      if (/^open video(?: in a new tab)?$/i.test(link.textContent.trim())) link.remove();
    });
  };

  const cleanSubmitCopy = () => {
    const renderStep = document.querySelector(".steps li:nth-child(2)");
    if (renderStep) {
      if (renderStep.dataset.defaultDetail !== "Send replay for rendering") {
        renderStep.dataset.defaultDetail = "Send replay for rendering";
      }
      const detail = renderStep.querySelector("small");
      if (detail && detail.textContent !== "Send replay for rendering" && /o!rdr|render service/i.test(detail.textContent)) {
        detail.textContent = "Send replay for rendering";
      }
    }

    const note = document.querySelector("#view-analyze .tiny-note");
    if (note && note.textContent !== "Keep this tab open while the replay renders.") {
      note.textContent = "Keep this tab open while the replay renders.";
    }

    const videoLink = document.querySelector("#videoLink");
    if (videoLink) {
      videoLink.hidden = true;
      videoLink.setAttribute("aria-hidden", "true");
      videoLink.tabIndex = -1;
      if (videoLink.textContent) videoLink.textContent = "";
    }

    const footer = document.querySelector("#modelFooter");
    if (footer && footer.textContent !== "rank estimates are approximate") {
      footer.textContent = "rank estimates are approximate";
    }
  };

  const displayedGuessCount = (round) => (Array.isArray(round?.distribution?.bins)
    ? round.distribution.bins.reduce((sum, item) => sum + Math.max(0, Number(item?.count) || 0), 0)
    : 0);

  const patchCommunityCount = (panel, round) => {
    const section = panel?.querySelector?.("[data-community-distribution]");
    const bins = Array.isArray(round?.distribution?.bins) ? round.distribution.bins : [];
    if (!section || !bins.length) return;

    const total = displayedGuessCount(round);
    const label = section.querySelector(".community-distribution-head small");
    const labelText = `${total.toLocaleString()} ${total === 1 ? "guess" : "guesses"}`;
    if (label && label.textContent !== labelText) label.textContent = labelText;

    section.querySelectorAll(".community-bar").forEach((bar, index) => {
      const item = bins[index];
      if (!item) return;
      const count = Math.max(0, Number(item.count) || 0);
      const title = `${formatRank(item.lower)}–${formatRank(item.upper)} · ${count} ${count === 1 ? "guess" : "guesses"}`;
      if (bar.title !== title) bar.title = title;
    });
  };

  if (typeof updateChallengeRound === "function") {
    const baseUpdateChallengeRound = updateChallengeRound;
    updateChallengeRound = function finalCopyUpdateChallengeRound(round, mode, challengeDate) {
      baseUpdateChallengeRound(round, mode, challengeDate);
      const panel = round?.root?.querySelector?.(".reveal-panel");
      if (panel) {
        roundByPanel.set(panel, round);
        patchCommunityCount(panel, round);
      }
    };
  }

  if (typeof renderPrediction === "function") {
    const baseRenderPrediction = renderPrediction;
    renderPrediction = function genericRenderPrediction(payload) {
      baseRenderPrediction(payload);
      const context = document.querySelector("#rankContext");
      const contextText = `${formatTopPercent(payload.topPercent)} of ranked players`;
      if (context && context.textContent !== contextText) context.textContent = contextText;
      cleanSubmitCopy();
    };
  }

  const observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      if (mutation.type === "characterData") {
        sanitizeTextNode(mutation.target);
        continue;
      }
      mutation.addedNodes.forEach((node) => {
        sanitizeTextWithin(node);
        if (node instanceof Element) removeGalleryVideoLinks(node);
      });

      const target = mutation.target instanceof Element ? mutation.target : mutation.target.parentElement;
      const panel = target?.closest?.(".reveal-panel");
      const round = panel ? roundByPanel.get(panel) : null;
      if (panel && round) patchCommunityCount(panel, round);
    }
    cleanSubmitCopy();
    removeGalleryVideoLinks();
  });

  observer.observe(document.documentElement, {
    childList: true,
    subtree: true,
    characterData: true,
  });

  sanitizeTextWithin(document);
  cleanSubmitCopy();
  removeGalleryVideoLinks();
})();
