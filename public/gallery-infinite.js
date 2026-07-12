/* Gallery-backed Infinite, spoiler-first gallery, and video-frame thumbnail fallback. */
(() => {
  const UI = window.rankguessUI;
  let spoilersHidden = true;
  let nextItem = null;
  let nextPromise = null;
  let nextError = null;
  let recent = [];
  let previewObserver = null;

  const ratio = (item) => item.actualRank && item.predictedRank
    ? Math.max(item.actualRank, item.predictedRank) / Math.max(1, Math.min(item.actualRank, item.predictedRank))
    : Infinity;

  async function galleryBatch() {
    const first = await requestJSON("/api/gallery?limit=60&offset=0");
    if (!first.configured || !first.items?.length) throw new Error("The gallery has no playable replays yet.");
    if (Number(first.total) <= 60) return first.items;
    const offset = Math.floor(Math.random() * (Math.max(0, Number(first.total) - 60) + 1));
    const page = await requestJSON(`/api/gallery?limit=60&offset=${offset}`);
    return page.items?.length ? page.items : first.items;
  }

  async function pullReplay() {
    const items = (await galleryBatch()).filter((item) => item?.id && item?.videoURL && Number(item.actualRank) > 0);
    if (!items.length) throw new Error("The gallery has no challenge-ready replays.");
    let candidates = items.filter((item) => !recent.includes(item.id));
    if (!candidates.length) candidates = items;
    return candidates[Math.floor(Math.random() * candidates.length)];
  }

  function prefetchStatus(state, text) {
    document.querySelectorAll(".infinite-prefetch-status").forEach((node) => { node.dataset.state = state; node.textContent = text; });
  }

  function prefetch() {
    if (nextItem || nextPromise) return nextPromise;
    nextError = null;
    prefetchStatus("loading", "loading next replay");
    nextPromise = pullReplay().then((item) => {
      nextItem = item;
      prefetchStatus("ready", "next replay ready");
      return item;
    }).catch((error) => {
      nextError = error;
      prefetchStatus("error", "next replay unavailable");
      return null;
    }).finally(() => { nextPromise = null; });
    return nextPromise;
  }

  loadInfinite = async function galleryInfinite() {
    UI.pauseAllVideos();
    const root = document.querySelector("#infiniteRoot");
    let item = nextItem;
    nextItem = null;
    if (!item) {
      root.innerHTML = '<section class="generation-card"><div class="busy-line"><i></i><span>pulling a replay from the gallery</span></div><p>The next replay loads in the background as soon as this one opens.</p></section>';
      item = await (nextPromise || prefetch());
    }
    if (!item) {
      root.innerHTML = `<section class="mode-intro"><p class="kicker">infinite</p><h1>could not load a replay.</h1><p>${escapeHTML(nextError?.message || "Try again in a moment.")}</p><button class="primary-button narrow" id="retryInfinite" type="button">try again</button></section>`;
      document.querySelector("#retryInfinite")?.addEventListener("click", loadInfinite);
      return;
    }
    recent = [...recent.filter((id) => id !== item.id), item.id].slice(-12);
    infiniteRound = { item, guesses: [], revealed: false };
    mountChallenge(root, item, infiniteRound, "infinite", "infinite");
    prefetch();
  };

  const oldStart = document.querySelector("#startInfinite");
  if (oldStart) {
    const button = oldStart.cloneNode(true);
    oldStart.replaceWith(button);
    button.addEventListener("click", loadInfinite);
  }

  galleryCard = function spoilerCard(item) {
    const map = item.beatmap || {};
    const cover = item.thumbnailURL || `/api/gallery/${encodeURIComponent(item.id)}/thumbnail`;
    const errorRatio = ratio(item);
    const errorLabel = Number.isFinite(errorRatio) ? `${errorRatio.toFixed(2)}× rank ratio` : "rank unavailable";
    const errorWidth = Number.isFinite(errorRatio) ? Math.min(100, Math.max(4, Math.log10(Math.max(1, errorRatio)) / 2 * 100)) : 4;
    return `<article class="gallery-card ${spoilersHidden ? "spoiler" : ""}" data-gallery-id="${escapeHTML(item.id)}" tabindex="0" role="button" aria-label="Open replay">
      <button class="gallery-thumb" type="button" aria-label="Open replay"><video class="gallery-preview" data-src="${escapeHTML(item.videoURL)}" muted playsinline preload="none"></video><img src="${escapeHTML(cover)}" alt="" loading="lazy" decoding="async" /><span>watch</span></button>
      <div class="gallery-copy"><p class="gallery-eyebrow">replay</p><h2>${spoilersHidden ? "mystery player" : escapeHTML(item.player || "Unknown player")}</h2><p>${spoilersHidden ? "open to reveal map and ranks" : escapeHTML(`${map.artist ? `${map.artist} — ` : ""}${map.title || "Unknown map"}`)}</p>${spoilersHidden ? "" : `<small>${escapeHTML(`${map.version || "Unknown difficulty"} · ${Number(item.star || 0).toFixed(2)}★ · ${(item.mods || ["NM"]).join("")}`)}</small>`}</div>
      ${spoilersHidden ? '<div class="spoiler-strip">ranks hidden until opened</div>' : `<div class="gallery-ranks"><div><span>actual</span><strong>${formatRank(item.actualRank)}</strong></div><div><span>model</span><strong>${formatRank(item.predictedRank)}</strong></div></div><div class="gallery-error"><i style="width:${errorWidth}%"></i><span>${escapeHTML(errorLabel)}</span></div>`}
    </article>`;
  };

  const loadPreview = (video) => {
    if (!video.src && video.dataset.src) { video.src = `${video.dataset.src}#t=1`; video.load?.(); }
  };
  function observePreviews() {
    previewObserver?.disconnect();
    const previews = [...document.querySelectorAll(".gallery-preview")];
    if (!("IntersectionObserver" in globalThis)) return previews.forEach(loadPreview);
    previewObserver = new IntersectionObserver((entries) => entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      loadPreview(entry.target);
      previewObserver.unobserve(entry.target);
    }), { rootMargin: "200px" });
    previews.forEach((video) => previewObserver.observe(video));
  }

  openGalleryDialog = function spoilerDialog(item) {
    if (!item) return;
    UI.pauseAllVideos({ unloadDialog: true });
    const map = item.beatmap || {};
    const errorRatio = ratio(item);
    document.querySelector("#galleryDialogBody").innerHTML = `<video src="${escapeHTML(item.videoURL)}" controls autoplay playsinline preload="auto"></video><div class="dialog-copy"><p class="kicker">replay result</p><h1>${escapeHTML(item.player || "Unknown player")}</h1><p>${escapeHTML(`${map.artist ? `${map.artist} — ` : ""}${map.title || "Unknown map"} [${map.version || "?"}]`)}</p><div class="dialog-ranks"><div><span>actual</span><strong>${formatRank(item.actualRank)}</strong></div><div><span>model</span><strong>${formatRank(item.predictedRank)}</strong></div><div><span>ratio</span><strong>${Number.isFinite(errorRatio) ? `${errorRatio.toFixed(2)}×` : "—"}</strong></div></div><a href="${escapeHTML(item.videoURL)}" target="_blank" rel="noreferrer">open video in a new tab</a></div>`;
    const dialog = document.querySelector("#galleryDialog");
    if (dialog.showModal) dialog.showModal(); else dialog.setAttribute("open", "");
    UI.autoplayVideo(dialog.querySelector("video"), true).catch(() => {});
  };

  renderGallery = function spoilerGallery() {
    let items = [...galleryItems];
    const sort = document.querySelector("#gallerySort")?.value || "newest";
    if (sort === "error") items.sort((a, b) => ratio(b) - ratio(a));
    if (sort === "closest") items.sort((a, b) => ratio(a) - ratio(b));
    document.querySelector("#galleryGrid").innerHTML = items.map(galleryCard).join("");
    document.querySelectorAll(".gallery-card").forEach((card) => {
      const item = galleryItems.find((candidate) => candidate.id === card.dataset.galleryId);
      const image = card.querySelector(".gallery-thumb img");
      image?.addEventListener("error", () => image.remove(), { once: true });
      card.querySelector(".gallery-thumb")?.addEventListener("click", () => openGalleryDialog(item));
      card.addEventListener("click", (event) => { if (!event.target.closest("button,a,input,select")) openGalleryDialog(item); });
      card.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openGalleryDialog(item); } });
    });
    observePreviews();
    document.querySelector("#galleryEmpty").hidden = items.length !== 0;
  };

  const replaceControl = (selector, handler, event = "click") => {
    const old = document.querySelector(selector);
    if (!old) return null;
    const fresh = old.cloneNode(true);
    old.replaceWith(fresh);
    fresh.addEventListener(event, handler);
    return fresh;
  };
  replaceControl("#gallerySort", renderGallery, "change");
  replaceControl("#randomGallery", () => { if (galleryItems.length) openGalleryDialog(galleryItems[Math.floor(Math.random() * galleryItems.length)]); });
  const toggle = replaceControl("#gallerySpoilerToggle", () => {
    spoilersHidden = !spoilersHidden;
    syncToggle();
    renderGallery();
  });
  function syncToggle() {
    if (!toggle) return;
    toggle.textContent = spoilersHidden ? "spoilers hidden" : "spoilers shown";
    toggle.classList.toggle("active", spoilersHidden);
    toggle.setAttribute("aria-pressed", String(spoilersHidden));
  }
  syncToggle();

  const dialog = document.querySelector("#galleryDialog");
  dialog?.addEventListener("close", () => UI.pauseAllVideos({ unloadDialog: true }));
  document.querySelector("#closeGalleryDialog")?.addEventListener("click", () => UI.pauseAllVideos({ unloadDialog: true }));
})();
