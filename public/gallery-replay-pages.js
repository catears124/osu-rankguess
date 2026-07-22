/* Keep gallery replays in the gallery popup while copying canonical gallery links. */
(() => {
  const replayPathForID = (id) => `/gallery?replay=${encodeURIComponent(id)}`;
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
    if (spoilerPanel) {
      spoilerPanel.outerHTML = `<div class="dialog-ranks gallery-dialog-ranks-placeholder" aria-label="Ranks hidden until reveal">
        <div><span>actual</span><strong>#-</strong></div>
        <div><span>model</span><strong>#-</strong></div>
        <div><span>ratio</span><strong>-×</strong></div>
      </div>`;
    }
  }

  if (typeof galleryCard === "function") {
    const previousGalleryCard = galleryCard;
    galleryCard = function popupGalleryCard(item) {
      return previousGalleryCard(item)
        .replace(/\s*<button class="gallery-share"[^>]*>[\s\S]*?<\/button>/, "")
        .replace("<span>watch</span>", "")
        .replace("watch first, then reveal when ready", "reveal when ready");
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
    const copyButton = event.target.closest("[data-gallery-share]");
    if (!copyButton || !copyButton.closest("#galleryDialog")) return;
    if (!activePopupItem?.id) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    copyReplayLink(activePopupItem.id, copyButton);
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
