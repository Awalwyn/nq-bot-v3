"""
analyze.py — feature analysis for the exit model.

For each target head (expected MFE / MAE / time-to-MFE), report which features
carry signal, two ways:
  1. Spearman rank correlation of each numeric feature with the target
     (monotonic association, robust to scale/outliers; no scipy dependency).
  2. XGBoost gain importance from a quick fit (captures nonlinear + interactions).

Runs on real ENTRIES (deduped), censoring-aware. This is a TOOL that will be most
useful on the full dataset; on the 5-day pilot the rankings are directional only
(too little data, few regimes) — treat them as a smoke test of the plumbing, not
conclusions.

Usage:
    uv run python -m model.analyze --dir data/training
    uv run python -m model.analyze --dir data/training --target mfe_ticks --top 15
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data_pipeline import schema            # noqa: E402
from model import dataset, labels, entries  # noqa: E402

FEATURES = list(schema.MODEL_FEATURE_CANDIDATES)
HEADS = ["mfe_ticks", "mae_ticks", "minutes_to_mfe"]


def spearman(x: pd.Series, y: pd.Series) -> float:
    """Spearman rho = Pearson on ranks. Needs no scipy."""
    m = x.notna() & y.notna()
    if m.sum() < 10:
        return np.nan
    xr = x[m].rank()
    yr = y[m].rank()
    if xr.std() == 0 or yr.std() == 0:
        return np.nan
    return float(np.corrcoef(xr, yr)[0, 1])


def analyze_target(df: pd.DataFrame, target: str, top: int) -> pd.DataFrame:
    d = labels.prepare_target(df, target, censor_policy="drop")
    feats = [c for c in FEATURES if c in d.columns]
    y = pd.to_numeric(d[target], errors="coerce")

    rows = []
    for c in feats:
        x = pd.to_numeric(d[c], errors="coerce")
        if x.nunique(dropna=True) <= 1:
            continue
        rows.append({"feature": c, "spearman": spearman(x, y),
                     "abs_spearman": abs(spearman(x, y))})
    corr = pd.DataFrame(rows).sort_values("abs_spearman", ascending=False)

    # XGBoost gain importance
    gain = {}
    try:
        import xgboost as xgb
        X = d[feats].apply(pd.to_numeric, errors="coerce")
        mask = y.notna()
        model = xgb.XGBRegressor(n_estimators=200, max_depth=4, learning_rate=0.05,
                                 subsample=0.8, colsample_bytree=0.8,
                                 objective="reg:squarederror", tree_method="hist")
        model.fit(X[mask], y[mask], verbose=False)
        booster = model.get_booster()
        g = booster.get_score(importance_type="gain")  # keyed f0.. or names
        names = X.columns.tolist()
        for k, v in g.items():
            idx = int(k[1:]) if k.startswith("f") and k[1:].isdigit() else None
            name = names[idx] if idx is not None and idx < len(names) else k
            gain[name] = v
    except Exception as e:
        print(f"  (xgboost importance skipped: {e})")

    corr["xgb_gain"] = corr["feature"].map(gain).fillna(0.0)
    return corr.head(top).reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", help="folder with signals_*.csv")
    ap.add_argument("--signals", help="explicit signals CSV")
    ap.add_argument("--target", help="single head; default = all three")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--no-entries", action="store_true",
                    help="analyze raw signals instead of deduped entries")
    args = ap.parse_args()

    df = dataset.load_signals(args.signals, args.dir)
    if not args.no_entries:
        n0 = len(df)
        df = entries.to_entries(df, policy="first")
        print(f"analyzing {len(df)} real entries (from {n0} signals)\n")
    else:
        print(f"analyzing {len(df)} raw signals\n")

    targets = [args.target] if args.target else HEADS
    for t in targets:
        print(f"==== target: {t} ====")
        tbl = analyze_target(df, t, args.top)
        show = tbl[["feature", "spearman", "xgb_gain"]].copy()
        show["spearman"] = show["spearman"].round(3)
        show["xgb_gain"] = show["xgb_gain"].round(1)
        print(show.to_string(index=False))
        print()
    print("NOTE: pilot-scale results are DIRECTIONAL ONLY — too little data / too few "
          "regimes to trust. Re-run on the full dataset for real feature selection.")


if __name__ == "__main__":
    main()
