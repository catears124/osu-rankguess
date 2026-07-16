/* Final user-facing submit and gallery copy cleanup. */
(() => {
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
