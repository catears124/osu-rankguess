# Model card: osu!rankguess

## Intended task

Estimate an osu! player's global-rank percentile from one
computer-readable `.osr` replay and beatmap/play metadata.

## Production architecture

Five player-disjoint cross-fitted models are evaluated in parallel.
Every model contains:

1. a tabular and whole-replay-summary base model;
2. a temporal event-sequence encoder;
3. a dense high-action-window encoder;
4. a bounded replay residual branch.

The final prediction is the unweighted mean of the five raw replay
predictions.

## Honest test performance

| Model | MAE | RMSE | R² |
|---|---:|---:|---:|
| Base fold ensemble | 0.361694 | 0.516560 | 0.646008 |
| Production replay ensemble | 0.357103 | 0.511677 | 0.652668 |

## Known limitation

The elite tail is sparse. The model is substantially better at ordering
ordinary and strong players than distinguishing the small number of globally
elite accounts from one replay.

## Leakage exclusions

The model does not use:

- username or user ID;
- current global rank;
- global-rank percentile;
- PP;
- account-wide accuracy;
- account play time;
- join date.
