# osu!rankguess

osu!rankguess is a browser game and replay-analysis tool for estimating an osu!standard player's global rank from gameplay.

The site has four main parts:

- **Daily** presents three replay clips each day. The player gets up to five guesses per clip and can compare their first guess with the community distribution.
- **Infinite** generates an endless sequence of replay challenges.
- **Analyze** accepts an `.osr` replay, renders it through o!rdr, extracts replay and map features, and returns a predicted global rank.
- **Gallery** collects published results and shows the model's estimate beside the player's current public rank.

## How it works

For uploaded replays, the browser sends the `.osr` file to the application for parsing and directly submits the replay to o!rdr for video rendering. The backend combines three sources of information:

1. replay telemetry, including cursor movement, key presses, timing, and hit-event structure;
2. beatmap and score context, including difficulty, length, accuracy, mods, and play-level performance estimates;
3. the rendered clip and public osu! metadata used by the game and gallery.

The model predicts a player's global-rank percentile. The application converts that percentile into an approximate rank using the active osu!standard population estimate. The result is an estimate of player skill from one replay, not a lookup of the player's account rank.

Raw uploaded replay files are parsed during the request and are not stored in Postgres. Published entries retain the render URL and the metadata needed for the gallery and challenge modes.

## osu!3k

The model was developed from [osu!3k](https://github.com/catears124/osu-3k), a dataset I collected for learning player skill from osu! gameplay.

The final dataset contains 2,999 osu!standard plays from 1,757 unique players. Each example includes the replay, a rendered gameplay video, map and score metadata, and the player's global rank at collection time. The dataset was built to support player-disjoint evaluation, so the model is tested on players it did not see during training rather than on additional plays from the same accounts.

Useful fields include:

- raw `.osr` replay data;
- rendered gameplay video;
- player and beatmap identifiers;
- star rating and map length;
- score accuracy and enabled mods;
- global rank and global-rank percentile.

## Model

The production model is an ensemble of replay-based regressors exported to ONNX for CPU inference.

Each replay is converted into three representations:

- a fixed-length summary of global replay statistics;
- an event sequence describing cursor, key, and timing behavior across the play;
- local action windows centered on important gameplay events.

These replay features are combined with static map and score features such as star rating, accuracy, map length, mod-adjusted difficulty, hit counts, and play-level performance estimates. Player identity, account total PP, and global rank are excluded from the input features because they would directly leak the target.

The training pipeline uses player-grouped cross-validation, out-of-fold prediction, ensemble blending, and monotonic calibration. Temporal models were evaluated during development, but the final deployed bundle keeps the branches that improved held-out performance and packages them as ONNX models for Vercel's CPU runtime.

### Performance

On the locked player-disjoint test split of osu!3k, the final model reached:

| Metric | Result |
| --- | ---: |
| R² | 0.8462 |
| RMSE | 0.03994 |
| MAE | 0.01842 |

The target is global-rank percentile on a 0 to 1 scale. An MAE of 0.01842 corresponds to an average error of about 1.84 percentile points.

These metrics describe performance on the collected osu!3k distribution. Rank estimates can be less reliable for unusual mods, incomplete replays, maps far outside the training range, or players whose current rank has changed substantially since the replay was recorded.

## Stack

- FastAPI and Python for replay parsing, feature extraction, inference, and APIs
- ONNX Runtime for production model inference
- Postgres and Supabase for gallery entries, challenge guesses, and daily state
- o!rdr for replay rendering
- Vercel for deployment

## Local development

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e .
uvicorn app:app --reload
```

On macOS or Linux, activate the environment with:

```bash
source .venv/bin/activate
```

Environment variables used by the deployed application are documented in `.env.example`.

## Disclaimer

osu!rankguess is an unofficial project and is not affiliated with osu!, ppy Pty Ltd, or o!rdr.