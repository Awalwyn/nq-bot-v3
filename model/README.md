# model/ — Phase 2 (after data exists)

Built once `data_pipeline/validate.py` passes on a real dataset. Writing these
before seeing the data would mean guessing at distributions.

Planned:
- `exit_policy.py`  deterministic map: model predictions -> stop/target/trail/timeout
- `train.py`        fit XGBoost on logged signals (labels from schema.LABEL)
- `evaluate.py`     holdout metrics, calibration, baseline comparison (flat vs learned exit)
- `export_onnx.py`  export the trained model to ONNX for NinjaTrader
