# osu!rankguess — Vercel deployment

A FastAPI backend and static frontend for the five-fold raw `.osr` replay
ensemble.

## Pipeline

1. The browser computes a SHA-256 identifier for the `.osr`.
2. `POST /api/replay/cache` parses the replay, builds all replay tensors, caches
   them in memory, and returns the replay username plus a signed stateless
   fallback token.
3. `POST /api/ordr/render` forwards the same replay to o!rdr and returns a
   `renderID`.
4. The browser polls `GET /api/ordr/status?renderID=...` until o!rdr returns a
   description and video URL.
5. `POST /api/predict` sends the replay hash, signed cache token, o!rdr
   description, and video URL. The backend parses star rating, song length, and
   accuracy from the description and runs the ONNX model.

The signed token matters on Vercel because sequential requests are not
promised to land on the same warm function instance.

## Deploy

1. Push this directory to GitHub.
2. Import it into Vercel.
3. Leave **Root Directory blank** when `app.py` is at the repository root.
4. Add `CACHE_SIGNING_SECRET` using a long random value.
5. Optionally add `ORDR_API_KEY` to avoid unauthenticated o!rdr limits.
6. Deploy.

Optional variables:

- `ORDR_SKIN=whitecatCK1.0`
- `ORDR_RESOLUTION=960x540`
- `OSU_RANK_POPULATION=3000000`
- `REPLAY_CACHE_TTL_SECONDS=1800`

The root URL is redirected to `/index.html` by `vercel.json`.

## Local development

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -e .
uvicorn app:app --reload
```

Open `http://127.0.0.1:8000`.

## API

### `POST /api/replay/cache`

Multipart fields:

- `replay`: `.osr` file
- `replay_hash`: SHA-256 hex digest of the exact upload

Returns `player`, `eventCount`, `replayHash`, and `cacheToken`.

### `POST /api/ordr/render`

Multipart fields:

- `replay`: the same `.osr`
- `replay_hash`: the same SHA-256 digest
- `username`: username returned by the cache endpoint

Returns `renderID`.

### `GET /api/ordr/status?renderID=123`

Returns o!rdr progress. Once ready, it includes `description` and `videoURL`.

### `POST /api/predict`

JSON body:

```json
{
  "replayHash": "...",
  "cacheToken": "...",
  "renderID": 123,
  "description": "Player: ...",
  "videoURL": "https://...issou.best/...mp4"
}
```

### `GET /api/health`

Loads the ONNX model and reports the input contract.

## Notes

- osu!standard only.
- Relax and Autopilot are rejected because they were excluded from training.
- Negative-delta replay marker/seed records are skipped instead of terminating
  replay decoding.
- Maximum application upload is 4 MB.
- `model.onnx` includes the five raw fold models; the failed Ridge stacker is
  not deployed.
