# 0011 — Phase 2 groundwork (Mac-side, pre-full-data)

Status: accepted · Phase 2 · built on the validated 5-day pilot

## Context

Full-dataset decision is resolved: Matthew confirmed 365 days of real NQ tick data,
so no Databento is needed and the full collection is a go (pending the
continuous-vs-per-contract setup call). While that collection is pending, this
round builds the reusable Mac-side tooling so training + expectancy evaluation are
one command the day the full data lands. **Scope discipline: build the pipeline,
do not tune a model or draw conclusions from 5 days.**

## What was built

1. **`model/entries.py` — signal→entry dedup (keystone).** The logger records every
   15s bar in the score band (broad/raw by design); a persisting move produces a
   run of same-direction signals in one `signal_cluster_id`. You don't re-enter on
   every bar, so each cluster collapses to one real ENTRY. Policies: `first`
   (realistic live behavior, default) and `strongest` (sensitivity check). Pure
   deterministic bookkeeping on the cluster fields — no data created or changed.
   Pilot: **16,587 signals → 3,079 entries** (5.4× collapse; balanced 1550 long /
   1529 short).

2. **`model/evaluate.py` — expectancy on real entries.** Added `--entries` (dedup
   first) so the flat-exit baseline is per real trade, not per 15s signal. This is
   the honest bar a learned exit must beat.

3. **`model/analyze.py` — feature analysis.** Per head: Spearman rank correlation
   (no scipy dep) + XGBoost gain importance, on entries, censoring-aware. A tool for
   the full dataset; pilot rankings are directional only.

## Preliminary pilot numbers (NOT conclusions — 5 days, placeholder exits)

Per-entry flat-exit baseline (3,076 entries):

| strategy | mean ticks/trade | win % |
|---|---|---|
| flat 200/400 | **+7.2** | 55.7 |
| flat 100/200 | −5.6 | 61.9 |
| flat 50/100 (15m timeout) | −0.8 | 62.0 |

So the number a learned exit must beat on the full data is ~**+7.2 ticks/entry**
(flat 200/400). Feature-correlation smoke test: volatility-family features (ATR,
bar range, volume) lead association with MFE — intuitive, but directional only.

**These are from 5 days with placeholder tp/sl. They are plumbing-validation, not
evidence about the thesis.** Real numbers come from the full dataset.

## Deliberately NOT done (avoiding getting ahead)

- No hyperparameter tuning / model selection (needs full data + regimes).
- No committed data-driven exit bands (the `exit_policy` bands stay placeholder
  until fit on the full distribution).
- No walk-forward grid search (Phase 2 proper, on full data).
- Nothing touching live execution / NT8 strategy (Phase 3).

## State

Validator suite 41/41, all modules compile, no regressions. The day the full
dataset validates with `--pilot`, the sequence is: `entries` → `train --all-heads`
→ `analyze` → `evaluate --entries` with the learned policy plugged in.
