/* Final interaction overrides. */
(() => {
  const originalBindChallengeVideo = bindChallengeVideo;

  bindChallengeVideo = function soundOnChallengeVideo(root) {
    originalBindChallengeVideo(root);
    const video = root.querySelector(".challenge-video");
    const sound = root.querySelector(".video-toggle");
    if (!video || !sound) return;

    video.defaultMuted = false;
    video.muted = false;
    sound.textContent = "sound on";
    sound.classList.add("on");

    const startWithSound = () => {
      video.muted = false;
      sound.textContent = "sound on";
      sound.classList.add("on");
      video.play().catch(() => {
        const hint = root.querySelector(".video-playback-hint");
        if (hint) {
          hint.textContent = "click to play";
          hint.classList.add("visible");
        }
      });
    };

    video.addEventListener("loadeddata", startWithSound, { once: true });
    requestAnimationFrame(startWithSound);
  };

  const copy = async (text) => {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  };

  document.addEventListener("click", async (event) => {
    const button = event.target instanceof Element ? event.target.closest("#shareDaily") : null;
    if (!button) return;

    event.preventDefault();
    event.stopImmediatePropagation();

    const grid = document.querySelector(".share-grid")?.innerText?.trim() || "";
    const date = document.querySelector(".daily-summary .kicker")?.textContent?.trim() || "";
    const text = `osu!rankguess ${date}\n${grid}\n${location.origin}/#daily`;

    try {
      await copy(text);
      button.textContent = "copied";
    } catch {
      button.textContent = "copy failed";
    }
  }, true);
})();
