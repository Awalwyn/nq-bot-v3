"""
dataset.py — load the logger output and shape it for modeling.

This module encodes the CORRECTNESS decisions that must be right regardless of
what the data turns out to look like:

  * TIME-BASED split (never random) — a random split leaks the future into the
    past in a time series.
  * EMBARGO around the split boundary — each signal's label is computed from a
    forward 60-min window, so signals just before the cut have labels that peek
    into the test period. We drop a gap (default = the label window) between
    train and test to kill that overlap leakage.
  * LEAKAGE GUARD — label columns can never end up in X. Enforced with an assert,
    not a comment.

Feature SELECTION and hyperparameters are deliberately NOT decided here — the
builder exposes all point-in-time features and lets later, data-driven steps
choose. Depends only on pandas/numpy so it runs before xgboost is installed.
"""

from __future__ import annotations

import os
import sys
import glob

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data_pipeline import schema  # noqa: E402


# Regression targets the plan calls for (the "three heads"). Meta label columns
# (window_end, right_censored, finalize_reason, window_minutes) are NOT targets.
NUMERIC_TARGETS = [
    "mfe_ticks", "mae_ticks", "minutes_to_mfe", "minutes_to_mae",
    "final_delta_ticks",
    "delta_1m", "delta_3m", "delta_5m", "delta_10m", "delta_15m",
    "delta_30m", "delta_60m",
]


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------

def _latest(dir_path: str, prefix: str) -> str | None:
    hits = sorted(glob.glob(os.path.join(dir_path, f"{prefix}_*.csv")))
    return hits[-1] if hits else None


def load_signals(path: str | None = None, dir_path: str | None = None) -> pd.DataFrame:
    """Load a signals CSV, coerce dtypes, sort chronologically."""
    if path is None:
        if not dir_path:
            raise ValueError("provide path= or dir_path=")
        path = _latest(dir_path, "signals")
        if path is None:
            raise FileNotFoundError(f"no signals_*.csv in {dir_path}")

    df = pd.read_csv(path, dtype={"signal_id": str, "run_id": str})

    missing = [c for c in schema.SIGNALS_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"signals file missing columns: {missing}")

    df["signal_time"] = pd.to_datetime(df["signal_time"], errors="coerce")
    for c in schema.BOOL_COLUMNS:
        if c in df:
            df[c] = df[c].astype("Int8")
    for c in schema.CATEGORICAL_FEATURES:
        if c in df:
            df[c] = df[c].astype("category")

    df = df.sort_values("signal_time").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# splitting  (time-based, with embargo)
# ---------------------------------------------------------------------------

def time_split(
    df: pd.DataFrame,
    test_fraction: float = 0.2,
    embargo_minutes: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Chronological split with an embargo gap to prevent label-window overlap.

    embargo_minutes defaults to the dataset's own window_minutes (the forward
    label horizon), which is the correct gap to remove overlap leakage.
    """
    if df.empty:
        return df.copy(), df.copy()

    if embargo_minutes is None:
        wm = df["window_minutes"].dropna()
        embargo_minutes = int(wm.iloc[0]) if len(wm) else 60

    df = df.sort_values("signal_time").reset_index(drop=True)
    cut_idx = int(len(df) * (1.0 - test_fraction))
    cut_time = df["signal_time"].iloc[cut_idx]

    embargo = pd.Timedelta(minutes=embargo_minutes)
    train = df[df["signal_time"] < (cut_time - embargo)].copy()
    test = df[df["signal_time"] >= cut_time].copy()

    dropped = len(df) - len(train) - len(test)
    print(f"time_split: train={len(train)} test={len(test)} "
          f"embargoed={dropped} (gap={embargo_minutes}m, cut={cut_time})")
    return train, test


# ---------------------------------------------------------------------------
# X / y construction  (leakage-guarded)
# ---------------------------------------------------------------------------

def build_xy(
    df: pd.DataFrame,
    target: str,
    feature_cols: list[str] | None = None,
    drop_na_target: bool = True,
) -> tuple[pd.DataFrame, pd.Series]:
    """Return (X, y). X is point-in-time features only; label columns can never
    enter X. Rows with a missing target (e.g. a censored horizon) are dropped."""
    if target not in NUMERIC_TARGETS:
        raise ValueError(f"target '{target}' not in NUMERIC_TARGETS: {NUMERIC_TARGETS}")

    if feature_cols is None:
        feature_cols = list(schema.MODEL_FEATURE_CANDIDATES) + list(schema.CATEGORICAL_FEATURES)

    # hard leakage guard
    leaks = set(feature_cols) & schema.FORBIDDEN_AS_FEATURES
    assert not leaks, f"LEAKAGE: label columns requested as features: {leaks}"
    assert target in schema.LABEL or target in NUMERIC_TARGETS, "target must be a label"

    work = df.copy()
    if drop_na_target:
        work = work[work[target].notna()]

    X = work[feature_cols].copy()
    y = work[target].astype(float)

    # XGBoost consumes pandas 'category' dtype directly (enable_categorical=True).
    for c in schema.CATEGORICAL_FEATURES:
        if c in X:
            X[c] = X[c].astype("category")

    return X, y


def available_targets(df: pd.DataFrame) -> dict[str, int]:
    """How many non-null rows each target has — useful before committing to one."""
    return {t: int(df[t].notna().sum()) for t in NUMERIC_TARGETS if t in df}


# ---------------------------------------------------------------------------
# quick manual check:  python model/dataset.py --dir data/training
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", help="folder with signals_*.csv")
    ap.add_argument("--signals", help="explicit signals CSV")
    ap.add_argument("--target", default="mfe_ticks")
    ap.add_argument("--test-fraction", type=float, default=0.2)
    args = ap.parse_args()

    df = load_signals(args.signals, args.dir)
    print(f"loaded {len(df)} signals, {df['signal_time'].min()} .. {df['signal_time'].max()}")
    print("target row counts:", available_targets(df))

    train, test = time_split(df, test_fraction=args.test_fraction)
    Xtr, ytr = build_xy(train, args.target)
    Xte, yte = build_xy(test, args.target)
    print(f"X train {Xtr.shape}, X test {Xte.shape}, features={Xtr.shape[1]}")
    print(f"target '{args.target}': train mean={ytr.mean():.2f}, test mean={yte.mean():.2f}")
    print("no-leakage check passed (build_xy asserts).")
