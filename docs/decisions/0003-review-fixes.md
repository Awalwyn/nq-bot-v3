# 0003 — Data-integrity review fixes (pre-collection)

Status: accepted
Date: 2024-06 (rev 2 of the signal logger)
Context: peer review of Phase 1 Part 1 before large-scale historical collection.

The review did not change the Phase 1 architecture (broad/raw collection). It
tightened data semantics so the collected dataset is trustworthy. Eleven issues,
all confirmed against source, all fixed.

## Decisions

1. **Forward-window boundary is checked first.** In the 1-tick loop, a tick
   strictly after `WindowEnd` finalizes the observation using the last in-window
   price and does **not** update MFE / MAE / horizon deltas / final delta. Only
   prices at or before `WindowEnd` affect labels. Proven by `scripts/boundary_demo.py`.

2. **`mae_before_mfe` = time_of_final_MAE < time_of_final_MFE**, computed at
   finalize from `minutes_to_mae` / `minutes_to_mfe`. A non-occurring excursion
   is treated as +infinity; if neither occurred the value is 0 (false).

3. **Stateful features advance on every bar.** VWAP, regime, candle-run and the
   kernel recurrence update from the start of available data. The ML warmup guard
   gates only signal-observation creation. RTH VWAP resets at the RTH open.

4. **Contract-aware identity.** Full contract (`Instrument.FullName`, e.g.
   `NQ 12-26`), master, expiry and tick size are stored. `signal_id` uses the full
   contract so different contracts cannot collide. `instrument`, `instrument_master`,
   `expiry`, `tick_size` are columns on both files.

5. **Signals/bars pairing.** The validator requires matching `run_id`,
   `instrument`, `bar_period` and `tick_size` across the two files, else it FAILS.
   Bars are located by matching `run_id`, not "newest".

6. **Parity window starts after the signal bar.** Reconstruction from the bars
   file begins at the first bar strictly after the signal bar's close (`side='right'`)
   and ends at `window_end_actual`, using each row's `tick_size`. The signal bar's
   own intrabar high/low no longer produces false parity errors.

7. **`LogAllSessions` gates creation.** `false` = log only RTH signals; `true` =
   log and tag all sessions (default). The bars file always logs every bar.

8. **Session/timezone recorded, not silently changed.** `timezone_id`
   (`Bars.TradingHours.TimeZoneInfo.Id`), the logger RTH window and the classifier
   session (830–1500) are written to run metadata. The classifier session params are
   **not** changed blindly, because they feed the entry score that must match live;
   the ET/CT question is flagged for runtime verification against the metadata.

9. **Validator is config-aware.** It reads `<run_id>.meta.json` and validates the
   score band and window against the settings actually used for the run.

11. **Censored metadata.** Each observation stores `window_end_scheduled`,
   `window_end_actual`, `final_price`, `right_censored`, and a distinct
   `finalize_reason` (`window_complete` / `rth_close` / `terminated`).

## Artifacts
- `ninjascript/SignalLogger.cs` (rev 2), `data_pipeline/schema.py`, `data_pipeline/validate.py`
- `<run_id>.meta.json` run metadata (new)
- `scripts/boundary_demo.py` — executable boundary proof
- `scripts/make_sample_data.py` — emits all three finalize reasons + meta

## Runtime items to verify on the ThinkPad
- Confirm `RegimeFilterState.NormalizedSlopeDecline` is the real member name.
- Confirm `timezone_id` in metadata matches expectation and reconcile the
  logger RTH (930–1600) with the classifier session (830–1500).
