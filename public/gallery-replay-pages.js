/* Canonical fullscreen replay pages and copy-only gallery links. */
(() => {
  const replayPathForID = (id) => `/replay/${encodeURIComponent(id)}`;
  const replayURLForID = (id) => new URL(replayPathForID(id), location.origin).href;

  async function copyReplayLink(id, button) {
    if (!id) return;
    const url = replayURLForID(id);
    try {
      await navigator.clipboard.writeText(url);
    } catch {
      await copyText(url);
    }
    if (!button) return;
    button.textContent = "copied";
    setTimeout(() => {
      if (button.isConnected) button.textContent = "copy link";
    }, 1400);
  }

  function itemForCard(card) {
    const id = card?.dataset?.galleryId;
    if (!id || typeof galleryItems === "undefined") return null;
    return galleryItems.find((item) => item.id === id) || null;
  }

  if (typeof galleryCard === "function") {
    const previousGalleryCard = galleryCard;
    galleryCard = function replayPageGalleryCard(item) {
      return previousGalleryCard(item)
        .replace('aria-label="Share this replay">share</button>', 'aria-label="Copy replay link">copy link</button>');
    };
  }

  openGalleryDialog = function openFullscreenReplay(item) {
    if (!item?.id) return;
    location.assign(replayPathForID(item.id));
  };

  document.addEventListener("click", (event) => {
    const shareButton = event.target.closest(".gallery-share, [data-gallery-share]");
    if (!shareButton) return;
    const card = shareButton.closest(".gallery-card");
    const item = itemForCard(card);
    if (!item?.id) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    copyReplayLink(item.id, shareButton);
  }, true);

  const legacyReplayID = new URLSearchParams(location.search).get("replay")?.trim();
  if (location.pathname === "/gallery" && legacyReplayID) {
    location.replace(replayPathForID(legacyReplayID));
    return;
  }

  queueMicrotask(() => {
    if (typeof renderGallery === "function" && typeof galleryItems !== "undefined" && galleryItems.length) {
      renderGallery();
    }
  });
})();
