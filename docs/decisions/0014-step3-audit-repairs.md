# 0014 — Step 3 dataset audit repairs + the live-entry question

Status: accepted (repairs) · OPEN (live entry population) · 11 Oct 2026

## Context

An independent audit, run against Matthew's 25-stage "Step 3 – Dataset Audit"
framework, gave a CONDITIONAL PASS with four repairs. Every claim below was
re-verified here against the raw data before it was applied.

## Repairs applied (data_pipeline/clean.py, schema.py)

1. **Session 2026-06-12 dropped** (`dropped_session`). The merged series stayed
   on the expiring June contract: RTH volume was 43/bar vs 320–567 on the
   surrounding days. This removes 814 dev entries (232 RTH).
2. **Halt-time signals dropped** (`halt_time_signal`). These are signals stamped
   after 16:00:00 and up to 17:00:00 CT, or after Friday's close. All 6 are gone
   (4 by this rule, 2 by earlier rules), and none of them was an entry.
3. **Duplicate features excluded.**
   - `minutes_since_rth_open` = `minute_of_day` minus a constant.
   - `session_pass` = `is_rth` on every row.
4. **`delta_60m` removed.** It is 98.5% empty in the logger, because the 60-min
   horizon falls on the window edge. `final_delta_ticks` is the 60-min move.
   - Correction to ADR 0013: its "delta_60m 100% agreement" was computed on the
     ~2% of rows where the field exists.
   - The rebuild report now prints `n_compared` for every field so this can't
     recur.
   - No model input or target used delta_60m.
5. **Evaluator requirements** (to be built in the next step):
   - replay past 60 min until TP/SL
   - fill gap-through exits at the bar open
   - SL-first on same-bar ties

**Result:**
- Dev: 142,632 entries (40,013 RTH).
- Holdout: unchanged, 55,551 (15,953 RTH).
- `check_clean.py`: 24/24.

## OPEN — which entries does the live bot actually take?

The v2 live strategies (`RevisedMLLongs.cs` / `RevisedMLShorts.cs`) enter on
`ml.StartLongTrade` / `StartShortTrade` with:
- `EntriesPerDirection = 1`
- 200 TP / 400 SL
- `IsExitOnSessionCloseStrategy = false`
- all 7 entry filters on

`StartLongTrade` fires on a filtered state flip, not on every in-band bar. In
this year:
- Only **7% of signals pass all 7 filters** (`entry_filters_passed = 1`).
- A filtered state-flip approximation gives **~1,830 RTH entries for the year**:
  ~1,325 dev and ~510 holdout.
- By contrast, the cluster-first table has ~56K RTH entries.

So the thesis test must be scored on the population the live bot takes, which
needs Matthew to confirm:
- which strategy is live
- whether its filters are on
- how "max 7 contracts" is implemented, since v2 has EntriesPerDirection = 1

Training can still use the broad population, with the filter flags as features.
That choice gets its own dev comparison.
