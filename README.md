# osu!rankguess v7

A monochrome FastAPI/Vercel app that:

- predicts a player's **global rank** from one `.osr` replay;
- renders the replay through o!rdr;
- optionally publishes the result to a public gallery;
- runs a three-replay daily rank challenge;
- provides an endless random challenge mode.

Raw `.osr` files are parsed during the request and are not stored in Postgres.
The public database stores replay metadata, the o!rdr video URL, the model
prediction, and the player's osu! rank at submission time.

## Deploy

Copy the repository contents into the project root and leave Vercel's Root
Directory blank.

Keep the existing environment variables and add a serverless Postgres database.
On Vercel, install a Postgres provider from **Storage / Marketplace** (Neon is a
simple option) and connect it to this project. Confirm that it injects
`DATABASE_URL`, then redeploy.

Required/recommended environment variables are documented in `.env.example`.
Use stable random secrets for:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Set the output independently as `CACHE_SIGNING_SECRET`,
`DAILY_CHALLENGE_SALT`, and `GALLERY_ID_SALT`.

## API

- `POST /api/replay/cache`
- `POST /api/ordr/render`
- `GET /api/ordr/status`
- `POST /api/predict`
- `GET /api/gallery`
- `GET /api/challenge/daily`
- `GET /api/challenge/infinite`
- `POST /api/challenge/guess`
- `GET /api/health`

## Daily rules

The daily uses three public submissions with known osu! global ranks. Each
replay allows five guesses. A guess within 10% of the stored rank is accepted.
The three replay IDs are selected once per UTC date and persisted so the daily
does not change when new gallery items are submitted.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
uvicorn app:app --reload
```

Static files are in `public/`; on Vercel they are served directly.


## Supabase / psycopg URI compatibility

Version 3.0.1 sanitizes provider-only query parameters (including `supa`)
from the Vercel-injected `POSTGRES_URL` before passing it to psycopg. The
application prefers the integration-managed `POSTGRES_URL` over a manually
created `DATABASE_URL`.
