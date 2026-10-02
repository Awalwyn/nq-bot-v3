# 0006 — Validator + logger hardening, round 4

Status: accepted · Phase 1 · additive to 0004 / 0005

## Context

A further adversarial review (mutation testing) found corruptions that passed
validation, plus metadata/lifecycle gaps in the logger. All are now addressed,
each with a dedicated case in `scripts/mutation_tests.py` (25/25 passing; clean
data still validates with 0 errors / 0 warnings).

## Validator — new checks (each corruption now exits non-zero)

| Corruption | Check |
|---|---|
| metadata counts don't match files / deleted bar row | `check_meta_counts` reconciles `signal_count`/`bar_count` vs actual rows; `close_write_errors == 0` |
| wrong 15-minute timeframe | `check_bar_period` requires `bar_period == "15s"` in both files and metadata |
| non-numeric volume (and O/H/L/C, tick_size) | `check_bar_integrity` flags values that coerce to NaN from non-null input |
| MFE/MAE inflated | `check_join_parity` is now two-sided: logged excursions may not exceed the bar range by >1 tick |
| fabricated final delta | `check_final_delta`: `final_delta_ticks == (final_price − reference)/tick × direction` |
| negative minutes_to_mfe/mae | `check_minutes_nonneg` |
| invalid finalize_reason | `check_finalize_reason` against the allowed set |
| reversed direction text | `check_direction` validates text and consistency with `is_long` |
| falsified session tags | `check_session_tag` recomputes RTH/OVERNIGHT from `signal_time` vs the run's RTH window |
| falsified session dates | `check_session_date` requires `session_date` within [signal date, +1 trading day] |
| flipped mae_before_mfe | `check_mae_before_mfe` recomputes from `minutes_to_mae < minutes_to_mfe` (+∞ rules) |
| future horizons after censoring | `check_horizons_censoring`: a horizon minute past the covered window must be null |
| fabricated final price | `check_final_price_range`: `final_price` within the bar range at `window_end_actual` |

## Logger — lifecycle & metadata

- **Init guard in `State.Terminated`**: if setup never completed (`writersOpened`
  false), the handler returns without finalizing or writing a "completed" meta.
- **Flush/close exceptions surfaced**: `CloseWriters` counts failures into
  `close_write_errors` and prints them (was an empty catch). `CloseWriters` now
  runs before the final `WriteMeta`, so the count lands in the metadata.
- **`created_utc` captured once** at open and preserved on rewrite; **`completed_utc`**
  is a distinct timestamp set only on clean termination.
- **Metadata enriched**: `trading_hours_template`, `neighbors_count`,
  `max_bars_back`, `first_bar_time`/`last_bar_time`, `first_tick_time`/
  `last_tick_time`, `close_write_errors`. Logger tag bumped to rev3.
- **`session_date` now uses the trading-day convention** (`TradingHours.
  GetTradingDayFromLocal`), so overnight/Globex bars roll to the next RTH
  session's date instead of the calendar date.

## Open — needs Matthew's spec, not a guess

- **Overnight VWAP semantics.** VWAP is currently RTH-anchored: it resets at the
  RTH open and accumulates only during RTH, so `dist_vwap_ticks` is null overnight
  before the first RTH bar. This is a defined behavior, not a bug — but "what VWAP
  should be overnight" is a modeling decision. Confirm the intended definition
  (RTH-only as now, full-Globex-session, or rolling) and it'll be implemented to
  spec rather than changed blind.

## Runtime verification (trading machine only)

- `session_date` trading-day rolling relies on `TradingHours.GetTradingDayFromLocal`;
  confirm it resolves on the pilot data (spot-check an overnight bar's session_date).
- `trading_hours_template` records `Bars.TradingHours.Name`; confirm it's the
  template the desk intends.
