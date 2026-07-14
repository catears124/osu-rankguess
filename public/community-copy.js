/* Keep the community chart copy neutral and uncluttered. */
(() => {
  const cleanCommunityCopy = (root = document) => {
    root.querySelectorAll?.(".community-distribution-head small").forEach((node) => {
      node.textContent = String(node.textContent || "")
        .replace(/\breal\s+(?=guess(?:es)?\b)/gi, "")
        .replace(/\s*·\s*baseline-smoothed\b/gi, "")
        .trim();
    });

    root.querySelectorAll?.(".community-bar[title]").forEach((node) => {
      const title = String(node.getAttribute("title") || "")
        .replace(/\s*·\s*\d+\s+real\b/gi, "")
        .trim();
      node.setAttribute("title", title);
    });

    root.querySelectorAll?.(".community-distribution p").forEach((node) => {
      for (const child of [...node.childNodes]) {
        if (child.nodeType !== Node.TEXT_NODE) continue;
        child.textContent = String(child.textContent || "")
          .replace(/\s*·\s*baseline fades as real guesses arrive\b/gi, "");
      }
    });
  };

  const observer = new MutationObserver((records) => {
    for (const record of records) {
      for (const node of record.addedNodes) {
        if (!(node instanceof Element)) continue;
        if (node.matches?.(".community-distribution")) cleanCommunityCopy(node);
        else if (node.querySelector?.(".community-distribution")) cleanCommunityCopy(node);
      }
    }
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      cleanCommunityCopy();
      observer.observe(document.body, { childList: true, subtree: true });
    }, { once: true });
  } else {
    cleanCommunityCopy();
    observer.observe(document.body, { childList: true, subtree: true });
  }
})();
