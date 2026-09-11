# NQ Bot v3

Exit-policy upgrade for the NQ futures bot: a logger captures every Lorentzian
signal with a full point-in-time feature snapshot and its forward price path, an
XGBoost model learns a per-signal exit policy, and NinjaTrader runs it live.

**Read `docs/DEV_WORKFLOW.md` first.** It explains the two-machine split that the
whole project depends on.

## Two machines, two jobs

- **Mac** — authoring. You edit code and run git here. *Nothing that touches
  market data runs here.*
- **ThinkPad (Windows)** — execution. NinjaTrader, the logger, the Python
  pipeline, and the live bot all run here. Data is born here and stays here.

Code flows **Mac → git → ThinkPad**. Data stays on the ThinkPad.

## Repo layout

```
nq-bot-v3/
├─ ninjascript/        SignalLogger.cs        (edit on Mac, compile+run on ThinkPad)
├─ data_pipeline/      schema.py, validate.py (authored on Mac, run on ThinkPad)
├─ model/              Phase 2 — after data exists
├─ bias_engine/        Phase 3
├─ claude/             Phase 4
└─ docs/               DEV_WORKFLOW.md, decisions/
```

## Where things are today (Phase 1 — data collection)

Buildable and done:
- `ninjascript/SignalLogger.cs` — the observational logger (no orders).
- `data_pipeline/schema.py` — canonical column definitions (matches the .cs).
- `data_pipeline/validate.py` — data-quality gate.
- `docs/DEV_WORKFLOW.md` — the workflow bible.

Deferred until real data exists (deliberately — writing them now would be
guessing at distributions we haven't seen): `model/`, `bias_engine/`, `claude/`.

## Quickstart

### Mac (once)
```bash
cd "/Users/awalwyn/Documents/Github Projects/nq-bot-v3"
git init && git add -A && git commit -m "v3 scaffold: logger, schema, validator, docs"
# create a repo on GitHub, then:
# git remote add origin <url> && git push -u origin main
```

### ThinkPad (when it's back up)
1. `git clone <url> C:\nqbotv3\repo`
2. Copy `ninjascript\SignalLogger.cs` into
   `Documents\NinjaTrader 8\bin\Custom\Indicators\`, compile in NT8 (F5).
   (Full steps: `docs/DEV_WORKFLOW.md` §6–7.)
3. Apply SignalLogger to a 15s NQ chart, run replay → CSVs land in
   `C:\nqbotv3\data\training\`.
4. Validate before trusting the data:
   ```
   uv run python -m data_pipeline.validate --dir "C:\nqbotv3\data\training"
   ```
