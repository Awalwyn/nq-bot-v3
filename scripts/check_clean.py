"""
check_clean.py — contamination guards on the output of data_pipeline/clean.py.
Every check is an assert; any failure exits non-zero.

    python scripts/check_clean.py --clean data/clean
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data_pipeline import schema  # noqa: E402
from model import dataset, entries  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", required=True)
    a = ap.parse_args()
    dev_p = glob.glob(os.path.join(a.clean, "signals_dev_*.parquet"))[0]
    hold_p = glob.glob(os.path.join(a.clean, "holdout_LOCKED", "signals_HOLDOUT_*.parquet"))[0]
    man = json.load(open(glob.glob(os.path.join(a.clean, "manifest_*.json"))[0]))
    results = []

    def check(name, cond):
        results.append((name, bool(cond)))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    # 1. holdout is locked
    try:
        dataset.load_clean(hold_p)
        check("holdout refused without allow_holdout", False)
    except dataset.HoldoutLockedError:
        check("holdout refused without allow_holdout", True)

    dev = dataset.load_clean(dev_p)
    hold = dataset.load_clean(hold_p, allow_holdout=True)  # read-only structural checks

    # 2. temporal separation
    check("dev label windows end before first holdout signal",
          pd.to_datetime(dev["window_end_actual"]).max() < hold["signal_time"].min())
    check("no cluster in both dev and holdout",
          not (set(dev["signal_cluster_id"]) & set(hold["signal_cluster_id"])))
    check("no session in both dev and holdout",
          not (set(dev["session_date"]) & set(hold["session_date"])))
    check("embargo sessions absent from both",
          not (set(man["config"]["embargo_sessions"]) & (set(dev["session_date"]) | set(hold["session_date"]))))

    # 3. feature hygiene
    feats = schema.MODEL_FEATURE_CANDIDATES
    check("all model features present in dev", all(c in dev.columns for c in feats))
    check("no forbidden column among features", not (set(feats) & schema.FORBIDDEN_AS_FEATURES))
    check("no excluded column among features", not (set(feats) & set(schema.EXCLUDED_FEATURES)))
    check("dist_vwap_ticks removed from clean file", "dist_vwap_ticks" not in dev.columns)

    # 4. integrity drops took effect
    q = man["quarantined_sessions"]
    bad = 0
    for sess, info in q.items():
        t0 = pd.Timestamp(info["first_failing_signal"])
        for df in (dev, hold):
            m = (df["session_date"] == sess) & \
                (df["signal_time"] + pd.to_timedelta(df["window_minutes"], unit="m") >= t0)
            bad += int(m.sum())
    check("no rows from quarantined session tails", bad == 0)
    for df, nm in ((dev, "dev"), (hold, "holdout")):
        check(f"{nm}: bar/tick agreement within tolerance on every kept row",
              (df["xs_dmax"].fillna(0) <= man["config"]["xsource_tol_ticks"]).all())

    # 5. entries are intact (one first-signal per kept cluster)
    for df, nm in ((dev, "dev"), (hold, "holdout")):
        firsts = df.groupby("signal_cluster_id")["is_first_signal_in_cluster"].sum()
        check(f"{nm}: every kept cluster has exactly one first signal", (firsts == 1).all())
        ent = entries.to_entries(df, policy="first")
        check(f"{nm}: entries.py returns one row per cluster", len(ent) == df["signal_cluster_id"].nunique())

    # 6. extended rows are full, uncensored 60-min windows
    ext = dev[dev["label_source"] == "bars_extended_60m"]
    check("extended rows uncensored", (ext["right_censored"] == 0).all())
    span = (pd.to_datetime(ext["window_end_actual"]) - ext["signal_time"]).dt.total_seconds() / 60
    check("extended rows span exactly window_minutes", (span == ext["window_minutes"]).all())
    check("extended rows are all RTH", (ext["session_tag"] == "RTH").all())

    n_fail = sum(1 for _, ok in results if not ok)
    print(f"{len(results) - n_fail}/{len(results)} checks passed.")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
