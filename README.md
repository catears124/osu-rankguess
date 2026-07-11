# osu!rankguess — Vercel deployment

A production FastAPI backend and static frontend for the five-fold raw `.osr`
replay ensemble.

## Why this is one Python deployment

The training representation was written in NumPy/Python. Keeping the exact
`.osr -> tensors` implementation in Python avoids silent train/serve skew. The
frontend is static and served by Vercel's CDN; FastAPI handles only `/api/*`
and inference.

## Deploy

1. Push this directory to GitHub.
2. Import the repository into Vercel.
3. Add environment variables:
   - `OSU_CLIENT_ID`
   - `OSU_CLIENT_SECRET`
4. Deploy.

Register an osu! OAuth application from your osu! account settings. The app
uses the client-credentials flow only to resolve a replay's beatmap checksum
and mod-adjusted difficulty attributes.

The UI also supports manual star rating and map length when credentials are
not configured or a checksum cannot be resolved.

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

### `POST /api/predict`

Multipart fields:

- `replay`: `.osr` file
- `star`: optional manual mod-adjusted star rating
- `length_seconds`: optional manual mod-adjusted map length

### `GET /api/health`

Loads the ONNX model and reports its input contract.

## Notes

- osu!standard only.
- Relax and Autopilot are rejected because they were excluded from training.
- Maximum application upload is 4 MB, below Vercel's 4.5 MB function payload limit.
- `model.onnx` includes the five raw fold models; the failed Ridge stacker is not deployed.
