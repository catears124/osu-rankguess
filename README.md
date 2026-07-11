# osu!rankguess v3.2.0

A FastAPI + static frontend deployment for Vercel.

## Main modes

- **Daily 3** is the home page.
- **Infinite** sources a fresh public replay, renders a new clip through o!rdr, and does not read from or write to the gallery.
- **Analyze replay** predicts a rank from a user-uploaded `.osr`.
- **Gallery** shows community uploads and cron-seeded samples with beatmap thumbnails.

Daily guesses are stored in Postgres. The distribution shown after reveal uses one first guess per browser session, so repeated attempts do not dominate the chart.

## Required environment variables

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

`POSTGRES_URL` is normally injected by the Supabase Vercel integration. Do not create a fake `DATABASE_URL`.

Optional:

```text
OSU_RANK_POPULATION=5500000
GALLERY_SEED_TARGET=12
SEED_RENDER_TIMEOUT_SECONDS=160
REPLAY_CACHE_TTL_SECONDS=1800
ORDR_SKIN=whitecatCK1.0
ORDR_RESOLUTION=960x540
```

## Deploy

Place these files at the Vercel project root and redeploy. The database migration runs automatically on the first request that needs it.

After deployment, verify:

```text
/api/health
```

Expected version:

```json
{"version":"3.2.0"}
```

## Cron

The existing three daily gallery-seeding cron routes remain enabled. Vercel sends `CRON_SECRET` automatically as a Bearer token.
