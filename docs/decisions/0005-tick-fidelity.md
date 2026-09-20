# 0005 — Tick fidelity safeguard + pre-full-collection items

Status: accepted · Phase 1 · additive to 0004

## Context

An independent review re-listed the three round-3 fixes (exact pairing, strict
default, bar-path integrity) — all already implemented and passing (see 0004,
12/12 acceptance). A fresh read of the tick path found a gap the review did not
cover, and it is the most dangerous kind: **flaky data that passes every
validator check.**

MFE, MAE, `minutes_to_mfe/mae`, `mae_before_mfe`, and the horizon deltas are all
derived from the added 1-tick series (`OnBarUpdate`, `BarsInProgress == 1`). If
NinjaTrader cannot obtain true historical ticks for a date — the feed lacks tick
history, or Tick Replay is off — it synthesizes a few intra-bar prices from the
bar's OHLC. The magnitudes stay bounded by the bar high/low (so they look
plausible) but the **ordering and timing** of the excursions become artifacts of
the synthesis. The CSV looks clean and strict validation passes, yet the timing
labels the exit model would learn from are fiction. Nothing recorded exposed it.

## Decision

Make tick fidelity **visible in the data and checkable**:

- The logger now records `tick_updates` per observation (count of in-window tick
  updates that fed the labels). Written as the last signals column; added to
  `schema.SIGNALS_COLUMNS` (now 82).
- The validator errors on any `window_complete` observation with
  `tick_updates == 0` (labels were fabricated — the tick series never fed it) and
  reports median tick density so an analyst can spot OHLC-synthesized history
  (uniform, tiny counts) versus real ticks.
- The runbook now requires **Tick Replay** on the pilot chart and adds a Step 7
  check that `tick_updates` is large and varied. This makes the fidelity question
  answerable from the data itself, independent of NinjaTrader-internal behavior.

Acceptance: `scripts/mutation_tests.py` gains a case — window_complete rows with
zero tick updates must exit non-zero. 12/12 pass.

## Runtime items only verifiable on the trading machine (pilot)

- Tick Replay is on **and** the feed actually has tick history for the range
  (confirmed empirically via `tick_updates`, not just the checkbox).
- `regime.NormalizedSlopeDecline` resolves to the real property (compiles behind
  a try/catch; confirm the value isn't silently blank).
- `timezone_id` resolves as expected (0930 logger vs 0830 classifier).
- Removing the indicator fires `State.Terminated`, so the metadata flips to
  `completion_status: completed`.

## Must be settled before the FULL historical run (safe to defer past the 5-day pilot)

- **DST across the historical range.** RTH tagging and the 0930/1600 vs 0830/1500
  sessions are wall-clock. A multi-month pull crosses daylight-saving changes; a
  fall-back also repeats an hour, which the duplicate-bar check would flag. The
  5-day pilot almost certainly sits in one DST regime, so this does not block it,
  but the full run needs the session/timezone basis pinned (record UTC offset, or
  confirm the session template handles DST) before collection.
- **Contract roll.** A full historical range spans multiple front-month contracts
  (e.g. NQ 09-26 → 12-26). Decide whether to collect per-contract and how the
  roll is handled, so `instrument` identity stays clean across the boundary.
- **Tick-history depth vs Databento.** If the feed's tick history is shallow,
  decide NinjaTrader-only vs a Databento backfill — using the pilot's reported
  earliest-tick date and `tick_updates` density to judge.

## Consequence

The pilot is now defensible: if the tick feed is inadequate, the data says so
(`tick_updates`), rather than silently poisoning the model. The DST, roll, and
depth items are explicitly parked until after the pilot but before the full run.
