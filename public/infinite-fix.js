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
