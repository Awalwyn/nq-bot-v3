# 0013 — Phase 2 cleaning, label extension, and the locked holdout

Status: accepted · Phase 2 step 1 · 11 Oct 2026

## Context

The full-year run (run_20261011T220004Z_0ff4b383) needs cleaning before any
modeling. The handoff listed ~1,000 glitch rows on 4 days, found by inspection.
Three operating decisions also arrived:
- train on both sessions, go live on RTH only
- the bot does **not** force-close at 15:00
- max 7 contracts

## Decisions

1. **Integrity by cross-source agreement, not by outcome size.** MFE/MAE are
   rebuilt from the bars file over exactly the span the logger observed and
   compared with the tick labels.
   - After correcting for the close-stamped final bar, 99.86% of entries agree
     within 1 tick.
   - Rows disagreeing by > 4 ticks are dropped.
   - The rule finds the 4 known glitch days and 8 more on its own, with no
     hand-picked dates.
2. **Session-tail quarantine** for gross failures (> 12 ticks). A corrupt print
   can leak into later features, so everything from the first window that could
   touch it to session end is dropped.
3. **Whole-cluster drop** when a cluster's first signal fails. Otherwise
   `entries.py` would promote a later signal into a fake entry.
4. **Extend 15:00-cut RTH windows to 60 min from bars.** With no force-close the
   cut is not the real outcome. Treating those rows as censored or dropping them
   would delete or bias every 14:00–15:00 RTH entry (15% of RTH entries; mean
   MFE 160 cut vs 214 true).
   - Validated on 20,000 uncensored rows: ≥99.6% agreement on every label.
   - Logger conventions confirmed along the way:
     - `final_price` is the last in-window tick, i.e. the close of the bar
       stamped at window end.
     - Horizon deltas use the first tick after the boundary, i.e. the open of
       the next bar.
5. **Locked holdout split at clean time** (sessions ≥ 2026-07-01, embargo
   2026-06-30). It is a separate file, and `dataset.load_clean()` refuses it
   without `allow_holdout=True`.
6. **Feature list tightened** (`schema.EXCLUDED_FEATURES`):
   - removed absolute price levels, VWAP distance and raw volume
   - added score, is_long and is_rth

## Consequences

- **Dev:** 143,446 entries. **Holdout:** 55,551 entries. About 1.6% of entries
  were lost to integrity drops and the embargo.
- `scripts/check_clean.py` asserts the separation and hygiene guarantees (19 checks).
- **Still to fix in later steps:**
  - `train.py` eval_set = test
  - `analyze.py` running on the full year
  - the evaluator needs replay past 60 min plus the live position rules
