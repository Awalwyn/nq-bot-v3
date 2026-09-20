# Phase 1 — Data Collection Run Guide

**Purpose:** produce the first real NQ dataset with `SignalLogger` (rev 2) and confirm it
passes validation, so Phase 1 is complete. Written to be followed live on a screen-share.

**Who runs what:** Matthew drives NinjaTrader on his machine (the ThinkPad is down).
Validation can run on either machine — see Part G.

**Definition of done (Phase 1):**
1. `signals_<run_id>.csv`, `bars_<run_id>.csv`, and `<run_id>.meta.json` written for a real run.
2. Validator prints `PASSED — 0 error(s)`.
3. Timezone confirmed from the meta file (review point #8).
4. Files shared back so we can move to Phase 2 (model).

---

## Part A — Prerequisites (check before the call)

Matthew already runs the full entry-model stack locally, so the dependencies are done. The only
**new** file in this whole process is `SignalLogger.cs`.

- [ ] **NinjaTrader 8** running and connected to a data feed with **historical tick data** for NQ.
      Tick history depth is what limits how far back we can collect — we check it in Part C.
- [ ] **Classifier stack already installed** (it is — this is the strategy Matthew already runs).
      `SignalLogger` just references the existing `MLLorentzianClassification`, `MLExtensionsLib`,
      `KernelFunctionsLib`, and `RegimeFilterState` types. Nothing to add.
- [ ] Get the current `ninjascript/SignalLogger.cs` from the ZIP awalwyn sent — that's the one file to drop in. Do not download from GitHub; that copy is not updated yet.

> The logger **creates** `C:\nqbotv3\data\training` automatically and writes with AutoFlush,
> so there's no folder-prep step and no risk of a half-written file.

---

## Part B — Install & compile the logger

1. Open the repo, go to `ninjascript/SignalLogger.cs`.
2. Copy it into NinjaTrader's custom indicators folder on Matthew's machine:
   `C:\Users\<Matthew>\Documents\NinjaTrader 8\bin\Custom\Indicators\SignalLogger.cs`
3. In NinjaTrader: **New → NinjaScript Editor**, then press **F5** (Compile).
4. Watch the **Errors** tab at the bottom.
   - **0 errors** → good, continue. (This is the expected case — the libs it references are
     already in his Custom folder from the entry model, so it compiles alongside them.)
   - An error on `regime.NormalizedSlopeDecline` → the one member name I inferred. Send me the
     real property on `RegimeFilterState` and I'll patch that single line (it's in a try/catch,
     but confirm it during the call).

---

## Part C — Verify historical tick-data depth (2-minute check)

This decides how much history we can collect. MFE/MAE accuracy comes from a 1-tick series, so
we need **tick** history, not just minute bars.

1. Control Center → **Tools → Historical Data** (or just open a chart and note how far back it loads).
2. Select the **NQ front-month contract** (whatever is current, e.g. `NQ 12-26`) and look at how
   far back **Tick** data is available.
3. Report the earliest tick date. That's our usable collection window.
   - Plenty of tick history → great, we can do a large pull.
   - Thin tick history (only weeks) → we still run Phase 1 on what's there; the Databento backfill
     is the contingency if we later need more depth. Don't buy anything speculatively.

---

## Part D — Create the 15-second NQ chart

1. **New → Chart.**
2. Add the instrument: the **NQ front-month contract** (must match what has tick data from Part C).
3. Set the bar type to **15 Second** (Bars period type = Second, value = 15). This is required —
   the whole dataset is defined on 15s bars.
4. **Enable Tick Replay** (critical for label fidelity). Globally: Tools → Options → Market Data →
   check "Show Tick Replay". Then on the chart's Data Series dialog, check **Tick Replay**. Without
   real historical ticks, NinjaTrader synthesizes intra-bar prices and the *timing* labels
   (`minutes_to_mfe/mae`, `mae_before_mfe`, horizon deltas) are wrong even though the file looks
   clean. The `tick_updates` column lets us verify this after the run (Part F / Part G).
5. **Days / bars to load:** for the **first smoke run set this small — 3 to 5 trading days.**
   We confirm everything works before doing the big pull. (We'll raise this in Part H.)
6. Load the chart.

---

## Part E — Apply SignalLogger and set parameters

1. On the chart: **right-click → Indicators…** (or the indicators toolbar button).
2. Find **SignalLogger**, double-click to add it.
3. Set the parameters. Defaults are already correct for our locked decisions — **confirm each
   group matches this**:

**Group "1. Collection"**

| Field | Value |
|---|---|
| Min \|Score\| To Log | **4** |
| Max \|Score\| To Log | **8** |
| Forward Window Minutes | **60** |
| Cut Window At RTH Close (right-censor) | **true** |
| Log All Sessions (false = RTH only) | **true** |
| RTH Start HHmm | **930** |
| RTH End HHmm | **1600** |

**Group "2. Output"**

| Field | Value |
|---|---|
| Output Folder | **C:\nqbotv3\data\training** |
| Write Bars File | **true** |
| Show Markers On Chart | **true** for the call *(so we can see signals firing live; can turn off later)* |

**Group "3. Composite Filter Config"** — leave all at defaults (these only affect the composite
filter flag; every filter is still logged individually regardless).

**Group "4. ML Source"** — leave defaults: ML Neighbors = 8, ML Max Bars Back = 2000.

4. Click **OK / Apply.**

The indicator processes the loaded history on apply. With "Show Markers" on, you'll see signal
marks appear where `|score|` is 4–8. Rows stream to the CSV as observations finalize.

---

## Part F — Confirm the run wrote files, and verify the timezone (review point #8)

1. Open **`C:\nqbotv3\data\training`**. You should see three files:
   - `signals_run_<timestamp>.csv`
   - `bars_run_<timestamp>.csv`
   - `run_<timestamp>.meta.json`
2. Open the **`.meta.json`** and read these fields out loud on the call:
   - `timezone_id` — e.g. `US Eastern Standard Time` or `Central Standard Time`
   - `rth_start` / `rth_end` (930 / 1600)
   - `classifier_session_start` / `classifier_session_end` (830 / 1500)
3. **The check:** our logger session (0930–1600) and the classifier session (0830–1500) are one
   hour apart. If `timezone_id` is **Eastern**, then 0930 ET and 0830 CT describe the **same**
   RTH open in two notations — consistent, nothing to fix. If it resolves to something that makes
   those two point at genuinely different hours, flag it and we adjust before the big pull.
4. Sanity-eyeball one bar: find a `bar_time` you know is the **9:30 ET cash open** and confirm the
   timestamp reads the way that timezone implies.

> We deliberately did **not** hard-edit the classifier's 0830–1500 to match, because those params
> feed the entry score. The meta file records whatever the machine actually resolves, so the dataset
> is reproducible either way — this step just tells us which case we're in.

---

## Part G — Validate the data

Run the validator against the output folder. Two options:

**Option 1 — on Matthew's machine** (if Python + uv are installed):
```
cd <repo>
uv sync
python -m data_pipeline.validate --dir "C:\nqbotv3\data\training"
```

**Option 2 — ship the three files to the Mac** and validate there:
```
cd "/Users/awalwyn/Documents/Github Projects/nq-bot-v3"
python3 -m data_pipeline.validate --dir /path/to/the/three/files
```

Expected tail:
```
PASSED — 0 error(s), 0 warning(s).
```
The validator reads the run's `.meta.json`, so it checks against the actual score band / window
used, confirms the signals and bars files are from the **same run**, and checks the censoring flags.

If it prints errors, copy the full output to me — the message names the exact check that failed.

---

## Part H — Full run, then hand off

Once the smoke run passes **and** the timezone is confirmed:

1. Remove the indicator (or open a fresh chart) to start a clean run with a new `run_id`.
2. Raise **Days to load** on the chart to as far back as tick data allows (Part C).
3. Re-apply `SignalLogger` with the same parameters and let the historical load finish.
4. Re-run the validator (Part G) on the new run's files.
5. **To close the run cleanly:** remove the indicator or close the chart — the writers flush and
   close on termination. (AutoFlush means the file is already complete, but this ends it tidily.)
6. Send back the three files (`signals_`, `bars_`, `.meta.json`) for the full run, plus the
   validator output.

That completes Phase 1. Next is Phase 2 (model training) on the Mac using this dataset.

---

## Quick troubleshooting

| Symptom | Cause / fix |
|---|---|
| Compile errors on `MLLorentzian…` / `Kernel…` / `MLExtensions…` | Unexpected here (libs already present). Means the entry-model files didn't compile — fix those first, then recompile `SignalLogger`. |
| Error on `regime.NormalizedSlopeDecline` | Send me the real property name on `RegimeFilterState`; one-line patch. |
| No files appear in the output folder | Indicator didn't apply, or an early error — check the NinjaScript **Output** window (New → NinjaScript Output) for `SignalLogger:` lines. |
| `signals` file has header only | `\|score\|` never hit 4–8 in the loaded range, or wrong bar type. Confirm 15-second bars and enough history. |
| MFE/MAE look coarse / stair-stepped | No historical **tick** data for that range — you're getting bar-resolution paths. Check Part C. |
| Validator: pairing FAIL | The signals and bars files are from different runs — use the matching pair with the same `run_id`. |
| Validator: score band error | Chart/param mismatch — the run's meta says one band but rows fall outside it. Send me the output. |
