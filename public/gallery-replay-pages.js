/* Keep gallery replays in the gallery popup while copying canonical replay links. */
(() => {
  const replayPathForID = (id) => `/replay/${encodeURIComponent(id)}`;
  const replayURLForID = (id) => new URL(replayPathForID(id), location.origin).href;
  let activePopupItem = null;

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

  function normalizePopupControls() {
    const body = document.querySelector("#galleryDialogBody");
    if (!body) return;
    body.querySelectorAll("[data-gallery-share]").forEach((button) => {
      const currentText = button.textContent.trim();
      if (currentText !== "copied" && currentText !== "copy link") button.textContent = "copy link";
      if (button.getAttribute("aria-label") !== "Copy replay link") {
        button.setAttribute("aria-label", "Copy replay link");
      }
    });
    const spoilerPanel = body.querySelector(".dialog-spoiler-panel");
    const hiddenValue = spoilerPanel?.querySelector("strong");
    if (hiddenValue && hiddenValue.textContent !== "-") hiddenValue.textContent = "-";
    const label = spoilerPanel?.querySelector("span");
    if (label && !label.querySelector("small")) {
      const hint = document.createElement("small");
      hint.textContent = "click to reveal";
      label.appendChild(hint);
    }
  }

  if (typeof galleryCard === "function") {
    const previousGalleryCard = galleryCard;
    galleryCard = function popupGalleryCard(item) {
      return previousGalleryCard(item)
        .replace('aria-label="Share this replay">share</button>', 'aria-label="Copy replay link">copy link</button>');
    };
  }

  if (typeof openGalleryDialog === "function") {
    const previousOpenGalleryDialog = openGalleryDialog;
    openGalleryDialog = function restoredGalleryDialog(item, options = {}) {
      if (!item?.id) return;
      activePopupItem = item;
      const result = previousOpenGalleryDialog(item, options);
      normalizePopupControls();
      return result;
    };
  }

  document.addEventListener("click", (event) => {
    const copyButton = event.target.closest(".gallery-share, [data-gallery-share]");
    if (!copyButton) return;
    const cardItem = itemForCard(copyButton.closest(".gallery-card"));
    const item = cardItem || activePopupItem;
    if (!item?.id) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    copyReplayLink(item.id, copyButton);
  }, true);

  const dialogBody = document.querySelector("#galleryDialogBody");
  if (dialogBody && "MutationObserver" in globalThis) {
    new MutationObserver(normalizePopupControls).observe(dialogBody, {
      childList: true,
      subtree: true,
    });
  }

  document.querySelector("#galleryDialog")?.addEventListener("close", () => {
    activePopupItem = null;
  });

  queueMicrotask(() => {
    if (typeof renderGallery === "function" && typeof galleryItems !== "undefined" && galleryItems.length) {
      renderGallery();
    }
    normalizePopupControls();
  });
})();
