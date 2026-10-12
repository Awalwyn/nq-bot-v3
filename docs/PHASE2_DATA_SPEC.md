# NQ Bot v3 — Phase 2 Data-Handling Spec (anti-contamination / anti-overfitting)

_Agreed 11 Oct 2026. This is the contract for every Phase 2 step. If a step needs to break a rule here, change this doc first, with the reason._

## 0. Operating decisions (from Avery, 11 Oct 2026)

| Decision | Value | What it changes |
|---|---|---|
| Sessions | **Train on RTH + overnight; go live on RTH only** | `is_rth` is a model feature; every result is reported RTH and overnight separately; the go/no-go is judged on **RTH**. We also train an RTH-only model and keep whichever is better on RTH. Overnight goes live later only after passing its own holdout test. |
| Force-close at 15:00 CT | **No** | The logger's 15:00 RTH cut is not the real outcome → those windows are rebuilt to the full 60 min from bars (done, §2). The flat baseline has no timeout live, so the evaluator must replay it past 60 min on the bars file until TP/SL. |
| Position limit | **Max 7 contracts** | Evaluation simulates the live position rules: skip a signal when 7 are already open. **Assumed** until confirmed: 1 contract per entry, opposite-direction signals ignored while in a position (the strategy's "Ignore Opposite Signal While In Position"). Affects scoring only, not training. |

Prop-firm note: if trading eval/funded accounts, check the firm's end-of-day rule. Many auto-liquidate near the close, which would be a force-close in practice and must be modelled.

## 1. Locked holdout

- **Dev:** CME sessions 2025-10-13 → 2026-06-29. **Embargo:** session 2026-06-30 removed. **Holdout:** sessions 2026-07-01 → 2026-10-09.
- The holdout is a separate file (`holdout_LOCKED/`). `dataset.load_clean()` raises `HoldoutLockedError` unless `allow_holdout=True`.
- The holdout is touched **once**, for the final learned-vs-flat test. A failed holdout is not followed by tweaking and re-testing on it. Any next attempt needs new data (e.g. the forward paper period).
- Splits are by CME session (17:00 CT roll). Each cluster belongs to the session of its first signal, so no cluster straddles a boundary. Guard: the last dev label window ends before the first holdout signal (asserted).

## 2. Cleaning rules (implemented: `data_pipeline/clean.py`, ADR 0013)

**Principle: drop rows because the data is broken, never because an outcome is extreme.**

1. **Cross-source integrity.** For every signal, MFE/MAE are rebuilt from the 15s bars over exactly the span the logger observed and compared with the tick labels. Disagreement > 4 ticks → drop. Zero bars in the window → drop.
2. **Session quarantine.** Any disagreement > 12 ticks is a corrupt print. It can also contaminate later features (ATR, range, kernel), so that session is dropped **from the first window that could have touched it to session end**.
3. **Whole-cluster rule.** If a cluster's first signal fails, the whole cluster is dropped. Otherwise `entries.py` would promote a later row into a fake entry.
4. **15:00-cut RTH windows are extended** to the full 60 min from bars. They are rebuilt only if the bars cover the full window with no internal hole > 300 s; otherwise they stay censored.
5. `session_date` is recomputed on the CME 17:00 CT roll (the logger's copy is kept as `session_date_logger`).
6. `dist_vwap_ticks` is removed from the clean file (wrong VWAP session boundary).

**Rebuild validated** on 20,000 uncensored rows (bars vs tick labels):

| Field | Agreement | Tolerance |
|---|---|---|
| mfe_ticks | 100% | 1 tick |
| mae_ticks | 99.995% | 1 tick |
| minutes_to_mfe / minutes_to_mae | 100% | 15 s |
| delta_15m | 99.99% | 2 ticks |
| delta_60m | 100% | 2 ticks |
| final_delta_ticks | 99.6% | 2 ticks |

**Result on run_20261011T220004Z_0ff4b383:**

| | Signals | Entries |
|---|---|---|
| Raw | 1,124,484 | 202,313 |
| Quarantined session tails (12 sessions) | −10,777 | −1,954 |
| Bar/tick disagreement | −1,785 | −267 |
| No bars in window | −1,089 | −197 |
| Cluster entry failed | −949 | — |
| Embargo (2026-06-30) | −4,689 | −898 |
| **Dev** | **795,692** | **143,446** (40,245 RTH / 103,201 overnight) |
| **Holdout** | **309,503** | **55,551** (15,953 RTH / 39,598 overnight) |

- **15:00-cut windows extended:** 47,943 signals (101 failed coverage and stay censored).
- **Censored entries left:** dev 6,313, holdout 2,331. These are mostly overnight windows crossing the 16:00 halt or feed holes.
- **Quarantined sessions:** 2025-12-23, 2026-03-20, 03-27, 04-10, 05-22, 06-10, 06-11, 06-25, 07-20, 07-24, 07-29, 08-28.
- Guard suite `scripts/check_clean.py`: 19/19 pass.

**Data notes for Matthew's next run:**
- Template "Default 24 x 5" dropped **Sunday 17:00–23:00 CT** every week (overnight only).
- The bad-close-tick pattern (corrupt print in the last RTH hour) recurs on **Fridays**: Mar 20, Mar 27, Apr 10, May 22, Jul 24, Aug 28.
- Use **CME US Index Futures ETH**.

## 3. Features (`schema.MODEL_FEATURE_CANDIDATES`)

- **Rule:** known at the signal bar close, scale-free, and computable identically in NinjaScript live.
- **Excluded (`schema.EXCLUDED_FEATURES`):**
  - absolute price levels (`ema200_value`, `ema800_value`, `sma200_value`, `kernel_value`, `kernel_gaussian`): back-adjusted, and they fingerprint the date
  - `dist_vwap_ticks`
  - raw `volume`
  - `filter_config`
  - `day_of_week`
- **Added:** `score`, `is_long`, `is_rth`.
- **Forbidden in X:** all labels plus the clean-audit columns (`xs_*` are computed from labels).

## 4. Validation design (dev only)

- **Walk-forward, expanding window, monthly test folds Feb → Jun 2026 (5 folds)**, cut at session boundaries with a 1-session embargo.
- In each fold, the last month of the training span is the validation slice for early stopping. **Never early-stop on a test fold** (the current `train.py` passes the test set as `eval_set` — to be fixed).
- **Everything learned is learned inside the fold:** feature selection (`analyze.py` runs on dev/training folds only), hyperparameters, censoring treatment, exit-band coefficients and clamps, and the "best flat" baseline.
- Censored rows (now ~4% of entries, mostly overnight) are excluded from the MFE/MAE heads. Their count is reported per fold.

## 5. Overfitting / memorization guards

- **Search:** a small pre-declared hyperparameter grid (~15 configs), every trial logged; pick by **mean across folds**, not the best fold.
- **Model size:** shallow trees and a large `min_child_weight`. The effective sample is far below 143K (≈800 entries/day with overlapping windows).
- **Head baselines:** each head must beat predict-the-mean **and** a volatility-bucket median in ≥4 of 5 folds.
- **Label-shuffle test:** with shuffled targets, skill must vanish (otherwise something leaks).
- **Date canary:** a day-index feature is added on purpose. High importance means the model is fingerprinting regimes.
- **Fold reporting:** the train-vs-validation gap is reported per fold.
- **RTH vs overnight:** results always reported separately.
- **Confidence intervals:** day-block bootstrap CIs on every expectancy difference.

## 6. The thesis test (pre-registered)

- **Where:** on the holdout, **RTH entries**, under the live position rules (max 7 contracts).
- **Pass condition:** the learned exit must beat **both**:
  - flat 200/400, replayed past 60 min until TP/SL because there is no force-close live
  - the **best flat TP/SL tuned on dev**
- **Margin:** the day-block bootstrap 95% CI on the per-trade expectancy difference must exclude zero.
- **Also reported:** total P&L, max drawdown and worst day, since 7 stacked same-direction contracts are correlated risk.
- **If it fails,** it is a no-go. We don't ship.
