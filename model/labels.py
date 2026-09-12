"""
labels.py — turn the logger's RAW excursions into modeling targets.

The logger records raw outcomes (mfe_ticks, mae_ticks, minutes_to_*, horizon
deltas). This module defines the model's targets and, crucially, handles
RIGHT-CENSORING correctly:

    A right-censored row's window was cut short (session close before 60 min).
    Its mfe_ticks / mae_ticks are LOWER BOUNDS, not the true maxima — the real
    excursion could be larger in the part of the window we never saw. Training a
    regression on them as if they were exact biases the model. So every target
    here carries an explicit censoring policy.

No thresholds or model choices are baked in. The one parameterized label
(reached_favorable) takes the tick level as an argument — the level itself is a
data-driven decision made later.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from model.dataset import NUMERIC_TARGETS  # noqa: E402

# Human-readable meaning of each target (single source for train/evaluate).
TARGET_MEANINGS = {
    "mfe_ticks": "max favorable excursion, ticks (censored = lower bound)",
    "mae_ticks": "max adverse excursion, ticks (censored = lower bound)",
    "minutes_to_mfe": "minutes from entry to the favorable extreme",
    "minutes_to_mae": "minutes from entry to the adverse extreme",
    "final_delta_ticks": "signed close-vs-entry at window end (dir-normalized)",
    "delta_1m": "dir-normalized close delta 1 min out",
    "delta_60m": "dir-normalized close delta 60 min out",
}

# Targets whose value is a LOWER BOUND when the row is right-censored.
CENSOR_SENSITIVE = {"mfe_ticks", "mae_ticks", "minutes_to_mfe", "minutes_to_mae"}


def censoring_report(df: pd.DataFrame) -> dict:
    n = len(df)
    c = int((df.get("right_censored", pd.Series([0] * n)) == 1).sum())
    return {"rows": n, "censored": c, "censored_pct": round(100 * c / max(n, 1), 2)}


def prepare_target(
    df: pd.DataFrame,
    target: str,
    censor_policy: str = "drop",
) -> pd.DataFrame:
    """Return a copy of df ready to train `target` on, per censoring policy.

    censor_policy (only affects CENSOR_SENSITIVE targets):
      "drop"  - remove right-censored rows (safe default; unbiased, loses rows)
      "keep"  - keep them as-is (only valid if you model them as lower bounds)
      "flag"  - keep them and add an `is_censored` column for the caller to weight
    """
    if target not in NUMERIC_TARGETS:
        raise ValueError(f"unknown target '{target}'")

    out = df.copy()
    if target in CENSOR_SENSITIVE and "right_censored" in out:
        if censor_policy == "drop":
            before = len(out)
            out = out[out["right_censored"] != 1]
            print(f"prepare_target('{target}'): dropped {before - len(out)} "
                  f"censored rows ({len(out)} left).")
        elif censor_policy == "flag":
            out["is_censored"] = (out["right_censored"] == 1).astype("Int8")
        elif censor_policy == "keep":
            pass
        else:
            raise ValueError(f"bad censor_policy '{censor_policy}'")

    out = out[out[target].notna()]
    return out


def reached_favorable(df: pd.DataFrame, ticks: float) -> pd.Series:
    """Parameterized classification label: did MFE reach `ticks` favorable?

    Censoring-aware: if a censored row's lower-bound MFE already >= ticks, the
    answer is a definite True. If it's < ticks, the true answer is UNKNOWN (the
    unseen tail might have reached it), so we return <NA> rather than guessing.
    The `ticks` level is a data-driven choice made later — not fixed here.
    """
    reached = df["mfe_ticks"] >= ticks
    label = reached.astype("boolean")
    if "right_censored" in df:
        unknown = (df["right_censored"] == 1) & (~reached)
        label = label.mask(unknown, other=pd.NA)
    return label


if __name__ == "__main__":
    import argparse
    from model import dataset
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir")
    ap.add_argument("--signals")
    args = ap.parse_args()

    df = dataset.load_signals(args.signals, args.dir)
    print("censoring:", censoring_report(df))
    for t in ("mfe_ticks", "minutes_to_mfe", "delta_60m"):
        prepped = prepare_target(df, t, censor_policy="drop")
        print(f"  {t:16s} -> {len(prepped):4d} usable rows | {TARGET_MEANINGS.get(t,'')}")
    lab = reached_favorable(df, ticks=20)
    print(f"reached_favorable(20): True={int((lab==True).sum())} "
          f"False={int((lab==False).sum())} unknown={int(lab.isna().sum())}")
