(() => {
  const states = new WeakMap();
  let activeVideo = null;
  let reconcileFrame = 0;

  const isVisible = (video) => {
    const view = video.closest(".view");
    return !document.hidden && (!view || !view.hidden);
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

  const setText = (node, value) => {
    if (node && node.textContent !== value) node.textContent = value;
  };

  const showHint = (video, text) => {
    const { hint } = controlsFor(video);
    if (!hint) return;
    setText(hint, text);
    hint.classList.add("visible");
    clearTimeout(Number(hint.dataset.timer || 0));
    hint.dataset.timer = String(setTimeout(() => hint.classList.remove("visible"), 650));
  };

  const sync = (video) => {
    const { wrap, sound, play } = controlsFor(video);
    if (wrap) wrap.dataset.videoState = video.paused ? "paused" : "playing";
    video.setAttribute("aria-label", `Replay video. Click to ${video.paused ? "play" : "pause"}.`);
    if (sound) {
      setText(sound, video.muted ? "sound off" : "sound on");
      sound.classList.toggle("on", !video.muted);
      sound.setAttribute("aria-pressed", String(!video.muted));
    }
    if (play) {
      setText(play, video.paused ? "play" : "pause");
      play.setAttribute("aria-label", video.paused ? "Play replay" : "Pause replay");
    }
  };

  const pauseVideo = (video) => {
    if (!video.paused) video.pause();
    if (video.classList.contains("challenge-video") && states.has(video)) sync(video);
  };

  const pauseOthers = (current) => {
    document.querySelectorAll("video").forEach((video) => {
      if (video !== current) pauseVideo(video);
    });
    activeVideo = current;
  };

  const attemptPlay = async (video, preferSound = true, explicit = false) => {
    const state = states.get(video);
    if (!state || (!explicit && state.userPaused) || !isVisible(video)) return false;

    state.userPaused = false;
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
        if (activeVideo === video) activeVideo = null;
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
      // Replacing the node still removes every listener attached by older layers.
    }
    source.replaceWith(video);

    const wrap = video.closest(".video-wrap");
    if (wrap && !wrap.querySelector(".video-play")) {
      const button = document.createElement("button");
      button.className = "video-play";
      button.type = "button";
      button.textContent = "pause";
      button.setAttribute("aria-label", "Pause replay");
      wrap.insertBefore(button, wrap.querySelector(".video-toggle"));
    }

    states.set(video, { userPaused: false });
    video.defaultMuted = false;
    video.muted = false;

    video.addEventListener("play", () => {
      if (!isVisible(video)) {
        pauseVideo(video);
        return;
      }
      pauseOthers(video);
      sync(video);
    });
    video.addEventListener("pause", () => {
      if (activeVideo === video) activeVideo = null;
      sync(video);
    });
    video.addEventListener("volumechange", () => sync(video));
    video.addEventListener("loadeddata", () => attemptPlay(video, true), { once: true });
    video.addEventListener("error", () => showHint(video, "video unavailable"));

    sync(video);
    if (video.readyState >= 2) queueMicrotask(() => attemptPlay(video, true));
    else video.load();
    return video;
  };

  const initializeWithin = (root) => {
    if (!(root instanceof Element || root instanceof Document)) return false;
    let found = false;
    if (root instanceof Element && root.matches(".challenge-video")) {
      initialize(root);
      found = true;
    }
    root.querySelectorAll?.(".challenge-video").forEach((video) => {
      initialize(video);
      found = true;
    });
    return found;
  };

  const reconcilePlayback = () => {
    reconcileFrame = 0;
    const visibleVideos = [];

    document.querySelectorAll(".challenge-video").forEach((source) => {
      const video = initialize(source);
      const state = states.get(video);
      if (!isVisible(video)) {
        pauseVideo(video);
      } else {
        visibleVideos.push(video);
      }
      if (!state) sync(video);
    });

    let preferred = activeVideo && visibleVideos.includes(activeVideo) && !activeVideo.paused
      ? activeVideo
      : visibleVideos.find((video) => !states.get(video)?.userPaused) || null;

    visibleVideos.forEach((video) => {
      if (video !== preferred) pauseVideo(video);
    });

    if (preferred && preferred.paused && !states.get(preferred)?.userPaused) {
      attemptPlay(preferred, !preferred.muted);
    }
  };

  const scheduleReconcile = () => {
    if (reconcileFrame) return;
    reconcileFrame = requestAnimationFrame(reconcilePlayback);
  };

  bindChallengeVideo = function definitiveVideoBinding(root) {
    if (!root) return;
    root.dataset.playbackBound = "1";
    delete root.dataset.playbackPending;
    initializeWithin(root);
    scheduleReconcile();
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
      await attemptPlay(video, !video.muted, true);
      showHint(video, "playing");
    } else {
      state.userPaused = true;
      pauseVideo(video);
      showHint(video, "paused");
    }
  }, true);

  document.addEventListener("play", (event) => {
    const video = event.target;
    if (!(video instanceof HTMLVideoElement)) return;
    if (!isVisible(video)) {
      pauseVideo(video);
      return;
    }
    pauseOthers(video);
    if (video.classList.contains("challenge-video") && states.has(video)) sync(video);
  }, true);

  document.addEventListener("click", (event) => {
    if (event.target instanceof Element && event.target.closest("[data-view-link]")) {
      setTimeout(scheduleReconcile, 0);
    }
  });
  window.addEventListener("popstate", () => setTimeout(scheduleReconcile, 0));
  window.addEventListener("pageshow", scheduleReconcile);

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      document.querySelectorAll("video").forEach(pauseVideo);
      activeVideo = null;
    } else {
      scheduleReconcile();
    }
  });

  const observer = new MutationObserver((mutations) => {
    let needsReconcile = false;
    for (const mutation of mutations) {
      if (mutation.type === "attributes") {
        if (mutation.target instanceof Element && mutation.target.matches(".view")) {
          needsReconcile = true;
        }
        continue;
      }
      mutation.addedNodes.forEach((node) => {
        if (node instanceof Element && initializeWithin(node)) needsReconcile = true;
      });
    }
    if (needsReconcile) scheduleReconcile();
  });

  observer.observe(document.documentElement, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ["hidden"],
  });

  initializeWithin(document);
  scheduleReconcile();
})();
