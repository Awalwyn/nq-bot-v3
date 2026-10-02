# 0007 — Hardening round 5 (pilot-grade)

Status: accepted · Phase 1 · additive to 0004–0006

## Context

A re-test reported that all 14 corruptions still passed — but it was run against
an older build (its acceptance suite showed 12 cases; the current suite is 32).
The 14 checks were already present in the build under 0006. This round adds the
genuinely new requirements from the latest review on top of that, and fixes the
pilot runbook sequencing. Acceptance is now **32/32**, clean data still passes in
both strict and the new `--pilot` mode.

## Logger

- **Completion only on clean shutdown.** `State.Terminated` now finalizes, then
  flushes/closes (recording close errors), and sets `completion_status` to
  `completed` only if `signal_write_errors == bar_write_errors ==
  close_write_errors == 0`; otherwise `failed`. Adds `termination_reason`.
- **Total tick count** (`tick_count`) recorded in metadata.
- **VWAP no longer carries a stale prior-session value**: `CurrentVwap` returns
  NaN outside RTH, so `dist_vwap_ticks` is blank overnight rather than inheriting
  the last RTH VWAP.
- **Full config in metadata**: write-bars/show-markers, all composite-filter
  toggles, ADX/regime thresholds, kernel settings (`kernel_h/r/x/lag`,
  smoothing), neighbors/max-bars-back, created/completed timestamps, first/last
  bar and tick times.

## Validator (new checks, each with a mutation test)

- Metadata **count reconciliation** (signal & bar) and `close_write_errors == 0`.
- **Infinite / non-numeric** values in bar and signal numeric fields.
- **`--pilot` mode**: strict + **exhaustive** parity (no sampling) + **locked
  config** (`schema.PILOT_CONFIG`: instrument_master NQ, tick 0.25, 15s, score
  4–8, 60-min window, RTH-cut, all-sessions, bars-file) + a documented **tick
  density floor** (`PILOT_MIN_TICKS_PER_MIN`).
- Excursion times bounded by **both** the covered window and the forward window;
  `window_end_actual` must not precede `signal_time`.
- `window_complete` must have `right_censored == 0` (now an error).
- Signal and its **signal bar agree on session** tag/date; RTH 15s **cadence**
  gap detection (interior gaps flagged; exchange breaks allowed).
- **Tick coverage** must span the bar range; total tick count surfaced.
- (Carried from 0006) counts/timeframe/volume/OHLC/MFE-MAE two-sided/final
  delta/final price/direction/finalize-reason/session/horizon/`mae_before_mfe`.

## Runbook

Rebuilt to the required order: install → compile → **select trading-hours
template + confirm timezone** → 15s chart + Tick Replay → confirm tick history →
apply locked settings → let history finish → **remove indicator** → confirm
`completed` + zero write errors + matching counts → **isolated folder** with the
run_id-matched trio → **strict `--pilot` validation** → **10-signal manual
parity** → send → **wait for approval** → only then full collection. Removed the
validate-before-remove contradiction, the auto-jump to full collection, the
duplicate tick bullets, the hard-coded suite count, and the unsupported
0930-ET/0830-CT equivalence assumption.

## Open — still needs a decision (not a bug)

- **VWAP definition.** Now blank outside RTH (safe default). If the desk wants a
  full-Globex/session VWAP or both RTH and session VWAP recorded, specify it.
- **`session_date` convention.** Uses NinjaTrader's trading-day
  (`GetTradingDayFromLocal`); confirm on the pilot, or we rename to
  `calendar_date` + add `trading_date`.

## Two lifecycle cases only NinjaTrader can exercise

`runInitialized`/`writersOpened` guard (terminated before writers opened) and a
real writer-close failure can't be exercised from Python; the validator instead
rejects any run whose metadata is not `completed` or shows close errors, which is
the observable consequence. The guard and error-surfacing are in the logger.
