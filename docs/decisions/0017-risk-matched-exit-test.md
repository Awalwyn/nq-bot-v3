# 0017 — Risk-matched exit test: the model adds nothing a volatility lookup doesn't

Status: RESULT · 12 Oct 2026 · dev only, holdout still sealed

## Question

ADR 0016 found that learned exits have lower per-trade risk than flat 200/400, but they also use smaller stops. This test asks whether the model earns that, or whether simpler rules match it.

## What ran

`model/risk_eval.py`. The folds, costs and replay are the same as ADR 0016. All settings are chosen on each fold's validation slice and scored on its unseen test month.

Four exit policies:

| Policy | Rule |
|---|---|
| learned | TP = a·XGB MFE prediction, SL = b·XGB MAE prediction |
| vol_scaled | TP = a·volMFE, SL = b·volMAE, from the median MFE/MAE per ATR decile of the fit span (no ML) |
| flat_matched | flat TP/SL equal to the learned policy's mean TP/SL on the validation slice |
| flat200_400 | the live setting |

## Results (unseen months Feb–Jun, net of costs)

**Cluster-first RTH (22,691 trades, 102 days):**

| Policy | Mean net | Std per trade | Daily std | Worst day | Max drawdown |
|---|---|---|---|---|---|
| learned | −0.8 [−5.8, +4.4] | 202 | 5,931 | −21.8K | −55.8K |
| vol_scaled | +3.2 [−3.0, +9.5] | 254 | 7,074 | −16.4K | −70.7K |
| flat_matched | −1.2 [−8.8, +6.6] | 200 | 8,436 | −38.4K | −103.6K |
| flat200_400 | −1.0 [−8.4, +6.5] | 301 | 8,475 | −25.4K | −104.0K |

Paired differences per trade:

| Comparison | Mean | 95% CI |
|---|---|---|
| learned − flat_matched | +0.4 | [−6.3, +6.6] |
| learned − vol_scaled | −4.0 | [−10.4, +2.2] |

**v2-live approximation (707 trades, 98 days):**

| Policy | Mean net | Worst day | Max drawdown | Days below −800 ticks |
|---|---|---|---|---|
| learned | +4.9 | −1,440 | −5,639 | 8 |
| vol_scaled | +5.3 | −2,290 | −9,939 | 10 |
| flat_matched | −3.1 | −1,384 | −7,817 | 4 |
| flat200_400 | −4.2 | −1,819 | −8,208 | 10 |

None of the paired differences is significant. For learned − flat_matched it is +8.1, CI [−4.5, +19.5].

## Findings

1. **Per-trade risk reduction is just smaller stops.** flat_matched has the same per-trade std as learned (200 vs 202).
2. **Scaling stops to volatility does cut daily tail risk on the broad population.** At matched per-trade risk, max drawdown roughly halves vs flat. A no-ML ATR lookup gets most of that, and it has the best worst day.
3. **No policy shows an expectancy edge.** The model's settings also change from fold to fold.
4. **On the live-approx set (n = 707), the evidence is too thin to rank policies.**

## Decision

- **Phase 2 thesis on dev: not supported.** XGBoost exits beat neither flat nor a volatility lookup.
- **If the team wants risk control now,** a deterministic ATR-scaled stop/target is the candidate: trivial in C#, no ONNX. It should be tested against flat 200/400 on the live entry population once Matthew confirms it.
- **Holdout stays sealed** until the live population and the one pre-registered comparison are fixed.
