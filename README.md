# osu!rankguess v9

An intentionally slightly-janky FastAPI/Vercel site that predicts an osu!standard
player's **global rank** from one `.osr` replay.

It includes:

- direct `.osr` analysis through the five-fold ONNX ensemble;
- o!rdr video rendering;
- a public replay gallery with beatmap-cover thumbnails;
- a three-replay daily rank challenge;
- infinite random guessing mode;
- automatic gallery seeding from public osu! scores with downloadable replays.

Raw `.osr` files are parsed in memory and are never stored in Postgres. The public
database stores only replay/map metadata, the o!rdr video URL, the thumbnail URL,
the model prediction, and the player's current public global rank.

## Deploy

Put the repository contents at the Vercel project root and leave **Root Directory**
blank.

Keep the connected Supabase integration. Its managed `POSTGRES_URL` is used
automatically; do not create a fake `DATABASE_URL` with an unknown password.

Required environment variables:

```text
OSU_CLIENT_ID
OSU_CLIENT_SECRET
CACHE_SIGNING_SECRET
DAILY_CHALLENGE_SALT
GALLERY_ID_SALT
CRON_SECRET
```

Strongly recommended:

```text
ORDR_API_KEY
```

Generate each secret independently:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Optional configuration:

```text
ORDR_SKIN=whitecatCK1.0
ORDR_RESOLUTION=960x540
OSU_RANK_POPULATION=5500000
GALLERY_SEED_TARGET=12
SEED_RENDER_TIMEOUT_SECONDS=160
REPLAY_CACHE_TTL_SECONDS=1800
```

Redeploy after adding or changing environment variables.

## Gallery seeding

`vercel.json` installs three daily UTC cron jobs:

```text
02:05 UTC -> /api/cron/seed-gallery/0
10:05 UTC -> /api/cron/seed-gallery/1
18:05 UTC -> /api/cron/seed-gallery/2
```

Each job:

1. chooses a different rank stratum from the public osu!standard rankings;
2. finds a good public score whose replay is downloadable;
3. downloads and parses the replay using the exact production feature code;
4. renders it through o!rdr;
5. runs rank inference;
6. inserts it into the gallery with `source = cron`.

The jobs are duplicate-safe and stop once the number of challenge-eligible gallery
rows reaches `GALLERY_SEED_TARGET` (default: 12). User uploads count toward that
target, so the robot does not keep filling the database forever.

To populate the first three immediately after deployment, call the same protected
endpoints manually:

```bash
curl -H "Authorization: Bearer $CRON_SECRET" \
  https://osu-rankguess.vercel.app/api/cron/seed-gallery/0

curl -H "Authorization: Bearer $CRON_SECRET" \
  https://osu-rankguess.vercel.app/api/cron/seed-gallery/1

curl -H "Authorization: Bearer $CRON_SECRET" \
  https://osu-rankguess.vercel.app/api/cron/seed-gallery/2
```

With no verified `ORDR_API_KEY`, space manual calls apart to respect o!rdr's
unverified render limit. The scheduled jobs are already eight hours apart.

## Thumbnails

New records store the beatmap cover returned by osu!'s API. Existing gallery rows
without a stored thumbnail lazily backfill through:

```text
GET /api/gallery/{public_id}/thumbnail
```

The gallery opens the o!rdr MP4 when a thumbnail is clicked; it does not preload 24
videos at once.

## API

```text
POST /api/replay/cache
POST /api/ordr/render
GET  /api/ordr/status
POST /api/predict
GET  /api/gallery
GET  /api/gallery/{public_id}/thumbnail
GET  /api/challenge/daily
GET  /api/challenge/infinite
POST /api/challenge/guess
GET  /api/cron/seed-gallery/{slot}
GET  /api/health
```

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
uvicorn app:app --reload
```

Static frontend files live in `public/` and are served directly by Vercel.
