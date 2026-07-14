/* Let completed rounds minimize without advancing. */
(() => {
  const dismissResults = (backdrop) => {
    const panel = backdrop.closest(".reveal-panel") || backdrop.parentElement;
    const dialog = backdrop.querySelector(".result-dialog");
    const originalNext = backdrop.querySelector(".next-challenge");
    if (!panel || !dialog || !originalNext) return;

    backdrop.hidden = true;
    backdrop.style.display = "none";
    document.body.classList.remove("result-open");
    panel.querySelector(".result-after-dock")?.remove();

    const dock = document.createElement("div");
    dock.className = "result-after-dock";
    const actual = dialog.querySelector(".result-actual")?.textContent?.trim() || "round complete";
    const nextLabel = originalNext.textContent?.trim() || "next";
    dock.innerHTML = `<div class="result-after-summary"><span>round complete</span><strong>${actual}</strong></div>
      <div class="result-after-actions">
        <button class="secondary-button result-show" type="button">results</button>
        <button class="primary-button result-next" type="button">${nextLabel}</button>
      </div>`;
    panel.appendChild(dock);

    dock.querySelector(".result-show")?.addEventListener("click", () => {
      dock.remove();
      backdrop.hidden = false;
      backdrop.style.removeProperty("display");
      document.body.classList.add("result-open");
      requestAnimationFrame(() => dialog.focus({ preventScroll: true }));
    });
    dock.querySelector(".result-next")?.addEventListener("click", () => originalNext.click());

    const video = panel.closest(".polish-shell")?.querySelector(".challenge-video");
    if (video?.paused) video.play().catch(() => {});
  };

  document.addEventListener("click", (event) => {
    const backdrop = event.target instanceof Element ? event.target.closest(".result-backdrop") : null;
    if (!backdrop || backdrop.hidden) return;
    const dialog = backdrop.querySelector(".result-dialog");
    if (dialog?.contains(event.target)) return;
    event.preventDefault();
    event.stopPropagation();
    dismissResults(backdrop);
  }, true);
})();
