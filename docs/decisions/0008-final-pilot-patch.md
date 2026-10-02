# 0008 — Final pilot patch (round 6)

Status: accepted · Phase 1 · additive to 0004–0007

## Context

The reviewer confirmed the round-5 build on the correct copy: 32/32 suite, clean
`--pilot` passes, all corruptions rejected. Remaining items were a NinjaTrader
compile blocker plus packaging/lock refinements. All addressed; suite now 40/40.

## Fixes

1. **Compile blocker — session date.** Replaced the non-existent
   `Bars.TradingHours.GetTradingDayFromLocal(t)` with a stored `SessionIterator`
   (`new SessionIterator(BarsArray[0])`, `sessionIterator.GetTradingDay(t)`),
   initialized in `State.DataLoaded`. This is the documented NinjaTrader API for
   the trading-day of a timestamp.

2. **Pilot config fully locked.** `schema.PILOT_CONFIG` now also pins `rth_start`,
   `rth_end`, `classifier_session_start`, `classifier_session_end`,
   `neighbors_count`, `max_bars_back`. The Trading Hours template is enforced via
   `--expect-template "<name>"` (or `EXPECTED_TRADING_HOURS_TEMPLATE`); `--pilot`
   fails if the template is not pinned, so an official pilot can't pass without it
   being chosen. Each locked value has a mutation test that fails `--pilot`.

3. **Clean `--pilot` acceptance test.** The suite now asserts the clean synthetic
   dataset passes `--pilot` (exit 0), so a future change can't silently make pilot
   mode reject healthy data.

4. **Partial writer-init cleanup.** If the second writer fails to open, the catch
   now closes any partially-opened writer, sets `completion_status = failed` and
   `termination_reason = init_failed`, prints the full error, and leaves
   `writersOpened = false`.

5. **Runbook corrected (repo markdown).** `docs/RUN_PHASE1_COLLECTION.md` rewritten
   to the required order: let the run process → remove the indicator → confirm
   `completed` → isolate the three run_id-matched files → run
   `--pilot --expect-template` → 10-signal manual parity → send for approval →
   only then full history. Removed validate-before-remove, the auto-jump to full
   collection, and the unsupported 0930-ET/0830-CT equivalence; added the template
   lock and the exact pilot command. The .docx operator guide matches.

## Still a decision, not a bug

- The exact Trading Hours template name and the clock basis (chart/ET/CT and the
  matching RTH numbers) must be chosen before the official pilot. The validator
  enforces them once `--expect-template` is supplied; the runbook says so.
- VWAP definition overnight (currently blank outside RTH) — confirm or specify.

## Acceptance

`scripts/mutation_tests.py`: 40/40. Clean dataset passes strict and `--pilot`
(with the template pinned); every corruption and every unlocked-config mutation
exits non-zero; boundary demo passes.
