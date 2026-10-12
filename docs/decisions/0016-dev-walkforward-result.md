# 0016 — Dev walk-forward result: heads pass, learned exits do not beat flat

Status: RESULT · 11 Oct 2026 · dev only, holdout still sealed

## What ran

- **`model/walkforward.py`:**
  - 5 monthly test folds, Feb to Jun 2026, expanding window with a 1-session embargo.
  - Early stopping on a 20-session validation slice, never on the test month.
  - A pre-declared 4-config grid, chosen by mean validation MAE.
  - Label-shuffle guard and date canary.
- **`model/policy_eval.py`:** learned bands TP = a·pred_MFE and SL = b·pred_MAE, against flat exits.
  - a, b and the in-fold "best flat" are chosen on the validation slice only.
  - Each fold is scored on its unseen test month with `replay.py`, net of 1 tick commission plus 1 tick slippage on market exits.

## Heads (cluster-first entries, all sessions)

| Head | Beats mean | Beats vol-bucket median | Test Spearman | Shuffled Spearman |
|---|---|---|---|---|
| mfe_ticks | 5/5 | 5/5 | 0.40 | 0.03 |
| mae_ticks | 5/5 | 5/5 | 0.40 | 0.03 |
| minutes_to_mfe | 0/5 | 0/5 | 0.10 | −0.01 |

- **Date canary is low:** day_index ranks 8–11 of ~30 features, with 1–4.5% of gain.
- **The heads mostly read volatility:** the top features are atr_historical, volatility_pass and bar_range_ticks. They predict how big the moves will be, not which way.
- **Separation fails.** Predicted MFE − MAE does not rank realized outcomes. The Spearman with the 60-min move is +0.02, +0.06, −0.15, −0.10 and +0.06 by fold, and the quintile spreads flip sign.

## Exit policy on unseen months (RTH 08:30–15:00, net ticks per trade)

| Population | Learned | Flat 200/400 | Best flat (in fold) | Learned − flat 200/400 |
|---|---|---|---|---|
| cluster_first (22,691 trades) | −0.8 [−5.8, +4.4] | −1.0 [−8.4, +6.5] | −3.9 [−23.2, +15.9] | +0.2 [−7.1, +7.6] |
| v2-live approximation (707 trades) | +4.9 [−9.9, +20.4] | −4.2 [−18.9, +12.8] | −12.3 [−44.3, +21.0] | +9.1 [−9.5, +26.8] |

- **No expectancy gain.** The learned a, b swing between folds (0.25/3.0, 1.5/0.5, 1.0/3.0, 1.0/2.0, 1.0/0.5), which is noise-fitting. In-fold best flat does worse than 200/400, for the same reason.
- **Lower risk, but not proven to come from learning.**
  - The learned exits have a per-trade std of 202 vs 300, and a smaller worst day in 4/5 months.
  - But their average stop is 289 vs 400, so a smaller flat stop may do the same.
  - A risk-matched flat comparison is required before claiming any benefit.

## Decision

- **Do NOT open the holdout.** Dev shows nothing for it to confirm, and a failed holdout can't be reused. Keep it sealed for a version that passes on dev.
- **Phase 2 thesis, "learned exits alone beat flat": NOT supported on this year.**
- **Options for Avery and Matthew** (see the handoff doc):
  1. Reconcile with real bot results; the entry rule may differ from the assumption here.
  2. Volatility-scaled exits as risk control, if they beat a risk-matched flat on dev.
  3. Make v3 a trade-quality predictor that can skip trades, with new directional context features (Layer 1 daily context was never collected).
  4. Stop stacking contracts.
