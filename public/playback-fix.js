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

/* Keep community chart copy neutral. */
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
