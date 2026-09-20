# Phase 1 — Status & Handoff

_Last updated: 16 September 2026_

## The short version

Phase 1 **code** is built, frozen, and fully tested. The remaining Phase 1 work
is **running the logger in NinjaTrader to collect and strictly validate a real
NQ dataset** — that's Matthew's pilot. Everything validated so far is synthetic:
it proves the code is correct, not that we have data. **Phase 2 (model training)
cannot truly begin until the real dataset exists.**

## Where Phase 1 stands

| Item | Owner | Status |
|---|---|---|
| Signal logger + all data-integrity fixes | dev | Complete (frozen) |
| Validator — strict, rev 3 | dev | Complete |
| Mutation / acceptance suite | dev | 11/11 passing |
| Boundary proof (no post-window leakage) | dev | Passed |
| Synthetic validation | dev | Passed, 0 errors |
| NinjaTrader compile | Matthew | Pending |
| 5-day pilot collection | Matthew | Pending |
| Strict validation of real pilot data | awalwyn | Pending |
| Timezone / session confirmation | Matthew | Pending |
| Full historical collection | Matthew | Blocked on pilot |
| Phase 2 — model training | awalwyn | Blocked on real data |

## What Matthew does now

1. Install the exact `ninjascript/SignalLogger.cs` from this package into
   `Documents\NinjaTrader 8\bin\Custom\Indicators`.
2. Compile (F5), confirm 0 errors.
3. Build a 15-second front-month NQ chart, 5 days loaded.
4. Apply `SignalLogger` with the locked Phase 1 settings.
5. Confirm the three output files; sanity-check in Excel/Notepad.
6. Zip the three files and send them to awalwyn.
7. Report: earliest data date, signal row count, `timezone_id`.

Full click-by-click is in `Matthew_Phase1_Runbook.docx`. This is the summary.

## Four things only Matthew can verify (need the live runtime)

- **Tick Replay is on and the feed has real tick history** — confirm the signals
  file's `tick_updates` column is large and varied, not 0 or a tiny constant.
  This is what makes the timing labels (MFE/MAE/horizons) trustworthy.

- `regime.NormalizedSlopeDecline` resolves to the real property (it compiles
  behind a try/catch; confirm the value isn't silently blank).
- `timezone_id` resolves as expected — the 0930 (logger) vs 0830 (classifier)
  question. Read it from the `.meta.json`.
- Removing the indicator fires a clean `State.Terminated`, so the metadata flips
  to `completion_status: completed`. Check the meta says `completed` afterward.

## Approval gate — Phase 1 is complete when

- SignalLogger compiles with 0 errors.
- The three files share one `run_id` and one full contract identity.
- Strict validation returns 0 errors.
- `tick_updates` confirms real ticks fed the labels (not synthesized).
- `tick_updates` confirms real ticks fed the labels (not synthesized).
- `tick_updates` confirms real ticks fed the labels (not synthesized).
- The metadata timezone and the 0930–1600 logger session are confirmed against
  NinjaTrader time values.
- The classifier's 0830–1500 is confirmed as the intended market hours.
- A manual sample of signals matches the chart (time, direction, score,
  reference close, MFE, MAE, horizons, censoring).
- No post-window price affects any label.
- The run reports clean completion and zero write errors.

## After the pilot

Same settings, full history range, clean close, strict validation. If the full
dataset is too large to email, use a shared drive or transfer service. **Phase 1
is complete only after the full dataset passes strict validation.**

## Meanwhile (awalwyn)

The Phase 2 skeleton already exists and is tested on synthetic data: time-based
split with embargo, censoring-aware labels, the XGBoost training loop, and ONNX
export with a parity check. It can be reviewed or hardened now, but training is
not meaningful until the pilot data lands.
