# 0012 — Data-gap handling (hole-aware logger + validator)

Status: accepted · Phase 1/2 · triggered by the 60-day continuous-merge gate

## Context

The 60-day continuous-contract gate (NQ DEC26, Merge Back Adjusted) validated the
merge itself (dense front-month data, smooth across the Sept roll, single
instrument) but exposed a **real feed hole**: no tick data from 2026-09-02 10:15
to 2026-09-06 17:00 (~2.5 trading days — Sept 2 pm, Sept 3, Sept 4 absent). The
chart showed candles there (minute data backfilled by the server) but Tick Replay
had no ticks, so the tick-driven logger captured nothing. A reload reproduced the
hole byte-for-byte across two runs → the data is genuinely not on the feed for
that span (most likely the recording machine was off over the Labor Day long
weekend and the feed does not backfill historical ticks).

Two distinct problems surfaced:

1. **Silent mislabeling (logger).** A window whose forward span runs into a
   no-tick period is finalized `window_complete` with understated MFE/MAE, not
   censored. When ticks resume, the first post-gap tick trips the
   `tickTime > WindowEnd` boundary and the window is written as a completed,
   uncensored observation — but it only ever saw data up to the gap. 202 signals
   at the hole's leading edge were affected.
2. **False fabrication errors (validator).** `check_final_price_range` and
   `check_join_parity` cross-check logged labels against the bars file. Where the
   covering bars don't exist (the hole), the lookup jumps across the gap and
   produces garbage envelopes → the row is flagged as fabricated/inflated. A
   separate, benign case also tripped the checks: at a violent close (Aug 21) the
   Tick-Replay path printed ~5 ticks beyond the 15s bar OHLC — a real
   reconstruction difference, not corruption.

## Decisions

### Logger — censor windows that run into a gap
New property `MaxDataGapSeconds` (default **300**). At window finalization, if the
last in-window tick is more than `MaxDataGapSeconds` before the window end, the
window is right-censored with `finalize_reason = "data_gap"` instead of
`window_complete`. This treats feed holes, the daily maintenance halt, and
weekends uniformly as right-censoring (the training pipeline is already
censoring-aware). Recorded in meta as `max_data_gap_seconds`. RTH-close cuts
still take precedence (`rth_close`).

### Validator — distinguish holes & tick/bar divergence from fabrication
- **`data_gap`** added to `VALID_FINALIZE_REASONS`.
- **Missing-covering-bar skip.** In both cross-checks, if the nearest bar on/after
  `window_end_actual` is more than ~1.5 bar-periods away, the covering bar was
  never written (a hole) — the row is skipped and counted, not failed. (Tolerance
  inferred from the bars' own cadence via `_bar_gap_tol`, not hard-wired to 15s.)
- **`check_data_holes`** (new): scans bar coverage, reports each abnormal gap
  (excluding the normal ~16:00 maintenance halt and the weekend) as a WARNING with
  its time span and the count of signal windows overlapping it.
- **Range-scaled divergence tolerance.** The final-price and join-parity tolerances
  scale with the local bar range — floor 2/1 ticks, **cap 12 ticks** — so the
  few-tick Tick-Replay-vs-OHLC divergence at violent bars passes while gross
  fabrication (the suite's +1000-tick mutations) still fails.

## Result

- 60-day gate now **PASSES `--pilot`: 0 errors**, with the Sept 2–6 hole reported
  as a loud warning (102.8h, 202 signals) plus the benign Sept 7 holiday
  early-close (5.0h, 0 signals).
- Mutation/acceptance suite **41/41** — fabrication detection intact (1000-tick
  MFE/MAE inflation and fabricated final price all still caught).

## Implications / follow-ups (not blockers)

- **Tick depth is the open question for 365.** The feed serves ≥60 days of ticks
  (back to Aug 9) with one hole. Whether it can serve a clean 365 is unverified —
  many feeds cap historical *tick* retention and rely on local recording for the
  rest. Verify true tick depth before the full pull; if short, import deep history
  (e.g. Databento) into NinjaTrader and run the logger over it so NT still computes
  the native features.
- The `data_gap` reclassification will modestly raise the right-censored rate
  (overnight windows crossing the nightly maintenance halt now censor correctly).
