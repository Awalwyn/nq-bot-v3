# ThinkPad Setup — ordered command sheet

Run these top to bottom when the ThinkPad is back up. PowerShell unless noted.
Placeholders in `<...>` — fill in your real values.

---

## 0. Prereqs (confirm once)

- NinjaTrader 8 installed (it already is — v2 runs on it).
- Git for Windows installed: `winget install --id Git.Git -e`

---

## 1. Install uv (Python manager)

The `astral.sh` install script failed earlier with a DNS error. Use winget
instead — different infrastructure, no DNS issue:

```powershell
winget install --id=astral-sh.uv -e
```

Fallbacks if winget can't find it:
```powershell
# a) if Python is already installed:
pip install uv

# b) or fix DNS, then retry the original script:
ipconfig /flushdns
# Settings > Network > adapter > Edit DNS > Manual > IPv4 on > 1.1.1.1 / 8.8.8.8
```

Verify: `uv --version`

---

## 2. Clone the repo

```powershell
git clone <your-repo-url> C:\nqbotv3\repo
cd C:\nqbotv3\repo
```

(On later updates it's just `git pull` from `C:\nqbotv3\repo`.)

---

## 3. Create the data folders

```powershell
mkdir C:\nqbotv3\data\training -Force
mkdir C:\nqbotv3\data\context  -Force
mkdir C:\nqbotv3\models         -Force
```

---

## 4. Set up the Python environment

```powershell
cd C:\nqbotv3\repo
uv sync
```

Smoke-test that the pipeline code runs before there's any real data:
```powershell
uv run python scripts\make_sample_data.py --out C:\nqbotv3\data\training
uv run python -m data_pipeline.validate --dir C:\nqbotv3\data\training
```
You should see `PASSED`. Then delete the sample files so they don't mix with
real data:
```powershell
del C:\nqbotv3\data\training\*sample*
```

---

## 5. Confirm the classifier dependencies are in NinjaTrader

`SignalLogger.cs` will NOT compile without these three, which already exist
since v2 runs. Confirm they're in:
`C:\Users\<you>\Documents\NinjaTrader 8\bin\Custom\Indicators\`
- `MLLorentzianClassification.cs`
- `MLExtensionsLib.cs`
- `KernelFunctionsLib.cs`

---

## 6. Copy the logger into NinjaTrader and compile

```powershell
copy C:\nqbotv3\repo\ninjascript\SignalLogger.cs "C:\Users\<you>\Documents\NinjaTrader 8\bin\Custom\Indicators\"
```
Then in NinjaTrader: open the **NinjaScript Editor** → find `SignalLogger` →
press **F5** to compile. Watch the Output window for errors.

> If it errors on `regime.NormalizedSlopeDecline`, that's the one property name
> I inferred and couldn't verify. Tell Claude the real property on
> `RegimeFilterState` and re-pull — everything else is confirmed against your
> harness.

---

## 7. Run the logger over history

1. Open a **15-second NQ chart**.
2. Add the **SignalLogger** indicator. Confirm settings:
   - Min/Max |Score| = 4 / 8
   - Forward Window Minutes = 60
   - Log All Sessions = true
   - Output Folder = `C:\nqbotv3\data\training`
   - Write Bars File = true
3. Backfill via **Control Center → New → Playback**, load 12–24 months of NQ,
   or load a long historical range on the chart so it walks the past bars.
4. Watch `C:\nqbotv3\data\training\` — `signals_run_*.csv` and `bars_run_*.csv`
   appear and grow (they flush every row, so you can open them mid-run).

---

## 8. Validate before trusting the data

```powershell
cd C:\nqbotv3\repo
uv run python -m data_pipeline.validate --dir "C:\nqbotv3\data\training"
```
Only move on to distributions / training once this prints `PASSED`.

---

## Notes / gotchas

- **NinjaTrader is Windows-only** — this whole sheet is ThinkPad-only. Code is
  authored on the Mac and pulled here.
- **Number formatting** is already handled (`InvariantCulture`), so a
  non-US locale won't corrupt the CSVs.
- **Tick data**: MFE/MAE come off a 1-tick series. If your replay range only has
  minute data, those labels get coarser — the bars file still lets you recompute
  at 15s resolution. Confirm your replay range actually has tick data.
- **Never commit from the ThinkPad.** NinjaTrader rewrites the `.cs` it compiles;
  the repo copy stays canonical because you *copy* into the NT8 folder (step 6)
  rather than pointing NT8 at the repo.
