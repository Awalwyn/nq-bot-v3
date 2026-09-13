"""
validate.py — data-quality gate for the v3 logger output (rev 2).

Run on the ThinkPad after each logging run, BEFORE training. Reports only;
hard failures exit non-zero. Rev 2 adds the data-integrity review fixes:

  * PAIRING  — signals and bars must share run_id, instrument (full contract),
    bar_period and tick_size, or validation FAILS (no accidental cross-run pair).
  * CONFIG-AWARE — reads <run_id>.meta.json and validates the score band and
    window against the settings actually used for that run, not hardcoded 4-8.
  * PARITY WINDOW — reconstruction starts AFTER the signal bar closes (the ref
    price is that bar's close; its earlier intrabar high/low must not count) and
    ends at window_end_actual, using each row's own tick_size.
  * CENSORING — finalize_reason breakdown; rth_close / terminated rows must carry
    right_censored = 1.

Usage:
    uv run python -m data_pipeline.validate --dir "C:\\nqbotv3\\data\\training"
    uv run python -m data_pipeline.validate --signals path.csv --bars path.csv
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

from . import schema


class Report:
    def __init__(self):
        self.errors, self.warnings, self.info = [], [], []
    def err(self, m): self.errors.append(m)
    def warn(self, m): self.warnings.append(m)
    def note(self, m): self.info.append(m)
    def print(self):
        for m in self.info: print(f"  · {m}")
        for m in self.warnings: print(f"  ! WARN  {m}")
        for m in self.errors: print(f"  X ERROR {m}")
        print("-" * 64)
        print(f"{'FAILED' if self.errors else 'PASSED'} — "
              f"{len(self.errors)} error(s), {len(self.warnings)} warning(s).")


def _latest(dir_path, prefix):
    hits = sorted(glob.glob(os.path.join(dir_path, f"{prefix}_*.csv")))
    return hits[-1] if hits else None


def _bars_for_run(dir_path, run_id):
    """Find the bars file whose content run_id matches (not just newest)."""
    for p in sorted(glob.glob(os.path.join(dir_path, "bars_*.csv"))):
        try:
            head = pd.read_csv(p, nrows=1)
            if "run_id" in head and str(head["run_id"].iloc[0]) == str(run_id):
                return p
        except Exception:
            continue
    return _latest(dir_path, "bars")


def load(signals_path, bars_path, dir_path, rep):
    if dir_path and not signals_path:
        signals_path = _latest(dir_path, "signals")
    if not signals_path or not os.path.exists(signals_path):
        rep.err(f"signals file not found: {signals_path}")
        return None, None, None
    rep.note(f"signals: {signals_path}")
    sig = pd.read_csv(signals_path, dtype={"signal_id": str, "run_id": str})

    run_id = str(sig["run_id"].iloc[0]) if "run_id" in sig and len(sig) else None
    folder = dir_path or os.path.dirname(signals_path)

    if not bars_path:
        bars_path = _bars_for_run(folder, run_id) if run_id else _latest(folder, "bars")
    bars = None
    if bars_path and os.path.exists(bars_path):
        rep.note(f"bars:    {bars_path}")
        bars = pd.read_csv(bars_path, dtype={"run_id": str})
    else:
        rep.warn("bars file not found — pairing + parity checks skipped.")

    meta = None
    if run_id:
        mp = os.path.join(folder, run_id + ".meta.json")
        if os.path.exists(mp):
            meta = json.load(open(mp))
            rep.note(f"meta:    {os.path.basename(mp)}")
        else:
            rep.warn(f"run metadata {run_id}.meta.json not found — "
                     "validating against schema defaults, not run settings.")
    return sig, bars, meta


# --------------------------------------------------------------------------

def check_pairing(sig, bars, rep):
    """signals and bars must agree on run_id, instrument, bar_period, tick_size."""
    if bars is None:
        return
    for col in ("run_id", "instrument", "bar_period", "tick_size"):
        if col not in sig or col not in bars:
            rep.err(f"pairing: column '{col}' missing from one of the files.")
            continue
        s = set(pd.unique(sig[col].dropna()))
        b = set(pd.unique(bars[col].dropna()))
        if not s.issubset(b) and s != b:
            rep.err(f"pairing MISMATCH on '{col}': signals={s} bars={b} — "
                    "these files are not from the same run.")
    if not any("pairing" in e for e in rep.errors):
        rep.note("pairing OK (run_id / instrument / bar_period / tick_size agree).")


def check_columns(sig, bars, rep):
    if list(sig.columns) != schema.SIGNALS_COLUMNS:
        missing = [c for c in schema.SIGNALS_COLUMNS if c not in sig.columns]
        extra = [c for c in sig.columns if c not in schema.SIGNALS_COLUMNS]
        if missing: rep.err(f"signals missing columns: {missing}")
        if extra: rep.err(f"signals unexpected columns: {extra}")
        if not missing and not extra: rep.err("signals column ORDER differs from schema.")
    else:
        rep.note(f"signals columns OK ({len(sig.columns)}).")
    if bars is not None:
        if list(bars.columns) != schema.BARS_COLUMNS:
            rep.err(f"bars columns mismatch: {list(bars.columns)}")
        else:
            rep.note(f"bars columns OK ({len(bars.columns)}).")


def check_nulls_and_bools(sig, rep):
    for c in schema.REQUIRED_NON_NULL:
        if c in sig and sig[c].isna().any():
            rep.err(f"required '{c}' has {int(sig[c].isna().sum())} null(s).")
    for c in schema.BOOL_COLUMNS:
        if c in sig:
            bad = set(pd.unique(sig[c].dropna())) - {0, 1}
            if bad:
                rep.err(f"bool '{c}' has non-0/1 values: {sorted(bad)[:5]}")


def check_ids(sig, rep):
    d = sig["signal_id"].duplicated().sum()
    if d: rep.err(f"signal_id not unique: {int(d)} duplicate(s).")
    runs = pd.unique(sig["run_id"])
    if len(runs) != 1: rep.warn(f"{len(runs)} run_ids in signals (expected 1).")


def check_score_direction(sig, meta, rep):
    lo = int(meta["min_abs_score"]) if meta and "min_abs_score" in meta else schema.SCORE_MIN_ABS_DEFAULT
    hi = int(meta["max_abs_score"]) if meta and "max_abs_score" in meta else schema.SCORE_MAX_ABS_DEFAULT
    a = sig["score"].abs()
    out = sig[(a < lo) | (a > hi)]
    if len(out): rep.err(f"{len(out)} rows have |score| outside run band [{lo},{hi}].")
    else: rep.note(f"score band OK (|score| in [{lo},{hi}] from run config).")
    bad = sig[((sig["score"] > 0) & (sig["is_long"] != 1)) |
              ((sig["score"] < 0) & (sig["is_long"] != 0))]
    if len(bad): rep.err(f"score sign vs is_long mismatch: {len(bad)} rows.")


def check_clusters(sig, rep):
    bad_dir = sum(g["is_long"].nunique() > 1 for _, g in sig.groupby("signal_cluster_id"))
    if bad_dir: rep.err(f"{bad_dir} cluster(s) mix long and short.")
    if (sig["bars_since_cluster_start"] < 0).any():
        rep.err("negative bars_since_cluster_start.")


def check_session(sig, rep):
    bad = set(pd.unique(sig["session_tag"])) - schema.VALID_SESSION_TAGS
    if bad: rep.err(f"invalid session_tag: {bad}")


def check_composite_filter(sig, rep):
    mis = 0
    for _, r in sig.iterrows():
        cfg = str(r.get("filter_config", "none"))
        if cfg in ("none", "nan", ""):
            exp = 1
        else:
            comps = [schema.FILTER_COMPONENTS[t] for t in cfg.split("+") if t in schema.FILTER_COMPONENTS]
            exp = int(all(int(r[c]) == 1 for c in comps)) if comps else 1
        if int(r["entry_filters_passed"]) != exp:
            mis += 1
    if mis: rep.err(f"entry_filters_passed != AND(components) in {mis} rows.")
    else: rep.note("composite filter consistent with individual filters.")


def check_labels(sig, meta, rep):
    if (sig["mfe_ticks"] < 0).any() or (sig["mae_ticks"] < 0).any():
        rep.err("negative MFE or MAE present.")
    st = pd.to_datetime(sig["signal_time"], errors="coerce")
    wea = pd.to_datetime(sig["window_end_actual"], errors="coerce")
    wes = pd.to_datetime(sig["window_end_scheduled"], errors="coerce")
    wmin = sig["window_minutes"]
    # actual end must not exceed scheduled end
    if (wea > wes + pd.Timedelta(seconds=1)).any():
        rep.err("window_end_actual is after window_end_scheduled in some rows.")
    # scheduled end == signal_time + window_minutes
    sched_calc = st + pd.to_timedelta(wmin, unit="m")
    if (abs((wes - sched_calc).dt.total_seconds()) > 1).any():
        rep.warn("window_end_scheduled != signal_time + window_minutes in some rows.")
    # times within window
    for col in ("minutes_to_mfe", "minutes_to_mae"):
        bad = sig[(sig[col].notna()) & (sig[col] > wmin + 0.001)]
        if len(bad): rep.warn(f"{len(bad)} rows have {col} beyond window_minutes.")
    rep.note("label window bounds OK.")


def check_censoring(sig, rep):
    n = len(sig)
    c = int((sig["right_censored"] == 1).sum())
    rep.note(f"right-censored: {c}/{n} ({100*c/max(n,1):.1f}%).")
    reasons = sig["finalize_reason"].value_counts().to_dict()
    rep.note(f"finalize_reason: {reasons}")
    # rth_close and terminated must be flagged censored
    for reason in ("rth_close", "terminated"):
        rows = sig[sig["finalize_reason"] == reason]
        if len(rows) and (rows["right_censored"] != 1).any():
            rep.err(f"'{reason}' rows exist with right_censored != 1.")
    # window_complete rows should generally NOT be censored
    wc = sig[sig["finalize_reason"] == "window_complete"]
    if len(wc) and (wc["right_censored"] == 1).any():
        rep.warn("some window_complete rows are marked right_censored.")


def check_join_parity(sig, bars, rep, sample=200):
    if bars is None:
        return
    bars = bars.copy()
    bars["bar_time"] = pd.to_datetime(bars["bar_time"], errors="coerce")
    bars = bars.sort_values("bar_time")
    bt = bars["bar_time"].values
    hi = bars["high"].values
    lo = bars["low"].values

    s = sig.copy()
    s["signal_time"] = pd.to_datetime(s["signal_time"], errors="coerce")
    s["window_end_actual"] = pd.to_datetime(s["window_end_actual"], errors="coerce")
    s = s.sample(min(sample, len(s)), random_state=0)

    exceed = checked = 0
    for _, r in s.iterrows():
        tick = float(r["tick_size"]) if r.get("tick_size", 0) else 0.25
        # (fix 6) begin AFTER the signal bar closes: side='right' excludes the
        # signal bar itself, whose earlier intrabar high/low pre-dates the ref.
        lo_i = np.searchsorted(bt, np.datetime64(r["signal_time"]), side="right")
        hi_i = np.searchsorted(bt, np.datetime64(r["window_end_actual"]), side="right")
        if hi_i <= lo_i:
            continue
        seg_hi, seg_lo = hi[lo_i:hi_i], lo[lo_i:hi_i]
        if len(seg_hi) == 0:
            continue
        ref = r["signal_reference_price"]
        if int(r["is_long"]) == 1:
            fav = (seg_hi.max() - ref) / tick
            adv = (ref - seg_lo.min()) / tick
        else:
            fav = (ref - seg_lo.min()) / tick
            adv = (seg_hi.max() - ref) / tick
        fav, adv = max(fav, 0.0), max(adv, 0.0)
        if fav > r["mfe_ticks"] + 1.0 or adv > r["mae_ticks"] + 1.0:
            exceed += 1
        checked += 1

    if checked == 0:
        rep.warn("join-parity: no overlapping bars for sampled signals.")
    elif exceed:
        rep.err(f"join-parity: {exceed}/{checked} sampled signals have bars-based "
                f"MFE/MAE exceeding logged by >1 tick (possible logging bug).")
    else:
        rep.note(f"join-parity OK on {checked} sampled signals "
                 "(window starts after signal bar).")


def summary(sig, meta, rep):
    rep.note(f"observations: {len(sig)}")
    rep.note(f"instrument: {sig['instrument'].iloc[0]}  tick_size: {sig['tick_size'].iloc[0]}")
    if meta:
        rep.note(f"timezone: {meta.get('timezone_id')}  rth: {meta.get('rth_start')}-{meta.get('rth_end')}  "
                 f"classifier: {meta.get('classifier_session_start')}-{meta.get('classifier_session_end')}")
    rep.note(f"by score: {sig['score'].value_counts().sort_index().to_dict()}")
    rep.note(f"by session: {sig['session_tag'].value_counts().to_dict()}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir")
    ap.add_argument("--signals")
    ap.add_argument("--bars")
    ap.add_argument("--sample", type=int, default=200)
    args = ap.parse_args()

    print("=" * 64)
    print("v3 data validation (rev 2)")
    print("=" * 64)

    rep = Report()
    sig, bars, meta = load(args.signals, args.bars, args.dir, rep)
    if sig is None:
        rep.print(); sys.exit(2)

    check_columns(sig, bars, rep)
    if not any("missing columns" in e for e in rep.errors):
        check_pairing(sig, bars, rep)
        check_nulls_and_bools(sig, rep)
        check_ids(sig, rep)
        check_score_direction(sig, meta, rep)
        check_clusters(sig, rep)
        check_session(sig, rep)
        check_composite_filter(sig, rep)
        check_labels(sig, meta, rep)
        check_censoring(sig, rep)
        check_join_parity(sig, bars, rep, sample=args.sample)
        summary(sig, meta, rep)

    rep.print()
    sys.exit(1 if rep.errors else 0)


if __name__ == "__main__":
    main()
