# osu!rankguess v11.1

A FastAPI/Vercel osu! rank-guessing game with four surfaces:

- **Daily 3** is the homepage and stores one independent community first guess per browser.
- **Infinite** selects a new public score, downloads its `.osr`, renders a fresh clip through o!rdr, and creates a private challenge row. It never samples the gallery.
- **Analyze** predicts a global rank from an uploaded `.osr` and can publish the result.
- **Gallery** uses beatmap thumbnails, filters, sorting, a replay dialog, and actual/model rank comparisons.

The guessing control uses a synchronized numeric input and a **soft-logarithmic** slider. It is a `log1p` transform rather than a pure logarithm, preserving useful precision at elite ranks without making the lower-ranked half impossible to control.

Raw uploaded `.osr` files are parsed during the request and are not stored in Postgres. Public rows store render metadata and URLs. Fresh Infinite rows are saved with `published = false` solely so the answer can be revealed and community guesses can be aggregated.

## Deploy

Copy the repository contents into the Vercel project root and leave Root Directory blank. Keep the Supabase/Vercel integration connected; the application prefers its injected `POSTGRES_URL` and removes provider-only URI options before connecting with psycopg.

Required and recommended variables are listed in `.env.example`. The important ones are:

```text
OSU_CLIENT_ID
OSU_CLIENT_SECRET
ORDR_API_KEY
CACHE_SIGNING_SECRET
CRON_SECRET
DAILY_CHALLENGE_SALT
GALLERY_ID_SALT
POSTGRES_URL
```

Redeploy after changing environment variables. `/api/health` should report application version `4.0.1`.

## Model behavior

The repository ships with the existing legacy ONNX replay ensemble, so it remains deployable immediately. `app.py` also understands a new `model/bundle.json` ensemble. When that bundle exists it uses:

- map-aware replay features with effective EZ/HR/DT/HT AR, OD, CS, HP, hit windows, and circle radius;
- timing/unstable-rate **proxies** from monotonic one-to-one key-down/object alignment;
- cursor-to-object aim-error proxies measured at the matched press time;
- locally calculated stable per-play PP from the `.osu` map and replay hit counts, with public-score PP as a validation/fallback;
- multiple static and multimodal ONNX members;
- OOF-derived non-negative blending and monotonic calibration.

Profile/account total PP is never used as a model feature because it directly leaks the player’s rank. Only the PP awarded to the submitted **play** is accepted.

## Train the v2 model

Install training-only dependencies and run the full preset:

```bash
pip install -r training/requirements.txt
python training/train_final_model.py \
  --data /kaggle/working/osu3k_splits.csv \
  --output /kaggle/working/rankguess-v2 \
  --preset max \
  --download-beatmaps \
  --enrich-score-pp \
  --pp-column auto \
  --population 5500000 \
  --workers 12 \
  --overwrite
```

The input supports CSV or Parquet. Required concepts are replay path, star rating, play accuracy, map length, player/group ID, and a target (`skill_target`, rank percentile, or global rank). Recommended additions are:

```text
beatmap_id or beatmap_path
score_pp / play_pp
score_id (optional; otherwise renderid or replay checksum is resolved automatically)
split with a held-out test set
```

The trainer calculates stable play PP locally whenever the `.osu` map is available. With `--enrich-score-pp`, it also validates/falls back through a public score ID or a conservative exact-score match. Beatmaps are resolved from `beatmap_id`, the canonical `renderid`, or the replay checksum. OAuth credentials must be in the environment. The trainer refuses a generic `pp` column when it appears constant per player or nearly reconstructs the rank target, because that is profile-PP leakage.

After training, replace the website’s `model/` contents with every file from:

```text
/kaggle/working/rankguess-v2/model/
```

The trainer writes cross-fitted OOF predictions, fold reports, player-disjoint locked-test metrics, elite/low-rank tail metrics, and ONNX verification results beside that folder. The `max` preset is intentionally expensive: it trains repeated group-CV CatBoost and deep replay ensembles rather than selecting on one convenient split.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
uvicorn app:app --reload
```

## Main API

```text
POST /api/replay/cache
POST /api/ordr/render
GET  /api/ordr/status
POST /api/predict
GET  /api/challenge/daily
POST /api/challenge/infinite
POST /api/challenge/guess
GET  /api/challenge/{id}/distribution
GET  /api/gallery
GET  /api/health
```
