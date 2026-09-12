"""
export_onnx.py — convert a trained XGBoost model to ONNX for NinjaTrader.

The live model runs inside NT8 via ONNX Runtime, taking a numeric feature vector
in a FIXED order (the same order train.py recorded in the model's .meta.json).
This exporter:

  1. loads the saved XGBoost model + its metadata,
  2. converts to ONNX,
  3. RUN-TIME PARITY CHECK: predicts on random inputs with both the XGBoost model
     and the ONNX model and asserts they match — so a conversion bug can't slip
     through silently into live trading,
  4. writes the .onnx next to the model and stamps the feature order into a
     sidecar so the C# side wires inputs in the exact same order.

Requires:  uv add onnxmltools onnxruntime
Usage:
    uv run python -m model.export_onnx --model models/mfe_ticks_<run>.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def export(model_path: str, out_path: str | None = None, tol: float = 1e-4):
    import xgboost as xgb
    from onnxmltools import convert_xgboost
    from onnxmltools.convert.common.data_types import FloatTensorType
    import onnxruntime as ort

    meta_path = model_path.replace(".json", ".meta.json")
    if not os.path.exists(meta_path):
        raise SystemExit(f"metadata sidecar not found: {meta_path} "
                         "(train.py writes it next to the model).")
    meta = json.load(open(meta_path))
    features = meta["features"]
    n_features = len(features)

    model = xgb.XGBRegressor()
    model.load_model(model_path)

    # onnxmltools requires positional feature names (f0, f1, ...), not the real
    # column names XGBoost stored from the training DataFrame. Strip them; the
    # .features.json sidecar preserves position -> feature so C# wires inputs in
    # the right order. This makes the ONNX input a plain positional vector.
    model.get_booster().feature_names = None

    # convert
    initial_types = [("input", FloatTensorType([None, n_features]))]
    onnx_model = convert_xgboost(model, initial_types=initial_types)

    out_path = out_path or model_path.replace(".json", ".onnx")
    with open(out_path, "wb") as f:
        f.write(onnx_model.SerializeToString())

    # ---- parity check: xgboost vs onnxruntime on random inputs ----
    rng = np.random.default_rng(0)
    sample = rng.normal(size=(64, n_features)).astype(np.float32)
    xgb_pred = model.predict(sample).ravel()
    sess = ort.InferenceSession(out_path, providers=["CPUExecutionProvider"])
    onnx_pred = np.array(sess.run(None, {"input": sample})[0]).ravel()

    max_diff = float(np.max(np.abs(xgb_pred - onnx_pred)))
    ok = max_diff <= tol

    # feature-order sidecar for the C# side
    order_path = out_path.replace(".onnx", ".features.json")
    json.dump({"target": meta.get("target"),
               "feature_order": features,
               "n_features": n_features,
               "onnx_input_name": "input"},
              open(order_path, "w"), indent=2)

    print(f"exported: {out_path}")
    print(f"features: {n_features} (order stamped in {os.path.basename(order_path)})")
    print(f"parity check: max|xgb-onnx| = {max_diff:.2e}  "
          f"({'PASS' if ok else 'FAIL'} at tol {tol:.0e})")
    if not ok:
        raise SystemExit("ONNX parity check FAILED — do not ship this model.")
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="path to a trained *.json XGBoost model")
    ap.add_argument("--out", help="output .onnx path (default: alongside model)")
    args = ap.parse_args()
    export(args.model, args.out)


if __name__ == "__main__":
    main()
