(() => {
  const states = new WeakMap();
  let activeVideo = null;

  const visible = (video) => {
    const view = video.closest(".view");
    return !view || !view.hidden;
  };

  const controlsFor = (video) => {
    const wrap = video.closest(".video-wrap");
    return {
      wrap,
      sound: wrap?.querySelector(".video-toggle") || null,
      play: wrap?.querySelector(".video-play") || null,
      hint: wrap?.querySelector(".video-playback-hint") || null,
    };
  };

  const showHint = (video, text) => {
    const { hint } = controlsFor(video);
    if (!hint) return;
    hint.textContent = text;
    hint.classList.add("visible");
    window.clearTimeout(Number(hint.dataset.timer || 0));
    const timer = window.setTimeout(() => hint.classList.remove("visible"), 650);
    hint.dataset.timer = String(timer);
  };

  const sync = (video) => {
    const { wrap, sound, play } = controlsFor(video);
    if (wrap) wrap.dataset.videoState = video.paused ? "paused" : "playing";
    video.setAttribute("aria-label", `Replay video. Click to ${video.paused ? "play" : "pause"}.`);
    if (sound) {
      sound.textContent = video.muted ? "sound off" : "sound on";
      sound.classList.toggle("on", !video.muted);
      sound.setAttribute("aria-pressed", String(!video.muted));
    }
    if (play) {
      play.textContent = video.paused ? "play" : "pause";
      play.setAttribute("aria-label", video.paused ? "Play replay" : "Pause replay");
    }
  };

  const pauseOthers = (current) => {
    document.querySelectorAll("video").forEach((other) => {
      if (other === current || other.paused) return;
      other.pause();
      if (other.classList.contains("challenge-video")) sync(other);
    });
    if (activeVideo && activeVideo !== current && !activeVideo.paused) {
      activeVideo.pause();
      sync(activeVideo);
    }
    activeVideo = current;
  };

  const attemptPlay = async (video, preferSound = true) => {
    const state = states.get(video);
    if (!state || state.userPaused || !visible(video)) return false;

    pauseOthers(video);
    video.autoplay = true;
    video.loop = true;
    video.playsInline = true;

    try {
      video.muted = !preferSound;
      await video.play();
      sync(video);
      return true;
    } catch {
      try {
        video.muted = true;
        await video.play();
        sync(video);
        return true;
      } catch {
        sync(video);
        showHint(video, "click to play");
        return false;
      }
    }
  };

  const initialize = (source) => {
    if (!source || source.dataset.rankguessPlayer === "1") return source;

    const video = source.cloneNode(true);
    video.dataset.rankguessPlayer = "1";
    video.autoplay = true;
    video.loop = true;
    video.playsInline = true;
    video.preload = "auto";

    try {
      source.pause();
      source.muted = true;
      source.removeAttribute("src");
      source.querySelectorAll("source").forEach((node) => node.removeAttribute("src"));
      source.load();
    } catch {
      // The replacement below still removes any listeners attached by older layers.
    }
    source.replaceWith(video);

    const wrap = video.closest(".video-wrap");
    if (wrap && !wrap.querySelector(".video-play")) {
      const button = document.createElement("button");
      button.className = "video-play";
      button.type = "button";
      button.textContent = "pause";
      button.setAttribute("aria-label", "Pause replay");
      const sound = wrap.querySelector(".video-toggle");
      wrap.insertBefore(button, sound || null);
    }

    states.set(video, { userPaused: false });
    video.defaultMuted = false;
    video.muted = false;

    video.addEventListener("play", () => {
      if (!visible(video)) {
        video.pause();
        return;
      }
      pauseOthers(video);
      sync(video);
    });
    video.addEventListener("pause", () => sync(video));
    video.addEventListener("volumechange", () => sync(video));
    video.addEventListener("loadeddata", () => attemptPlay(video, true), { once: true });
    video.addEventListener("error", () => showHint(video, "video unavailable"));

    sync(video);
    if (video.readyState >= 2) attemptPlay(video, true);
    else video.load();
    return video;
  };

  const initializeWithin = (root) => {
    if (!root) return;
    if (root.matches?.(".challenge-video")) initialize(root);
    root.querySelectorAll?.(".challenge-video").forEach(initialize);
  };

  const reconcilePlayback = () => {
    const visibleVideos = [];
    document.querySelectorAll(".challenge-video").forEach((source) => {
      const video = initialize(source);
      if (!visible(video)) {
        if (!video.paused) video.pause();
        sync(video);
        return;
      }
      visibleVideos.push(video);
    });

    const preferred = visibleVideos.find((video) => !states.get(video)?.userPaused) || null;
    if (preferred && preferred.paused) attemptPlay(preferred, !preferred.muted);
    visibleVideos.forEach((video) => {
      if (video !== preferred && !video.paused) video.pause();
      sync(video);
    });
  };

  bindChallengeVideo = function definitiveVideoBinding(root) {
    if (!root) return;
    root.dataset.playbackBound = "1";
    delete root.dataset.playbackPending;
    initializeWithin(root);
    reconcilePlayback();
  };

  document.addEventListener("click", async (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) return;

    const sound = target.closest(".video-toggle");
    if (sound) {
      const source = sound.closest(".video-wrap")?.querySelector(".challenge-video");
      if (!source) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      const video = initialize(source);
      video.muted = !video.muted;
      sync(video);
      showHint(video, video.muted ? "sound off" : "sound on");
      return;
    }

    const playButton = target.closest(".video-play");
    const clickedVideo = target.closest(".challenge-video");
    if (!playButton && !clickedVideo) return;

    const source = clickedVideo || playButton.closest(".video-wrap")?.querySelector(".challenge-video");
    if (!source) return;
    event.preventDefault();
    event.stopImmediatePropagation();

    const video = initialize(source);
    const state = states.get(video);
    if (video.paused) {
      state.userPaused = false;
      await attemptPlay(video, !video.muted);
      showHint(video, "playing");
    } else {
      state.userPaused = true;
      video.pause();
      if (activeVideo === video) activeVideo = null;
      sync(video);
      showHint(video, "paused");
    }
  }, true);

  const observer = new MutationObserver((mutations) => {
    let shouldReconcile = false;
    for (const mutation of mutations) {
      if (mutation.type === "childList") {
        mutation.addedNodes.forEach((node) => {
          if (node instanceof Element) initializeWithin(node);
        });
        shouldReconcile = true;
      } else if (mutation.type === "attributes") {
        shouldReconcile = true;
      }
    }
    if (shouldReconcile) queueMicrotask(reconcilePlayback);
  });

  observer.observe(document.documentElement, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ["hidden"],
  });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      document.querySelectorAll("video").forEach((video) => video.pause());
      activeVideo = null;
    } else {
      reconcilePlayback();
    }
  });

  initializeWithin(document);
  reconcilePlayback();
})();
