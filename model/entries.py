"""
entries.py — collapse overlapping 15s signals into real ENTRIES.

The logger records EVERY 15s bar whose score is in the band (by design — broad,
raw collection). While a move persists, that produces a run of same-direction
signals in one cluster (signal_cluster_id). You do NOT re-enter on every 15s bar
while already in a trade, so for realistic expectancy AND for training on one row
per trade, each cluster collapses to a single entry.

This is pure, deterministic bookkeeping on the cluster fields the logger already
writes — no data is created or changed. It is the keystone step before any
honest expectancy comparison or per-trade model evaluation.

Entry-selection policies:
  "first"     — the first signal in the cluster (is_first_signal_in_cluster==1).
                This is the realistic live behavior: enter when the signal first
                appears, then hold and manage the exit. DEFAULT.
  "strongest" — the signal with the largest |score| in the cluster (ties -> first).
                Useful as a sensitivity check; not how live entry works.

Usage:
    uv run python -m model.entries --dir data/training
    # or in code:
    from model import entries, dataset
    df = dataset.load_signals(dir_path=...)
    ent = entries.to_entries(df, policy="first")
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from model import dataset  # noqa: E402

VALID_POLICIES = ("first", "strongest")


def to_entries(df: pd.DataFrame, policy: str = "first") -> pd.DataFrame:
    """Return one row per signal_cluster_id (one real entry per cluster)."""
    if policy not in VALID_POLICIES:
        raise ValueError(f"policy must be one of {VALID_POLICIES}, got {policy!r}")
    if "signal_cluster_id" not in df.columns:
        raise ValueError("signals frame has no signal_cluster_id column")

    d = df.copy()
    d["signal_time"] = pd.to_datetime(d["signal_time"], errors="coerce")
    d = d.sort_values(["signal_cluster_id", "signal_time"]).reset_index(drop=True)

    if policy == "first":
        # Prefer the logger's own flag; fall back to the earliest-in-cluster row
        # so we never drop a cluster even if the flag is missing/zero.
        flagged = d[d.get("is_first_signal_in_cluster", 0) == 1]
        picked_ids = set(flagged["signal_cluster_id"].unique())
        chosen = [flagged]
        missing = d[~d["signal_cluster_id"].isin(picked_ids)]
        if len(missing):
            chosen.append(missing.groupby("signal_cluster_id", as_index=False).first())
        ent = pd.concat(chosen, ignore_index=True)
        # if the flag ever marks >1 per cluster, keep the earliest
        ent = ent.sort_values(["signal_cluster_id", "signal_time"]) \
                 .groupby("signal_cluster_id", as_index=False).first()
    else:  # strongest
        d["_absscore"] = d["score"].abs()
        # max |score|, ties broken by earliest signal_time
        ent = d.sort_values(["signal_cluster_id", "_absscore", "signal_time"],
                            ascending=[True, False, True]) \
               .groupby("signal_cluster_id", as_index=False).first() \
               .drop(columns="_absscore")

    return ent.sort_values("signal_time").reset_index(drop=True)


def entry_stats(df: pd.DataFrame, ent: pd.DataFrame) -> dict:
    sizes = df.groupby("signal_cluster_id").size()
    return {
        "raw_signals": int(len(df)),
        "clusters_entries": int(len(ent)),
        "collapse_ratio": round(len(df) / max(len(ent), 1), 1),
        "cluster_size_mean": round(float(sizes.mean()), 1),
        "cluster_size_median": int(sizes.median()),
        "cluster_size_max": int(sizes.max()),
        "entries_by_direction": ent["direction"].value_counts().to_dict() if "direction" in ent else {},
        "entries_by_session": ent["session_tag"].value_counts().to_dict() if "session_tag" in ent else {},
        "entries_by_finalize": ent["finalize_reason"].value_counts().to_dict() if "finalize_reason" in ent else {},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", help="folder with signals_*.csv")
    ap.add_argument("--signals", help="explicit signals CSV")
    ap.add_argument("--policy", default="first", choices=VALID_POLICIES)
    ap.add_argument("--out", help="optional: write entries CSV here")
    args = ap.parse_args()

    df = dataset.load_signals(args.signals, args.dir)
    ent = to_entries(df, policy=args.policy)
    st = entry_stats(df, ent)

    print(f"entry policy: {args.policy}")
    print(f"  raw signals ...... {st['raw_signals']}")
    print(f"  entries .......... {st['clusters_entries']}  "
          f"({st['collapse_ratio']}x collapse)")
    print(f"  cluster size ..... mean {st['cluster_size_mean']}, "
          f"median {st['cluster_size_median']}, max {st['cluster_size_max']}")
    print(f"  by direction ..... {st['entries_by_direction']}")
    print(f"  by session ....... {st['entries_by_session']}")
    print(f"  by finalize ...... {st['entries_by_finalize']}")

    if args.out:
        ent.to_csv(args.out, index=False)
        print(f"  wrote entries -> {args.out}")


if __name__ == "__main__":
    main()
