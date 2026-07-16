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
