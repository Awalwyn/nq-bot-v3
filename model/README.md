# model/ — Phase 2

Built and tested on synthetic data. Nothing here is *tuned* — hyperparameters,
exit bands, and feature choices are placeholders that get decided on real data.
The structure and the correctness guards are what's locked in.

Install deps:  `uv sync --extra model`

| File | Does | Run |
|---|---|---|
| `dataset.py`     | load, time-split **with embargo**, leakage-guarded X/y | `python -m model.dataset --dir data/training` |
| `labels.py`      | targets from raw excursions, **censoring-aware** | `python -m model.labels --dir data/training` |
| `evaluate.py`    | replay exits over logged paths, compare expectancy | `python -m model.evaluate --dir data/training` |
| `exit_policy.py` | predictions → stop/target/timeout (plugs into evaluate) | `python -m model.exit_policy` |
| `train.py`       | fit XGBoost per head, save model + metadata sidecar | `python -m model.train --dir data/training --all-heads` |
| `export_onnx.py` | model → ONNX **with a prediction parity check** | `python -m model.export_onnx --model models/<f>.json` |

## Correctness decisions baked in (see docs/decisions/0002)
- **Time-based split + embargo** — never random; a 60-min gap removes forward-window overlap leakage.
- **Censoring-aware targets** — right-censored MFE/MAE are lower bounds; dropped by default for regression.
- **Leakage guard** — label columns can never enter X (assert, not comment).
- **Numeric-only exportable model** — fixed feature order = the parity contract with C#.
- **Evaluate before model** — success metric (beat flat exit) is defined before any result exists.

## The workflow, once real data exists
```
uv sync --extra model
python -m model.evaluate --dir data/training        # flat-exit baselines first
python -m model.train    --dir data/training --all-heads
python -m model.export_onnx --model models/mfe_ticks_<run>.json
# then plug the trained heads into exit_policy + evaluate.compare to see if
# the learned exit beats the flat baseline on the SAME entries.
```
