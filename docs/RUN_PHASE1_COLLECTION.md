# Phase 1 — 5-Day Pilot Runbook

Authoritative pilot procedure. Run the steps in this exact order. The run is only
finalized (metadata flips to `completed`) when the indicator is **removed**, so
validation happens **after** removal, not before.

> A formatted operator version is delivered separately as
> `Matthew_Phase1_Runbook.docx`; this file is the source of truth.

## Session / timezone (decided)

The entry model runs **08:30–15:00 Central Time**. The chart timezone is
**Central**, so the logger RTH is **0830–1500** (matching the classifier) and the
Trading Hours template is **CME US Index Futures ETH**. These are pinned in code
and enforced by `--pilot`.

- Logger RTH: `RthStart=830`, `RthEnd=1500`
- Classifier session: `830–1500` (unchanged — already correct)
- `schema.PILOT_CONFIG` and `EXPECTED_TRADING_HOURS_TEMPLATE` enforce the above.

## Definition of done (pilot)

1. `SignalLogger.cs` compiles with 0 errors.
2. A 5-day run produces the three matching files; metadata reads
   `completion_status: completed` with zero write/close errors.
3. Strict **`--pilot`** validation passes (0 errors).
4. Manual parity of ≥10 signals against the chart/classifier looks right.
5. Pilot is **approved** before any full-history collection.

## Steps

1. **Install the final logger.** Copy `ninjascript/SignalLogger.cs` into
   `Documents\NinjaTrader 8\bin\Custom\Indicators`.
2. **Compile** (NinjaScript Editor → F5). Errors tab must read 0.
3. **Set the chart to Central Time** and select Trading Hours template
   **CME US Index Futures ETH** on the Data Series.
4. **Create the 15-second NQ chart** — front-month NQ, bar type = 15 **Second**,
   **Tick Replay** enabled (Tools → Options → Market Data → Show Tick Replay,
   then check it on the Data Series).
5. **Confirm historical tick availability** (temporarily load 365 days; note the
   earliest continuous tick date), then set Days to load = 5.
6. **Apply `SignalLogger`** with the locked settings: Min|Score| 4, Max|Score| 8,
   Forward Window 60, Cut At RTH Close true, Log All Sessions true,
   **RTH 830 / 1500**, Output `C:\ProgramData\nqbotv3\data\training`, Write Bars File true.
   ML/filters at default.
7. **Let historical processing finish** (markers stop appearing).
8. **Remove `SignalLogger`** from the chart. This finalizes the run and writes
   `completion_status: completed`.
9. **Confirm metadata says completed** — open `run_<run_id>.meta.json`; check
   `completion_status: completed`, all write-error counts 0, counts sane, and
   `trading_hours_template` = `CME US Index Futures ETH`, `rth_start` 830,
   `rth_end` 1500.
10. **Isolate the three matching files** — a fresh empty folder containing only
    `signals_<run_id>.csv`, `bars_<run_id>.csv`, `run_<run_id>.meta.json`
    (identify the set by the shared `run_id`).
11. **Run strict pilot validation:**
    ```
    uv run python -m data_pipeline.validate --dir "PATH_TO_PILOT_FILES" --pilot
    ```
    The template is pinned in code; pass `--expect-template "CME US Index Futures ETH"`
    to override. Must report `PASSED — 0 error(s)`.
12. **Manually compare at least 10 signals** against the chart/classifier: time,
    direction, score, reference close, MFE/MAE, horizons, censoring reason.
13. **Send the pilot** (isolated folder + validator output) for approval.
14. **Wait for approval.** Do not begin full history until approved.
15. **Only then, full-history collection** — same settings, same
    remove-then-validate sequence, Days to load raised to the confirmed history.

## Output folder note

The output folder is **`C:\ProgramData\nqbotv3\data\training`** by default. `ProgramData`
is deliberately chosen: it always exists on Windows, is writable without admin
rights, and is **never synced by OneDrive** — unlike `Documents`, `Desktop`, or the
user profile, which OneDrive redirects and which caused the earlier "markers show
but no files" problem.

If no files appear after a run with markers showing, the writer could not create the
path. Check **New → NinjaScript Output** for lines starting `SignalLogger:` — it
prints the exact path and any open-writer error. Fixes, in order of preference:
pre-create the folder in Explorer (`C:\ProgramData\nqbotv3\data\training`), or set
Output Folder to another non-synced local path such as `C:\nqbotdata\training`.
Do **not** point it at `Documents`/`Desktop` — those are OneDrive-synced.

Note: `ProgramData` is hidden by default in Explorer. Paste the full path into the
address bar, or enable View → Hidden items, to see it.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Compile error on `NormalizedSlopeDecline` | Send the exact text; one-line fix. |
| Markers show but no files | Output-folder write failed — see the note above; read NinjaScript Output. |
| `completion_status: running` | You validated before removing the indicator — remove it (step 8). |
| `completion_status: failed` | A writer failed to flush/close. Check write-error counts; do not use that run. |
| `tick_updates` all 0 / tiny | Tick Replay off or no tick history (steps 4–5). |
