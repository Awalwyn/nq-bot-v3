"""
train.py — fit an XGBoost regressor for one target (one of the "heads").

Plumbing only. The hyperparameters below are PLACEHOLDERS to be tuned on real
data; the point of this file is a correct, reproducible harness:

    load -> censoring-aware target prep -> time split (with embargo)
         -> leakage-guarded X/y -> fit -> holdout metrics
         -> save model + a metadata sidecar (features, target, schema hash,
            git sha, xgboost version, row counts) for full reproducibility.

Requires xgboost + scikit-learn:  uv add xgboost scikit-learn

Usage:
    uv run python -m model.train --dir data/training --target mfe_ticks --out models
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import datetime as dt

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data_pipeline import schema          # noqa: E402
from model import dataset, labels          # noqa: E402


# ---- PLACEHOLDER hyperparameters (tune on real data) ----------------------
DEFAULT_PARAMS = dict(
    n_estimators=300,
    max_depth=5,
    learning_rate=0.05,
    subsample=0.8,
    colsample_bytree=0.8,
    min_child_weight=5,
    reg_lambda=1.0,
    objective="reg:squarederror",
    tree_method="hist",
    enable_categorical=False,   # exportable model is numeric-only (see below)
)

# The exportable/live model uses NUMERIC features only, in a fixed order — that
# order is the parity contract shared with the C# side at serve time. The two
# string columns are excluded: filter_config is redundant (the individual
# *_pass flags are already numeric features) and day_of_week is dropped for now
# (add it later via a fixed weekday->int map if it proves useful).
NUMERIC_FEATURES = list(schema.MODEL_FEATURE_CANDIDATES)


def _git_sha():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return "unknown"


def train_one(dir_path, signals_path, target, out_dir,
              test_fraction=0.2, censor_policy="drop", params=None):
    import xgboost as xgb
    from sklearn.metrics import mean_absolute_error, mean_squared_error

    params = {**DEFAULT_PARAMS, **(params or {})}

    df = dataset.load_signals(signals_path, dir_path)
    df = labels.prepare_target(df, target, censor_policy=censor_policy)
    train_df, test_df = dataset.time_split(df, test_fraction=test_fraction)

    Xtr, ytr = dataset.build_xy(train_df, target, feature_cols=NUMERIC_FEATURES)
    Xte, yte = dataset.build_xy(test_df, target, feature_cols=NUMERIC_FEATURES)
    if len(Xtr) == 0 or len(Xte) == 0:
        raise SystemExit(f"not enough data after split (train={len(Xtr)}, test={len(Xte)}).")

    model = xgb.XGBRegressor(**params)
    model.fit(Xtr, ytr, eval_set=[(Xte, yte)], verbose=False)

    pred = model.predict(Xte)
    mae = float(mean_absolute_error(yte, pred))
    rmse = float(np.sqrt(mean_squared_error(yte, pred)))
    baseline_mae = float(np.mean(np.abs(yte - ytr.mean())))  # predict-the-mean baseline

    os.makedirs(out_dir, exist_ok=True)
    run = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = f"{target}_{run}"
    model_path = os.path.join(out_dir, stem + ".json")
    model.save_model(model_path)

    meta = {
        "target": target,
        "target_meaning": labels.TARGET_MEANINGS.get(target, ""),
        "created_utc": run,
        "git_sha": _git_sha(),
        "xgboost_version": xgb.__version__,
        "n_signals_columns": len(schema.SIGNALS_COLUMNS),
        "features": list(Xtr.columns),
        "n_features": Xtr.shape[1],
        "n_train": int(len(Xtr)),
        "n_test": int(len(Xte)),
        "censor_policy": censor_policy,
        "test_fraction": test_fraction,
        "params": params,
        "holdout_mae": round(mae, 4),
        "holdout_rmse": round(rmse, 4),
        "predict_mean_baseline_mae": round(baseline_mae, 4),
        "beats_baseline": bool(mae < baseline_mae),
        "note": "PLACEHOLDER hyperparameters; metrics on this dataset only.",
    }
    meta_path = os.path.join(out_dir, stem + ".meta.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    print(f"target={target}  n_train={len(Xtr)}  n_test={len(Xte)}  features={Xtr.shape[1]}")
    print(f"  holdout MAE={mae:.3f}  RMSE={rmse:.3f}  "
          f"(predict-mean baseline MAE={baseline_mae:.3f}, "
          f"{'BEATS' if mae < baseline_mae else 'does NOT beat'} baseline)")
    print(f"  saved: {model_path}")
    print(f"  meta:  {meta_path}")
    return model_path, meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", help="folder with signals_*.csv")
    ap.add_argument("--signals", help="explicit signals CSV")
    ap.add_argument("--target", default="mfe_ticks",
                    help=f"one of {dataset.NUMERIC_TARGETS}")
    ap.add_argument("--out", default="models", help="model output folder")
    ap.add_argument("--test-fraction", type=float, default=0.2)
    ap.add_argument("--censor-policy", default="drop", choices=["drop", "keep", "flag"])
    ap.add_argument("--all-heads", action="store_true",
                    help="train mfe_ticks, mae_ticks, minutes_to_mfe")
    args = ap.parse_args()

    targets = ["mfe_ticks", "mae_ticks", "minutes_to_mfe"] if args.all_heads else [args.target]
    for t in targets:
        train_one(args.dir, args.signals, t, args.out,
                  test_fraction=args.test_fraction, censor_policy=args.censor_policy)


if __name__ == "__main__":
    main()
