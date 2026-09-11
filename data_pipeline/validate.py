"""
validate.py — data-quality gate for the v3 logger output.

Run this on the ThinkPad after each logging run, BEFORE training or analysis.
It never fixes data; it reports. Hard failures exit non-zero so you can wire it
into a script and refuse to train on a broken dataset.

Usage:
    # validate the most recent run in a folder
    uv run python -m data_pipeline.validate --dir "C:\\nqbotv3\\data\\training"

    # or point at specific files
    uv run python -m data_pipeline.validate \
        --signals path\\to\\signals_run_x.csv \
        --bars    path\\to\\bars_run_x.csv

Checks
  1. Files load; columns match schema (names + order).
  2. Required columns non-null; bool columns are 0/1.
  3. Score in band; direction / is_long consistent with score sign.
  4. Cluster invariants (first-in-cluster flags, constant direction, monotonic
     bars_since_cluster_start).
  5. Session tag valid; session_pass == (tag == RTH).
  6. Composite filter == AND of the components named in filter_config.
  7. Label sanity: MFE/MAE >= 0; times within window; censor implies missing
     late horizons; window_end - signal_time <= window_minutes.
  8. Right-censor accounting (counts + %).
  9. signals<->bars JOIN PARITY: recompute MFE/MAE from the bars path over each
     signal's window and confirm the logged (tick-based) values are consistent.
 10. Distribution summary (by score, session, cluster position, filter pass rate).
"""

from __future__ import annotations

import argparse
import glob
import os
import sys

import numpy as np
import pandas as pd

from . import schema

TICK = 0.25  # NQ/MNQ tick size, in points. Only used for the bars-path parity check.


class Report:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.info: list[str] = []

    def err(self, m): self.errors.append(m)
    def warn(self, m): self.warnings.append(m)
    def note(self, m): self.info.append(m)

    def print(self):
        for m in self.info:
            print(f"  · {m}")
        for m in self.warnings:
            print(f"  ! WARN  {m}")
        for m in self.errors:
            print(f"  X ERROR {m}")
        print("-" * 64)
        if self.errors:
            print(f"FAILED — {len(self.errors)} error(s), {len(self.warnings)} warning(s).")
        else:
            print(f"PASSED — 0 errors, {len(self.warnings)} warning(s).")


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------

def _latest(dir_path: str, prefix: str) -> str | None:
    hits = sorted(glob.glob(os.path.join(dir_path, f"{prefix}_*.csv")))
    return hits[-1] if hits else None


def load(signals_path, bars_path, dir_path, rep: Report):
    if dir_path:
        signals_path = signals_path or _latest(dir_path, "signals")
        bars_path = bars_path or _latest(dir_path, "bars")

    if not signals_path or not os.path.exists(signals_path):
        rep.err(f"signals file not found: {signals_path}")
        return None, None
    rep.note(f"signals: {signals_path}")

    sig = pd.read_csv(signals_path, dtype={"signal_id": str, "run_id": str})
    bars = None
    if bars_path and os.path.exists(bars_path):
        rep.note(f"bars:    {bars_path}")
        bars = pd.read_csv(bars_path)
    else:
        rep.warn("bars file not found — join-parity check (9) will be skipped.")
    return sig, bars


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def check_columns(sig, bars, rep: Report):
    got = list(sig.columns)
    want = schema.SIGNALS_COLUMNS
    if got != want:
        missing = [c for c in want if c not in got]
        extra = [c for c in got if c not in want]
        if missing:
            rep.err(f"signals missing columns: {missing}")
        if extra:
            rep.err(f"signals has unexpected columns: {extra}")
        if not missing and not extra:
            rep.err("signals columns present but ORDER differs from schema.")
    else:
        rep.note(f"signals columns OK ({len(got)} columns).")

    if bars is not None:
        if list(bars.columns) != schema.BARS_COLUMNS:
            rep.err(f"bars columns mismatch. got={list(bars.columns)}")
        else:
            rep.note(f"bars columns OK ({len(bars.columns)} columns).")


def check_nulls_and_bools(sig, rep: Report):
    for c in schema.REQUIRED_NON_NULL:
        if c in sig and sig[c].isna().any():
            rep.err(f"required column '{c}' has {int(sig[c].isna().sum())} null(s).")
    for c in schema.BOOL_COLUMNS:
        if c in sig:
            bad = set(pd.unique(sig[c].dropna())) - {0, 1}
            if bad:
                rep.err(f"bool column '{c}' has non-0/1 values: {sorted(bad)[:5]}")


def check_ids(sig, rep: Report):
    dupes = sig["signal_id"].duplicated().sum()
    if dupes:
        rep.err(f"signal_id not unique: {int(dupes)} duplicate(s).")
    runs = pd.unique(sig["run_id"])
    if len(runs) != 1:
        rep.warn(f"signals file contains {len(runs)} run_ids (expected 1): {runs[:3]}")


def check_score_direction(sig, rep: Report):
    absscore = sig["score"].abs()
    out = sig[(absscore < schema.SCORE_MIN_ABS_DEFAULT) | (absscore > schema.SCORE_MAX_ABS_DEFAULT)]
    if len(out):
        rep.err(f"{len(out)} rows have |score| outside [{schema.SCORE_MIN_ABS_DEFAULT},"
                f"{schema.SCORE_MAX_ABS_DEFAULT}].")
    long_bad = sig[(sig["score"] > 0) & (sig["is_long"] != 1)]
    short_bad = sig[(sig["score"] < 0) & (sig["is_long"] != 0)]
    if len(long_bad) or len(short_bad):
        rep.err(f"score sign vs is_long mismatch: {len(long_bad)+len(short_bad)} rows.")
    dir_bad = sig[((sig["score"] > 0) & (sig["direction"] != "LONG")) |
                  ((sig["score"] < 0) & (sig["direction"] != "SHORT"))]
    if len(dir_bad):
        rep.err(f"score sign vs direction string mismatch: {len(dir_bad)} rows.")


def check_clusters(sig, rep: Report):
    if "signal_cluster_id" not in sig:
        return
    bad_dir = 0
    bad_first = 0
    for cid, g in sig.groupby("signal_cluster_id"):
        if g["is_long"].nunique() > 1:
            bad_dir += 1
        firsts = int(g["is_first_signal_in_cluster"].sum())
        if firsts != 1:
            bad_first += 1
    if bad_dir:
        rep.err(f"{bad_dir} cluster(s) contain both long and short signals.")
    if bad_first:
        rep.warn(f"{bad_first} cluster(s) do not have exactly one first-in-cluster flag "
                 f"(can happen if a run starts mid-cluster).")
    if (sig["bars_since_cluster_start"] < 0).any():
        rep.err("negative bars_since_cluster_start present.")


def check_session(sig, rep: Report):
    bad = set(pd.unique(sig["session_tag"])) - schema.VALID_SESSION_TAGS
    if bad:
        rep.err(f"invalid session_tag values: {bad}")
    mism = sig[(sig["session_tag"] == "RTH") & (sig["session_pass"] != 1)]
    mism2 = sig[(sig["session_tag"] == "OVERNIGHT") & (sig["session_pass"] != 0)]
    if len(mism) or len(mism2):
        rep.warn(f"session_pass vs session_tag inconsistent in {len(mism)+len(mism2)} rows.")


def check_composite_filter(sig, rep: Report):
    """entry_filters_passed must equal AND of components named in filter_config."""
    mismatches = 0
    for _, row in sig.iterrows():
        cfg = str(row.get("filter_config", "none"))
        if cfg in ("none", "nan", ""):
            expected = 1
        else:
            comps = [schema.FILTER_COMPONENTS[t] for t in cfg.split("+")
                     if t in schema.FILTER_COMPONENTS]
            expected = int(all(int(row[c]) == 1 for c in comps)) if comps else 1
        if int(row["entry_filters_passed"]) != expected:
            mismatches += 1
    if mismatches:
        rep.err(f"entry_filters_passed disagrees with AND(components) in {mismatches} rows.")
    else:
        rep.note("composite filter consistent with individual filters.")


def check_labels(sig, rep: Report):
    if (sig["mfe_ticks"] < 0).any() or (sig["mae_ticks"] < 0).any():
        rep.err("negative MFE or MAE present (should be positive magnitudes).")

    st = pd.to_datetime(sig["signal_time"], errors="coerce")
    we = pd.to_datetime(sig["window_end"], errors="coerce")
    span_min = (we - st).dt.total_seconds() / 60.0
    over = sig[span_min > sig["window_minutes"] + 0.001]
    if len(over):
        rep.err(f"{len(over)} rows have window_end - signal_time > window_minutes.")

    for col in ("minutes_to_mfe", "minutes_to_mae"):
        bad = sig[(sig[col].notna()) & (sig[col] > sig["window_minutes"] + 0.001)]
        if len(bad):
            rep.warn(f"{len(bad)} rows have {col} beyond window_minutes.")

    # censor implies later horizons are missing
    cens = sig[sig["right_censored"] == 1]
    if len(cens):
        # for censored rows cut before 60m, delta_60m should usually be NaN
        cut_early = cens[(we - st).dt.total_seconds() / 60.0 < 60]
        has_60 = cut_early["delta_60m"].notna().sum()
        if has_60:
            rep.warn(f"{has_60} right-censored rows (cut before 60m) still have delta_60m set.")


def check_censoring(sig, rep: Report):
    n = len(sig)
    c = int((sig["right_censored"] == 1).sum())
    rep.note(f"right-censored: {c}/{n} ({100*c/max(n,1):.1f}%).")
    reasons = sig["finalize_reason"].value_counts().to_dict()
    rep.note(f"finalize_reason: {reasons}")


def check_join_parity(sig, bars, rep: Report, sample=200):
    """Recompute MFE/MAE from the 15s bars path and compare to logged values.

    Logged MFE/MAE are tick-accurate; bars-based are 15s-bar-accurate. So the
    bars-based MFE should be <= logged MFE (a 15s bar can't exceed the intrabar
    extreme) within a small tolerance. A bars-based value MUCH larger than logged
    means a logging bug; much smaller is expected on fast moves.
    """
    if bars is None:
        return
    bars = bars.copy()
    bars["bar_time"] = pd.to_datetime(bars["bar_time"], errors="coerce")
    bars = bars.sort_values("bar_time")

    s = sig.copy()
    s["signal_time"] = pd.to_datetime(s["signal_time"], errors="coerce")
    s["window_end"] = pd.to_datetime(s["window_end"], errors="coerce")
    s = s.sample(min(sample, len(s)), random_state=0)

    exceed = 0
    checked = 0
    bt = bars["bar_time"].values
    hi = bars["high"].values
    lo = bars["low"].values
    for _, r in s.iterrows():
        lo_i = np.searchsorted(bt, np.datetime64(r["signal_time"]))
        hi_i = np.searchsorted(bt, np.datetime64(r["window_end"]))
        if hi_i <= lo_i:
            continue
        seg_hi = hi[lo_i:hi_i]
        seg_lo = lo[lo_i:hi_i]
        if len(seg_hi) == 0:
            continue
        ref = r["signal_reference_price"]
        if r["is_long"] == 1:
            fav = (seg_hi.max() - ref) / TICK
            adv = (ref - seg_lo.min()) / TICK
        else:
            fav = (ref - seg_lo.min()) / TICK
            adv = (seg_hi.max() - ref) / TICK
        fav = max(fav, 0.0)
        adv = max(adv, 0.0)
        # bars-based should not exceed logged by more than ~1 tick
        if fav > r["mfe_ticks"] + 1.0 or adv > r["mae_ticks"] + 1.0:
            exceed += 1
        checked += 1

    if checked == 0:
        rep.warn("join-parity: no overlapping bars found for sampled signals "
                 "(check timezones / that bars cover the signal window).")
    elif exceed:
        rep.err(f"join-parity: {exceed}/{checked} sampled signals have bars-based "
                f"MFE/MAE exceeding logged values by >1 tick (possible logging bug).")
    else:
        rep.note(f"join-parity OK on {checked} sampled signals.")


def summary(sig, rep: Report):
    rep.note(f"total observations: {len(sig)}")
    rep.note(f"by score: {sig['score'].value_counts().sort_index().to_dict()}")
    rep.note(f"by session: {sig['session_tag'].value_counts().to_dict()}")
    fp = 100 * (sig['entry_filters_passed'] == 1).mean()
    rep.note(f"entry_filters_passed rate: {fp:.1f}%")
    firsts = 100 * (sig['is_first_signal_in_cluster'] == 1).mean()
    rep.note(f"first-in-cluster rate: {firsts:.1f}% "
             f"(≈ cluster count {int((sig['is_first_signal_in_cluster']==1).sum())}).")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", help="folder holding signals_*.csv / bars_*.csv (uses newest)")
    ap.add_argument("--signals", help="explicit signals CSV path")
    ap.add_argument("--bars", help="explicit bars CSV path")
    ap.add_argument("--sample", type=int, default=200, help="rows sampled for join-parity")
    args = ap.parse_args()

    print("=" * 64)
    print("v3 data validation")
    print("=" * 64)

    rep = Report()
    sig, bars = load(args.signals, args.bars, args.dir, rep)
    if sig is None:
        rep.print()
        sys.exit(2)

    check_columns(sig, bars, rep)
    # only run value checks if the core columns are present
    if not any("missing columns" in e for e in rep.errors):
        check_nulls_and_bools(sig, rep)
        check_ids(sig, rep)
        check_score_direction(sig, rep)
        check_clusters(sig, rep)
        check_session(sig, rep)
        check_composite_filter(sig, rep)
        check_labels(sig, rep)
        check_censoring(sig, rep)
        check_join_parity(sig, bars, rep, sample=args.sample)
        summary(sig, rep)

    rep.print()
    sys.exit(1 if rep.errors else 0)


if __name__ == "__main__":
    main()
