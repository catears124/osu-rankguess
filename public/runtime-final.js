(() => {
  const original = bindChallengeVideo;
  bindChallengeVideo = function (root) {
    const video = root.querySelector('.challenge-video');
    if (!video || video.dataset.bound === '1') return;
    video.dataset.bound = '1';
    original(root);
    const autoplay = async () => {
      try {
        video.muted = false;
        await video.play();
      } catch {
        try {
          video.muted = true;
          await video.play();
        } catch {}
      }
    };
    video.addEventListener('loadeddata', autoplay, { once: true });
    if (video.readyState >= 2) autoplay();
  };
})();
