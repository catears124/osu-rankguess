/* Keep autoplay silent and only show the play prompt after an explicit pause. */
(() => {
  bindChallengeVideo = function fixedClickPlayback(root) {
    const video = root.querySelector(".challenge-video");
    const sound = root.querySelector(".video-toggle");
    const hint = root.querySelector(".video-playback-hint");
    const wrap = root.querySelector(".video-wrap");
    if (!video || !sound || !wrap) return;

    let userPaused = false;
    video.autoplay = true;
    video.defaultMuted = true;
    video.muted = true;
    video.loop = true;
    sound.textContent = "sound off";

    const hideHint = () => {
      if (!hint) return;
      hint.textContent = "";
      hint.classList.remove("visible");
    };

    const showPlayHint = () => {
      if (!hint || !userPaused) return;
      hint.textContent = "click to play";
      hint.classList.add("visible");
    };

    const syncState = () => {
      wrap.dataset.videoState = video.paused ? "paused" : "playing";
      video.setAttribute("aria-label", `Replay video. Click to ${video.paused ? "play" : "pause"}.`);
      sound.textContent = video.muted ? "sound off" : "sound on";
      sound.classList.toggle("on", !video.muted);
    };

    const start = async () => {
      try {
        await video.play();
        userPaused = false;
        hideHint();
        syncState();
        return true;
      } catch {
        syncState();
        showPlayHint();
        return false;
      }
    };

    const togglePlayback = async () => {
      if (video.paused) {
        await start();
        return;
      }
      userPaused = true;
      video.pause();
      syncState();
      showPlayHint();
    };

    video.addEventListener("click", togglePlayback);
    video.addEventListener("keydown", (event) => {
      if (!["Enter", " "].includes(event.key)) return;
      event.preventDefault();
      togglePlayback();
    });
    video.addEventListener("play", () => {
      userPaused = false;
      hideHint();
      syncState();
    });
    video.addEventListener("pause", syncState);
    video.addEventListener("loadeddata", () => start(), { once: true });
    video.addEventListener("error", () => {
      userPaused = false;
      hideHint();
      if (hint) {
        hint.textContent = "video unavailable";
        hint.classList.add("visible");
      }
    });

    sound.addEventListener("click", async (event) => {
      event.stopPropagation();
      video.muted = !video.muted;
      syncState();
      if (video.paused && !userPaused) await start();
    });

    syncState();
    requestAnimationFrame(() => start());
  };
})();
