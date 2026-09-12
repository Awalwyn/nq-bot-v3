# 0002 — Model skeleton: correctness before tuning

Status: accepted (skeleton built and tested on synthetic data; not yet tuned)

## Context
Phase 2 was built while the ThinkPad was offline, i.e. before any real data
exists. To avoid premature, data-driven guessing, we built only what depends on
the SCHEMA (structure), not on data VALUES (choices), and tested it on synthetic
data.

## Decisions
1. **Time-based split, never random**, with an **embargo** equal to the label
   window (60 min) between train and test — each signal's label is computed from
   its forward window, so signals near the cut would otherwise leak into test.
2. **Censoring-aware targets** — right-censored rows have lower-bound MFE/MAE;
   dropped by default for regression (`labels.prepare_target`).
3. **Leakage guard** — `build_xy` asserts no LABEL column can enter X.
4. **Numeric-only exportable model** — the live/ONNX model takes a positional
   numeric vector in a fixed order (recorded in each model's .meta.json and in a
   .features.json sidecar at export). filter_config is dropped (redundant with
   the numeric *_pass flags); day_of_week deferred.
5. **ONNX export includes a parity check** — xgboost vs onnxruntime predictions
   must match within tol or the export refuses to ship.
6. **Evaluate framework built before the model** — the success metric ("beat the
   flat exit on the same entries") is fixed in advance and cannot be retrofit.

## Deferred until real data (NOT decided here)
Feature selection, hyperparameters, exit-policy band values, and any claim that
the model works.
