# 0009 — Central-time session correction

Status: accepted · Phase 1 · config alignment (no schema/label/feature change)

## Context

The entry model operates **08:30–15:00 Central Time**. The chart timezone is
Central. The logger's RTH default was 09:30–16:00, which is one hour late under a
Central chart — it would drop the intended open and include an hour past the
intended close. The classifier session was already 08:30–15:00, so the logger and
validator are brought into alignment with it.

## Change (configuration only)

- **Logger** `SignalLogger.cs`: `RthStart 930 → 830`, `RthEnd 1600 → 1500`.
- **schema.py** `PILOT_CONFIG`: `rth_start 930 → 830`, `rth_end 1600 → 1500`;
  `EXPECTED_TRADING_HOURS_TEMPLATE = "CME US Index Futures ETH"` (was None).
- **Sample generator / tests**: RTH constants and the RTH-close cutoff derive from
  830/1500; the pinned template is `CME US Index Futures ETH`.
- **Runbook** (repo `.md` + operator `.docx`): timezone decided (Central), RTH
  0830–1500, template `CME US Index Futures ETH`; removed the "windows may be
  equivalent" language.

## Not changed

Classifier session (already 08:30–15:00), feature calculations, labels, ML/filter
logic, and the CSV/metadata schema are untouched.

## Acceptance

- Clean synthetic dataset passes strict and `--pilot` (0 errors), with
  `rth_start=830`, `rth_end=1500`, template `CME US Index Futures ETH`.
- Wrong RTH values or a wrong Trading Hours template are rejected by `--pilot`.
- Full suite 40/40; boundary demo passes.

## Handoff

Configuration-alignment only. After the code, validator expectations, and tests
agree on 08:30–15:00 CT, the build is ready for the NinjaTrader pilot run.
