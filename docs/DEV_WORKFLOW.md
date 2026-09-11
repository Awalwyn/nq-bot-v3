# NQ Bot v3 — Developer Workflow & File Map

**Who does what: the Mac authors, the ThinkPad runs.**

This doc explains every file we're building, which machine it belongs to, where it physically goes, and what you do with it. Read the "mental model" first — everything else follows from it.

---

## 1. The mental model (read this once, internalize it)

You have two machines with two completely different jobs. Do not blur them.

| | **Mac (your laptop)** | **ThinkPad (Windows box)** |
|---|---|---|
| **Role** | **Authoring / development** | **Execution / data production** |
| You do here | Write & edit code, run git, iterate, ask Claude | Compile & run NinjaTrader, produce data, run the Python pipeline |
| Runs | Text editor, git, (optionally) Python for exploration | **NinjaTrader 8**, the data logger, Python pipeline, the live bot |
| Can it run NinjaTrader? | ❌ No — NT8 is Windows-only | ✅ Yes — the only place it runs |
| Holds the canonical **code**? | ✅ Yes (git source of truth) | Working copies only |
| Holds the canonical **data**? | ❌ No | ✅ Yes — data is born here and lives here |

**Two one-way flows:**
- **Code flows Mac → ThinkPad** (via git). You never author on the ThinkPad.
- **Data is produced on the ThinkPad and stays there.** You pull copies to the Mac only for exploration if you want, but the ThinkPad is where the real runs happen.

The reason: NinjaTrader (which builds the features and produces the training data) is Windows-only and lives on the ThinkPad. Keeping the Python that reads that data on the same box removes an entire class of "it worked on my Mac" problems and guarantees feature parity with what runs live.

---

## 2. Folder layout

### On the Mac (authoring)
```
~/dev/nq-bot-v3/          <- the git repo, your source of truth
├─ ninjascript/           <- .cs files you EDIT here, COMPILE on the ThinkPad
├─ data_pipeline/         <- schema.py, validate.py
├─ model/                 <- train.py, evaluate.py, export_onnx.py, exit_policy.py
├─ bias_engine/
├─ claude/
└─ docs/                  <- this file lives here
```

### On the ThinkPad (execution)
```
C:\nqbotv3\
├─ repo\                  <- git clone of the same repo (pull-only; you don't edit here)
├─ data\
│   ├─ training\          <- signals_*.csv and bars_*.csv land HERE
│   └─ context\           <- daily_context.json (later)
└─ models\                <- trained model versions (later)

C:\Users\<you>\Documents\NinjaTrader 8\bin\Custom\Indicators\
                          <- where NinjaTrader actually reads .cs from (see §6)
```

---

## 3. One-time setup

### Mac
1. Install git and a Python manager (`uv` recommended): `curl -LsSf https://astral.sh/uv/install.sh | sh`
2. Clone/create the repo at `~/dev/nq-bot-v3/`.
3. Editor of choice (VS Code, etc.). You'll edit `.cs` here even though you can't run it here — that's fine, it's just text.

### ThinkPad
1. NinjaTrader 8 — already installed (v2 runs on it).
2. Install git for Windows and `uv` (`powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`).
3. `git clone` the repo to `C:\nqbotv3\repo\`.
4. Create the data folders: `C:\nqbotv3\data\training\` and `C:\nqbotv3\data\context\`.
5. Confirm the three v2 classifier files are present and compiling in NinjaTrader (they already are, since v2 runs): `MLLorentzianClassification.cs`, `MLExtensionsLib.cs`, `KernelFunctionsLib.cs`. **`SignalLogger.cs` will not compile without them.**

---

## 4. The file inventory — what each file is and what to do with it

### A. NinjaScript (C#) — authored on Mac, **compiled & run on the ThinkPad**

| File | What it is | Where it goes on the ThinkPad | What you do with it |
|---|---|---|---|
| `SignalLogger.cs` | **The data logger** (just built). Observational indicator, no orders. Produces the training data. | `Documents\NinjaTrader 8\bin\Custom\Indicators\` | Copy in, compile in NT8, apply to a 15s NQ chart, run replay to backfill (see §7). |
| `MLLorentzianClassification.cs` | Entry classifier (unchanged from v2). SignalLogger reads its score. | already in `...\Custom\Indicators\` | Leave alone. Must be present for SignalLogger to compile. |
| `MLExtensionsLib.cs`, `KernelFunctionsLib.cs` | Math libs SignalLogger calls. | already in `...\Custom\Indicators\` | Leave alone. Must be present. |
| `MLTradingStrategy_v3.cs` *(future)* | The live strategy that consumes model predictions and applies the exit policy. | `...\Custom\Strategies\` | Built in a later phase. |
| `onnx_wrapper.cs` *(future)* | Loads the trained model inside NT8. | `...\Custom\Strategies\` | Later. |

> **Key rule for `.cs` files:** you edit them on the Mac, but they only become real when compiled *inside NinjaTrader* on the ThinkPad. NinjaTrader rewrites the file when it compiles (it appends a generated code region). See §6 for the copy workflow that keeps this from fighting with git.

### B. Python — authored on Mac, **run on the ThinkPad** (against the data)

| File | What it is | What you do with it |
|---|---|---|
| `data_pipeline/schema.py` | The canonical column definitions for the CSVs. Single source of truth for the data shape. | Import it everywhere; never redefine columns ad hoc. |
| `data_pipeline/validate.py` *(next)* | Data-quality gate: schema/dtype checks, lookahead audit, right-censor accounting, `signals`↔`bars` join parity. | Run it on the ThinkPad after each logging run, **before** training. |
| `model/train.py` | Fits XGBoost on the logged data. | Run on the ThinkPad (data locality). Trains in seconds on CPU. |
| `model/evaluate.py` | Holdout metrics, calibration. | Run after training. |
| `model/export_onnx.py` | Exports the trained model to ONNX for NT8. | Produces the file `MLTradingStrategy_v3.cs` will load. |
| `model/exit_policy.py` | Deterministic policy turning predictions into stop/target/trail/timeout. | Shared by dev and (ported) live. |
| `bias_engine/*.py`, `claude/*.py` | Daily context + Claude critique/exception loops. | Later phases; run on the VPS or ThinkPad. |

> **Why run Python on the ThinkPad, not the Mac?** The data is there, the files are large, and it keeps one machine responsible for everything that touches market data. *If you'd rather train on the Mac* for comfort, you can — sync the CSVs over and run the exact same `train.py` — but keep all feature logic in `schema.py`/shared modules so nothing silently differs. Default: ThinkPad.

### C. Data — **produced and living on the ThinkPad only**

| File | What it is | Where |
|---|---|---|
| `signals_<run_id>.csv` | One row per observation: full feature snapshot + labels + cluster/filter/execution fields. | `C:\nqbotv3\data\training\` |
| `bars_<run_id>.csv` | Every 15s OHLCV logged once. Join on the 60-min window to reconstruct any path. | `C:\nqbotv3\data\training\` |
| `daily_context.json` *(later)* | Premarket bias-engine context. | `C:\nqbotv3\data\context\` |

> **Data never goes in git.** It's big and machine-specific. Back it up separately (copy to an external drive or a cloud folder). Add `data/` to `.gitignore`.

### D. Docs — authored on Mac, live in the repo

| File | What it is |
|---|---|
| `docs/DEV_WORKFLOW.md` | This file. |
| `docs/architecture.md` | The system design (from the project context doc). |
| `docs/runbook.md` | Operational steps once live. |
| `docs/decisions/` | Short records of decisions made (like the 7 logging decisions). |

---

## 5. Golden rules

1. **Author on the Mac, run on the ThinkPad.** Never edit code directly on the ThinkPad — you'll lose it on the next `git pull`.
2. **Code flows one way** (Mac → git → ThinkPad). **Data stays on the ThinkPad.**
3. **`.cs` files get copied into NinjaTrader's `Custom\Indicators` folder and compiled there** — don't point NinjaTrader at your git repo (see §6).
4. **`schema.py` is the one place columns are defined.** Everything imports it.
5. **Data is never committed to git.** `.gitignore` the `data/` folder.

---

## 6. Moving a `.cs` file from Mac to the ThinkPad (the clean way)

NinjaTrader **rewrites** a `.cs` file when it compiles (it maintains an auto-generated region at the bottom). If NinjaTrader edits a file that's also tracked by git, you get endless conflicts. Avoid that with a **copy step**:

1. **Mac:** edit `ninjascript/SignalLogger.cs`, commit, push.
2. **ThinkPad:** `git pull` into `C:\nqbotv3\repo\`.
3. **ThinkPad:** **copy** the file from the repo into NinjaTrader's folder:
   ```
   copy C:\nqbotv3\repo\ninjascript\SignalLogger.cs "C:\Users\<you>\Documents\NinjaTrader 8\bin\Custom\Indicators\"
   ```
   (Do this once; on later updates, copy again to overwrite.)
4. **ThinkPad, in NinjaTrader:** open the **NinjaScript Editor** → the file appears → press **F5** (Compile). Fix any red errors in the Output window.

The repo copy stays clean; NinjaTrader mangles only its own copy in the `Custom\Indicators` folder. The repo is always the source of truth.

> Transport options for Mac → ThinkPad: **git is best.** A shared cloud folder (OneDrive/Dropbox) or a USB stick also work if git isn't set up yet — but the copy-into-NinjaTrader step is the same regardless.

---

## 7. Current phase — step by step (produce your first dataset)

**Goal:** get `SignalLogger.cs` running and generating `signals_*.csv` + `bars_*.csv` from historical replay.

**On the Mac**
1. `SignalLogger.cs` is in `ninjascript/`. Commit and push it.

**On the ThinkPad**
2. `git pull`, then copy `SignalLogger.cs` into `...\NinjaTrader 8\bin\Custom\Indicators\` (§6).
3. Open the NinjaScript Editor, press **F5**. Confirm it compiles clean. *(If `regime.NormalizedSlopeDecline` errors, tell Claude the real property name and re-pull.)*
4. Open a **15-second NQ chart**.
5. Add the **SignalLogger** indicator to it. Check the settings:
   - Min/Max |Score| = 4 / 8
   - Forward Window Minutes = 60
   - Log All Sessions = true (tag RTH/Overnight)
   - Output Folder = `C:\nqbotv3\data\training`
   - Write Bars File = true
6. To **backfill history**, run it in **replay** (Control Center → New → Playback, load 12–24 months of NQ) or load a long historical range on the chart so `OnBarUpdate` walks the past bars.
7. Watch `C:\nqbotv3\data\training\` — you should see `signals_run_*.csv` and `bars_run_*.csv` appear and grow. (Files flush on every row, so you can open them mid-run.)
8. When the run finishes, **validate before trusting the data:** run `validate.py` (next deliverable) on the ThinkPad against those two files. Only then look at distributions / start training.

**Back on the Mac**
9. If validation surfaces a bug in the logger, fix `SignalLogger.cs` on the Mac, commit, and repeat from step 2.

---

## 8. The daily loop (later, once live) — preview

Once v3 is live this becomes a rhythm, all on the ThinkPad except code edits:
- **Premarket:** bias engine writes `daily_context.json`.
- **Session:** NT8 runs the live strategy; the logger keeps recording every signal.
- **Weekly:** run the Claude critique on the week's trades → review → retrain (`train.py`) → manually promote if better → export ONNX → drop into NT8.
- **Code changes** always originate on the Mac and flow through git.

---

## 9. Gotchas

- **NinjaTrader is Windows-only.** There is no Mac build. Don't try to run any `.cs` on the Mac — edit only.
- **Number formatting is already handled.** `SignalLogger.cs` writes all numbers with `InvariantCulture`, so a machine set to a European locale won't turn `1.5` into `1,5` and corrupt the CSV. (This was a real bug earlier in the project's history.)
- **Dependencies must be compiled first.** `SignalLogger.cs` references the three classifier/lib files — they must be present and compiling in the same `Custom\Indicators` folder.
- **Don't commit NinjaTrader's generated region.** That's exactly why we use the copy step in §6 instead of pointing NT8 at the repo.
- **Data out of git.** Big, machine-specific, regenerable. Back it up separately.
- **One source of truth for the schema.** If you add a column to the logger, update `schema.py` in the same commit so `validate.py` and `train.py` stay in sync.
```
