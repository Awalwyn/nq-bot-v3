# 0010 — Close-stamped bar convention, boundary tolerances, timezone lock

Status: accepted · Phase 1 · triggered by the first REAL 5-day pilot

## Context

Matthew ran the first real 5-day pilot (NQ DEC26, Central time, ETH template):
16,587 signals, 21,690 bars, 2.39M ticks, clean shutdown, zero write errors.
Running it through the validator exposed a **real validator bug the synthetic
suite could not** — plus the timezone-enforcement gap Matthew flagged.

## The validator bug (bar timestamp convention)

The validator reconstructed the price path assuming 15-second bars are stamped at
their interval **start**. NinjaTrader stamps each bar at its interval **close**
(`:00/:15/:30/:45`). Proof from the pilot: of the decisive final-price cases,
194/194 matched the close-stamped bar and 0 matched the start-stamped assumption.
The synthetic generator happened to start-stamp its bars **and** place every
signal/window boundary exactly on the bar grid, where the two conventions
coincide — so the suite passed 40/40 while real, off-grid tick data failed.

Fix: the bar that CONTAINS a tick time `t` is the first bar whose timestamp
`>= t` (searchsorted side="left"). Applied to `check_final_price_range`,
`check_window_coverage`, and `check_join_parity`. On exact-grid synthetic data
this is identical to the old logic, so the suite is unaffected.

## Boundary tolerances (benign real-data edge cases, not corruption)

Three legitimate boundary conditions were made explicit so real tick data passes
without weakening corruption detection:

1. **Exact-grid tick assignment.** A tick exactly on a 15s boundary can belong to
   the adjacent bar, so `final_price` is accepted within the containing bar **or
   its immediate neighbours**, and join-parity uses a two-envelope test:
   - *inner* = bars fully within `(signal_time, wea]` (the logger saw every tick)
     → bars here must not reveal MORE excursion than logged (understating bug).
   - *outer* = inner + the partial/containing bar + one neighbour
     → logged must not exceed this (inflation / fabrication). A +1000-tick
       fabrication is still rejected; the suite's inflation tests still pass.
2. **End-of-run truncation.** The run can stop mid-interval, so the final tick
   lands after the last WRITTEN bar. Rows whose `window_end_actual` is after the
   last bar are skipped by the bar-based checks (the covering bar doesn't exist)
   and the count is reported.
3. **Zero-length windows.** A signal firing exactly at a session/maintenance
   boundary (e.g. 16:00:00 daily halt) gets `window_end_actual == signal_time`,
   0 ticks, mfe=mae=0. These carry no label signal; they are now a WARNING (and
   excluded from the dead-tick / coverage checks), not an error. The logger could
   optionally censor them; 2/16,587 in the pilot, no training impact.

## Timezone lock (Matthew's request)

- `--pilot` now rejects a wrong `timezone_id`. Expected value
  `schema.EXPECTED_TIMEZONE_ID = "Central Standard Time"` (confirmed from the real
  pilot), overridable with `--expect-timezone`. A wrong chart timezone shifts the
  whole session an hour even when the RTH integers look right.
- Stale `930/1600` fallbacks in `check_session_tag` and `check_rth_cadence`
  corrected to `830/1500`.
- Synthetic generator `timezone_id` corrected from `US Eastern Standard Time` to
  `Central Standard Time`.
- New mutation test: a clean Central dataset passes `--pilot`; flipping only
  `timezone_id` to Eastern makes `--pilot` fail. Suite now 41/41.

## Result

The real pilot **PASSES exhaustive `--pilot` validation: 0 errors, 2 benign
zero-length-boundary warnings.** Full report: `pilot_validation_report.txt`.
This is the first real dataset to clear the gate.

## Follow-ups (not blockers)

- Logger could reclassify a signal firing exactly at a session boundary as
  censored rather than `window_complete` with a zero-length window.
- The 10-signal manual parity check (runbook step 13) is still required before
  final Phase 1 sign-off.
