/* Reuse an existing o!rdr job instead of submitting the same replay again. */
(() => {
  const originalCreateRender = createRender;
  const keyFor = (replayHash) => `osu-rankguess-render-v1-${String(replayHash || "").toLowerCase()}`;

  const readSaved = (replayHash) => {
    try {
      const value = JSON.parse(storage.get(keyFor(replayHash)) || "null");
      const renderID = Number(value?.renderID);
      return renderID > 0 ? renderID : null;
    } catch {
      return null;
    }
  };

  const save = (replayHash, renderID) => {
    const value = Number(renderID);
    if (value > 0) storage.set(keyFor(replayHash), JSON.stringify({ renderID: value, savedAt: Date.now() }));
  };

  const reusable = async (renderID) => {
    try {
      const status = await requestJSON(`/api/ordr/status?renderID=${encodeURIComponent(renderID)}`);
      return !status.failed;
    } catch {
      return false;
    }
  };

  createRender = async function recoveredCreateRender(file, replayHash, username) {
    const savedRenderID = readSaved(replayHash);
    if (savedRenderID && await reusable(savedRenderID)) {
      return { ok: true, renderID: savedRenderID, player: username, replayHash, reused: true };
    }

    const created = await originalCreateRender(file, replayHash, username);
    save(replayHash, created?.renderID);
    return created;
  };
})();
