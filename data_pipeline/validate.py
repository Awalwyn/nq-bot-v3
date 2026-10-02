"""
validate.py — data-quality gate for the v3 logger output (rev 3).

Run after each logging run, BEFORE training. Strict by default: any missing
required artifact, mismatched pairing, or corrupted bar path is an ERROR and
exits non-zero. Use --allow-incomplete only for throwaway exploratory files.

Rev 3 (validator hardening) adds, on top of rev 2:
  * FIX 1  EXACT PAIRING — run_id / instrument / bar_period / tick_size must be
    a single unique value in EACH file and equal across files. A bars file that
    carries an extra (rogue) run now FAILS instead of passing on subset logic.
    No fall-back to an unrelated newest bars file when no exact match exists.
  * FIX 2  STRICT BY DEFAULT — missing bars, missing metadata, a skipped pairing
    or parity check, unparseable timestamps, and any label time beyond the
    window are ERRORS (were warnings / silent). --allow-incomplete downgrades.
  * FIX 3  BAR-PATH INTEGRITY — duplicate/nonchronological/unparseable bar
    timestamps, invalid OHLC, negative volume / missing values / nonpositive
    tick size, a missing signal bar at signal_time, a signal_reference_price
    that differs from the signal bar close beyond tolerance, and window_complete
    rows with no covering bars. Plus: metadata must report clean completion and
    zero write errors.

Usage:
    python -m data_pipeline.validate --dir "C:\\nqbotv3\\data\\training"
    python -m data_pipeline.validate --dir <dir> --allow-incomplete
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
    """Bars file whose CONTENT run_id matches. No fall-back to newest (fix 1)."""
    for p in sorted(glob.glob(os.path.join(dir_path, "bars_*.csv"))):
        try:
            head = pd.read_csv(p, nrows=1)
            if "run_id" in head and str(head["run_id"].iloc[0]) == str(run_id):
                return p
        except Exception:
            continue
    return None


def load(signals_path, bars_path, dir_path, strict, rep):
    if dir_path and not signals_path:
        signals_path = _latest(dir_path, "signals")
    if not signals_path or not os.path.exists(signals_path):
        rep.err(f"signals file not found: {signals_path}")
        return None, None, None
    rep.note(f"signals: {signals_path}")
    sig = pd.read_csv(signals_path, dtype={"signal_id": str, "run_id": str})

    run_id = str(sig["run_id"].iloc[0]) if "run_id" in sig and len(sig) else None
    folder = dir_path or os.path.dirname(signals_path)

    # ---- bars (fix 2: required in strict; fix 1: exact run, no fall-back) ----
    if not bars_path:
        bars_path = _bars_for_run(folder, run_id) if run_id else None
    bars = None
    if bars_path and os.path.exists(bars_path):
        rep.note(f"bars:    {bars_path}")
        bars = pd.read_csv(bars_path, dtype={"run_id": str})
    else:
        msg = (f"bars file for run '{run_id}' not found — pairing + parity "
               "cannot run.")
        (rep.err if strict else rep.warn)(msg)

    # ---- meta (fix 2: required in strict) ----
    meta = None
    if run_id:
        mp = os.path.join(folder, run_id + ".meta.json")
        if os.path.exists(mp):
            try:
                meta = json.load(open(mp))
                rep.note(f"meta:    {os.path.basename(mp)}")
            except Exception as e:
                (rep.err if strict else rep.warn)(f"run metadata unreadable: {e}")
        else:
            (rep.err if strict else rep.warn)(
                f"run metadata {run_id}.meta.json not found.")
    return sig, bars, meta


# --------------------------------------------------------------------------
# FIX 2 — metadata completeness + clean completion
# --------------------------------------------------------------------------

def check_meta(meta, strict, rep):
    if meta is None:
        return  # already errored/warned in load()
    missing = [k for k in schema.META_REQUIRED_KEYS if k not in meta]
    if missing:
        (rep.err if strict else rep.warn)(f"metadata missing keys: {missing}")
    status = str(meta.get("completion_status", "unknown"))
    if status != schema.META_COMPLETION_OK:
        (rep.err if strict else rep.warn)(
            f"run completion_status is '{status}', not "
            f"'{schema.META_COMPLETION_OK}' — the run did not end cleanly.")
    for k in ("signal_write_errors", "bar_write_errors"):
        v = meta.get(k, None)
        if v is None:
            continue
        if int(v) != 0:
            (rep.err if strict else rep.warn)(f"metadata reports {k}={v} (must be 0).")
    if status == schema.META_COMPLETION_OK and not missing:
        rep.note(f"run metadata OK (completed; "
                 f"signals={meta.get('signal_count')} bars={meta.get('bar_count')} "
                 f"write_errors={meta.get('signal_write_errors')}/"
                 f"{meta.get('bar_write_errors')}).")


def check_meta_matches_files(sig, bars, meta, rep):
    """Consistency: metadata identity vs what the CSVs actually contain."""
    if meta is None:
        return
    checks = [("instrument_full", sig, "instrument"),
              ("tick_size", sig, "tick_size"),
              ("bar_period", sig, "bar_period")]
    if bars is not None:
        checks += [("instrument_full", bars, "instrument"),
                   ("tick_size", bars, "tick_size")]
    for mkey, frame, col in checks:
        if mkey not in meta or col not in frame:
            continue
        vals = set(pd.unique(frame[col].dropna()))
        mv = meta[mkey]
        try:
            ok = (len(vals) == 1 and (float(next(iter(vals))) == float(mv)
                  if mkey == "tick_size" else str(next(iter(vals))) == str(mv)))
        except (TypeError, ValueError):
            ok = str(next(iter(vals))) == str(mv)
        if not ok:
            rep.err(f"metadata {mkey}={mv!r} disagrees with {col} in file: {vals}.")
    if not any("metadata" in e for e in rep.errors):
        rep.note("metadata agrees with signals/bars identity.")


# --------------------------------------------------------------------------
# FIX 1 — exact pairing
# --------------------------------------------------------------------------

def check_pairing(sig, bars, strict, rep):
    if bars is None:
        if strict:
            rep.err("pairing check skipped (no bars) — not allowed in strict mode.")
        return
    ok = True
    for col in ("run_id", "instrument", "bar_period", "tick_size"):
        if col not in sig or col not in bars:
            rep.err(f"pairing: column '{col}' missing from one of the files.")
            ok = False
            continue
        sv = set(pd.unique(sig[col].dropna()))
        bv = set(pd.unique(bars[col].dropna()))
        if len(sv) != 1 or len(bv) != 1 or sv != bv:
            rep.err(f"pairing MISMATCH on '{col}': signals unique={sorted(map(str,sv))}, "
                    f"bars unique={sorted(map(str,bv))} — each file must hold exactly "
                    "one matching value (an extra/rogue run fails here).")
            ok = False
    if ok:
        rep.note("pairing OK (run_id / instrument / bar_period / tick_size each "
                 "single-valued and equal across files).")


# --------------------------------------------------------------------------
# FIX 3 — bar-path integrity
# --------------------------------------------------------------------------

def check_bar_integrity(bars, strict, rep):
    if bars is None:
        return
    n = len(bars)
    # required non-null
    for c in schema.BARS_REQUIRED_NON_NULL:
        if c not in bars:
            rep.err(f"bars missing required column '{c}'.")
        elif bars[c].isna().any():
            rep.err(f"bars '{c}' has {int(bars[c].isna().sum())} null(s).")
    if any("missing required column" in e for e in rep.errors):
        return
    # non-numeric values in numeric columns (coerce hides them as NaN otherwise)
    for c in ["open", "high", "low", "close", "volume", "tick_size"]:
        coerced = pd.to_numeric(bars[c], errors="coerce")
        nonnum = int((coerced.isna() & bars[c].notna()).sum())
        if nonnum:
            rep.err(f"{nonnum} non-numeric value(s) in bars '{c}'.")
        arr = coerced.to_numpy(dtype="float64", na_value=np.nan)
        if np.isinf(arr).any():
            rep.err(f"bars '{c}' contains infinite value(s).")
    # tick size positive
    if (pd.to_numeric(bars["tick_size"], errors="coerce") <= 0).any():
        rep.err("bars have nonpositive tick_size.")
    # volume non-negative
    if (pd.to_numeric(bars["volume"], errors="coerce") < 0).any():
        rep.err("bars have negative volume.")
    # timestamps parseable
    bt = pd.to_datetime(bars["bar_time"], errors="coerce")
    n_bad = int(bt.isna().sum() - bars["bar_time"].isna().sum())
    if n_bad > 0:
        rep.err(f"{n_bad} bar_time value(s) are unparseable.")
    # duplicates within run+instrument
    dup = bars.duplicated(subset=["run_id", "instrument", "bar_time"]).sum()
    if dup:
        rep.err(f"{int(dup)} duplicate bar timestamp(s) within run+instrument.")
    # chronology per instrument (monotonic non-decreasing as written)
    tmp = bars.assign(_bt=bt)
    nonchrono = 0
    for _, idx in tmp.groupby("instrument").groups.items():
        sub = tmp.loc[idx, "_bt"]
        if not sub.is_monotonic_increasing:
            nonchrono += 1
    if nonchrono:
        rep.err(f"{nonchrono} instrument(s) have non-chronological bar_time order.")
    # OHLC validity
    o = pd.to_numeric(bars["open"], errors="coerce")
    h = pd.to_numeric(bars["high"], errors="coerce")
    l = pd.to_numeric(bars["low"], errors="coerce")
    c = pd.to_numeric(bars["close"], errors="coerce")
    eps = 1e-9
    bad_ohlc = ((h < o - eps) | (h < c - eps) | (h < l - eps) |
                (l > o + eps) | (l > c + eps)).sum()
    if bad_ohlc:
        rep.err(f"{int(bad_ohlc)} bar(s) have invalid OHLC "
                "(high below open/close/low, or low above open/close).")
    if not any("bar" in e for e in rep.errors):
        rep.note(f"bar-path integrity OK ({n} bars: timestamps, OHLC, volume, tick).")


def check_signal_bar_and_refprice(sig, bars, strict, rep):
    """Every signal must have its signal bar present, and signal_reference_price
    must equal that bar's close within tolerance (fix 3)."""
    if bars is None:
        return
    b = bars[["instrument", "bar_time", "close"]].copy()
    b["_t"] = pd.to_datetime(b["bar_time"], errors="coerce")
    b = b.dropna(subset=["_t"]).drop_duplicates(subset=["instrument", "_t"])
    s = sig[["instrument", "signal_time", "signal_reference_price", "tick_size"]].copy()
    s["_t"] = pd.to_datetime(s["signal_time"], errors="coerce")
    merged = s.merge(b[["instrument", "_t", "close"]], on=["instrument", "_t"], how="left")
    missing = int(merged["close"].isna().sum())
    if missing:
        rep.err(f"{missing} signal(s) have no matching signal bar at signal_time.")
    ok = merged.dropna(subset=["close"])
    if len(ok):
        tick = ok["tick_size"].replace(0, np.nan).fillna(0.25)
        drift = (ok["signal_reference_price"] - ok["close"]).abs() / tick
        off = int((drift > schema.REF_PRICE_TOLERANCE_TICKS).sum())
        if off:
            rep.err(f"{off} signal(s) have signal_reference_price differing from the "
                    f"signal bar close by > {schema.REF_PRICE_TOLERANCE_TICKS} tick.")
    if not missing and (not len(ok) or off == 0):
        rep.note("signal bar present and reference_price == signal-bar close (within tol).")


def check_window_coverage(sig, bars, rep):
    """A window_complete observation must have at least one covering bar in
    (signal_time, window_end_actual]; zero is an unexplained gap (fix 3)."""
    if bars is None:
        return
    bt = np.sort(pd.to_datetime(bars["bar_time"], errors="coerce").dropna().values)
    if len(bt) == 0:
        rep.err("no parseable bar timestamps for coverage check.")
        return
    last_bar = bt[-1] if len(bt) else None
    wc = sig[sig["finalize_reason"] == "window_complete"].copy()
    wc["_s"] = pd.to_datetime(wc["signal_time"], errors="coerce")
    wc["_e"] = pd.to_datetime(wc["window_end_actual"], errors="coerce")
    empty = 0; zerolen = 0
    for _, r in wc.iterrows():
        if pd.isna(r["_e"]) or r["_e"] <= r["_s"]:
            zerolen += 1
            continue  # degenerate zero-length window (signal at a session boundary)
        if last_bar is not None and np.datetime64(r["_e"]) > last_bar:
            continue  # end-of-run: window extends past the last written bar
        lo_i = np.searchsorted(bt, np.datetime64(r["_s"]), side="right")
        hi_i = np.searchsorted(bt, np.datetime64(r["_e"]), side="left") + 1  # include the bar containing wea (close-stamped)
        if hi_i - lo_i <= 0:
            empty += 1
    if empty:
        rep.err(f"{empty} window_complete observation(s) have no covering bars "
                "(unexplained gap — bars file may be incomplete).")
    else:
        msg = f"window coverage OK ({len(wc)} completed windows all have bars)."
        if zerolen:
            msg += f" ({zerolen} zero-length boundary window(s) noted.)"
        rep.note(msg)
    if zerolen:
        rep.warn(f"{zerolen} window_complete row(s) have a zero-length window "
                 "(signal fired at a session/maintenance boundary; no label signal). "
                 "Consider censoring these in the logger.")


# --------------------------------------------------------------------------
# carried-over rev-2 checks (some promoted to errors under strict)
# --------------------------------------------------------------------------

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


def check_timestamps(sig, strict, rep):
    """Unparseable signal timestamps are errors in strict mode (fix 2)."""
    for col in ("signal_time", "window_end_actual", "window_end_scheduled"):
        if col not in sig:
            continue
        parsed = pd.to_datetime(sig[col], errors="coerce")
        bad = int(parsed.isna().sum() - sig[col].isna().sum())
        if bad > 0:
            (rep.err if strict else rep.warn)(f"{bad} unparseable '{col}' value(s).")


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
    if len(runs) != 1: rep.err(f"{len(runs)} run_ids in signals (expected exactly 1).")


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


def check_labels(sig, strict, rep):
    if (sig["mfe_ticks"] < 0).any() or (sig["mae_ticks"] < 0).any():
        rep.err("negative MFE or MAE present.")
    st = pd.to_datetime(sig["signal_time"], errors="coerce")
    wea = pd.to_datetime(sig["window_end_actual"], errors="coerce")
    wes = pd.to_datetime(sig["window_end_scheduled"], errors="coerce")
    wmin = sig["window_minutes"]
    if (wea > wes + pd.Timedelta(seconds=1)).any():
        rep.err("window_end_actual is after window_end_scheduled in some rows.")
    sched_calc = st + pd.to_timedelta(wmin, unit="m")
    if (abs((wes - sched_calc).dt.total_seconds()) > 1).any():
        (rep.err if strict else rep.warn)(
            "window_end_scheduled != signal_time + window_minutes in some rows.")
    # label times beyond the window are ERRORS in strict (fix 2)
    for col in ("minutes_to_mfe", "minutes_to_mae"):
        bad = sig[(sig[col].notna()) & (sig[col] > wmin + 0.001)]
        if len(bad):
            (rep.err if strict else rep.warn)(
                f"{len(bad)} rows have {col} beyond window_minutes.")
    rep.note("label window bounds checked.")


def check_meta_counts(sig, bars, meta, rep):
    """Reconcile the counts the logger reported against the actual files, and
    require zero close-write errors. Catches a deleted/added row (count drift)."""
    if meta is None:
        return
    ok = True
    if "signal_count" in meta and int(meta["signal_count"]) != len(sig):
        rep.err(f"metadata signal_count={meta['signal_count']} != actual signal rows {len(sig)}."); ok = False
    if bars is not None and "bar_count" in meta and int(meta["bar_count"]) != len(bars):
        rep.err(f"metadata bar_count={meta['bar_count']} != actual bar rows {len(bars)} "
                "(a bar row was added or deleted)."); ok = False
    if int(meta.get("close_write_errors", 0)) != 0:
        rep.err(f"metadata close_write_errors={meta.get('close_write_errors')} (must be 0)."); ok = False
    if ok:
        rep.note("metadata counts reconcile with files; close errors 0.")


def check_bar_period(sig, bars, meta, rep):
    exp = schema.EXPECTED_BAR_PERIOD
    seen = set(pd.unique(sig["bar_period"].dropna().astype(str)))
    if bars is not None:
        seen |= set(pd.unique(bars["bar_period"].dropna().astype(str)))
    if meta and "bar_period" in meta:
        seen |= {str(meta["bar_period"])}
    bad = {s for s in seen if s != exp}
    if bad:
        rep.err(f"bar_period {sorted(bad)} != expected '{exp}' — wrong timeframe collected.")
    else:
        rep.note(f"bar_period == '{exp}' across files and metadata.")


def check_finalize_reason(sig, rep):
    bad = set(pd.unique(sig["finalize_reason"].dropna().astype(str))) - schema.VALID_FINALIZE_REASONS
    if bad:
        rep.err(f"invalid finalize_reason value(s): {sorted(bad)}.")


def check_direction(sig, rep):
    d = sig["direction"].astype(str).str.lower()
    bad_val = int((~d.isin(schema.VALID_DIRECTIONS)).sum())
    if bad_val:
        rep.err(f"{bad_val} rows have invalid direction text (not long/short).")
    mism = int((((d == "long") & (sig["is_long"] != 1)) |
                ((d == "short") & (sig["is_long"] != 0))).sum())
    if mism:
        rep.err(f"{mism} rows have direction text reversed vs is_long.")
    if not bad_val and not mism:
        rep.note("direction text valid and consistent with is_long.")


def check_final_delta(sig, rep):
    dirn = np.where(sig["is_long"] == 1, 1.0, -1.0)
    tick = pd.to_numeric(sig["tick_size"], errors="coerce").replace(0, np.nan)
    expect = (pd.to_numeric(sig["final_price"], errors="coerce") -
              pd.to_numeric(sig["signal_reference_price"], errors="coerce")) / tick * dirn
    diff = (pd.to_numeric(sig["final_delta_ticks"], errors="coerce") - expect).abs()
    bad = int((diff > schema.FINAL_DELTA_TOLERANCE_TICKS).sum())
    if bad:
        rep.err(f"{bad} rows: final_delta_ticks != (final_price - reference)/tick * direction "
                "(fabricated final delta).")
    else:
        rep.note("final_delta_ticks consistent with final_price and reference.")


def check_minutes_nonneg(sig, rep):
    st = pd.to_datetime(sig["signal_time"], errors="coerce")
    wea = pd.to_datetime(sig["window_end_actual"], errors="coerce")
    covered = (wea - st).dt.total_seconds() / 60.0
    neg_cov = int((covered < -0.001).sum())
    if neg_cov:
        rep.err(f"{neg_cov} rows: window_end_actual is before signal_time (invalid window).")
    wmin = pd.to_numeric(sig["window_minutes"], errors="coerce")
    bad_neg = bad_over = 0
    for c in ("minutes_to_mfe", "minutes_to_mae"):
        v = pd.to_numeric(sig[c], errors="coerce")
        bad_neg += int((v < 0).sum())
        bad_over += int((v.notna() & ((v > covered + 0.5) | (v > wmin + 0.001))).sum())
    if bad_neg:
        rep.err(f"{bad_neg} negative minutes_to_mfe/mae value(s).")
    if bad_over:
        rep.err(f"{bad_over} minutes_to_mfe/mae beyond the covered/forward window.")
    if not bad_neg and not bad_over:
        rep.note("minutes_to_mfe/mae non-negative and within the window.")


def check_mae_before_mfe(sig, rep):
    tmae = pd.to_numeric(sig["minutes_to_mae"], errors="coerce")
    tmfe = pd.to_numeric(sig["minutes_to_mfe"], errors="coerce")
    mae0 = pd.to_numeric(sig["mae_ticks"], errors="coerce")
    mfe0 = pd.to_numeric(sig["mfe_ticks"], errors="coerce")
    inf = float("inf")
    t_mae = tmae.where(mae0 > 0, inf)
    t_mfe = tmfe.where(mfe0 > 0, inf)
    both_absent = np.isinf(t_mae) & np.isinf(t_mfe)
    expect = ((t_mae < t_mfe) & ~both_absent).astype(int)
    got = pd.to_numeric(sig["mae_before_mfe"], errors="coerce").fillna(-1).astype(int)
    bad = int((got != expect).sum())
    if bad:
        rep.err(f"{bad} rows: mae_before_mfe disagrees with minutes_to_mae < minutes_to_mfe (flipped).")
    else:
        rep.note("mae_before_mfe consistent with excursion timing.")


def check_session_tag(sig, meta, rep):
    lo = int(meta["rth_start"]) if meta and "rth_start" in meta else 830
    hi = int(meta["rth_end"]) if meta and "rth_end" in meta else 1500
    st = pd.to_datetime(sig["signal_time"], errors="coerce")
    hhmm = st.dt.hour * 100 + st.dt.minute
    in_rth = ((hhmm >= lo) & (hhmm <= hi)) if lo <= hi else ((hhmm >= lo) | (hhmm <= hi))
    expect = np.where(in_rth, "RTH", "OVERNIGHT")
    bad = int((sig["session_tag"].astype(str).values != expect).sum())
    if bad:
        rep.err(f"{bad} rows: session_tag inconsistent with signal_time vs RTH [{lo},{hi}] (falsified).")
    else:
        rep.note("session_tag consistent with signal_time.")


def check_session_date(sig, rep):
    st = pd.to_datetime(sig["signal_time"], errors="coerce")
    sd = pd.to_datetime(sig["session_date"], errors="coerce")
    diff = (sd - st.dt.normalize()).dt.total_seconds() / 86400.0
    bad = int(((diff < -0.001) | (diff > 1.001)).sum())
    if bad:
        rep.err(f"{bad} rows: session_date not within [signal date, +1 trading day] (falsified).")
    else:
        rep.note("session_date within the trading-day range of signal_time.")


def check_horizons_censoring(sig, rep):
    st = pd.to_datetime(sig["signal_time"], errors="coerce")
    wea = pd.to_datetime(sig["window_end_actual"], errors="coerce")
    covered = (wea - st).dt.total_seconds() / 60.0
    bad = 0
    for col, mn in schema.HORIZON_COLUMNS.items():
        populated = pd.to_numeric(sig[col], errors="coerce").notna()
        bad += int((populated & (mn > covered + 0.5)).sum())
    if bad:
        rep.err(f"{bad} horizon value(s) populated beyond the covered window "
                "(future horizon fabricated after censoring).")
    else:
        rep.note("no horizon deltas populated past the window end.")


def check_final_price_range(sig, bars, rep, sample=1000):
    if bars is None:
        return
    b = bars[["bar_time", "low", "high"]].copy()
    b["_t"] = pd.to_datetime(b["bar_time"], errors="coerce")
    b = b.dropna(subset=["_t"]).sort_values("_t")
    bt = b["_t"].values
    blo = pd.to_numeric(b["low"], errors="coerce").values
    bhi = pd.to_numeric(b["high"], errors="coerce").values
    last_bar = bt[-1] if len(bt) else None
    s = sig.copy()
    s["_e"] = pd.to_datetime(s["window_end_actual"], errors="coerce")
    s["_s"] = pd.to_datetime(s["signal_time"], errors="coerce")
    if len(s) > sample:
        s = s.sample(sample, random_state=0)
    bad = 0; skipped_eor = 0
    for _, r in s.iterrows():
        if pd.isna(r["_e"]) or r["_e"] <= r["_s"]:
            continue  # zero-length window carries no final price to check
        # end-of-run: the final tick can land after the last WRITTEN bar if the
        # run stopped mid-interval, so the containing bar was never flushed.
        if last_bar is not None and np.datetime64(r["_e"]) > last_bar:
            skipped_eor += 1
            continue
        # NinjaTrader stamps each bar at its interval CLOSE, so the bar CONTAINING
        # a tick time t is the first bar whose timestamp >= t (side=left). A tick
        # exactly on a grid boundary can belong to the adjacent bar, so accept the
        # final price within the containing bar OR its immediate neighbours.
        j = int(np.searchsorted(bt, np.datetime64(r["_e"]), side="left"))
        if j >= len(bt):
            j = len(bt) - 1
        if j < 0:
            continue
        tol = 2 * (float(r["tick_size"]) if r.get("tick_size", 0) else 0.25)
        lo_env = blo[max(0, j - 1):j + 2].min()
        hi_env = bhi[max(0, j - 1):j + 2].max()
        if r["final_price"] < lo_env - tol or r["final_price"] > hi_env + tol:
            bad += 1
    if bad:
        rep.err(f"{bad} sampled rows: final_price outside the bar range at window_end_actual "
                "(fabricated final price).")
    else:
        note = "final_price within the bar range at window end"
        if skipped_eor:
            note += f" ({skipped_eor} end-of-run rows skipped — final tick after last bar)"
        rep.note(note + ".")


def check_pilot_config(meta, pilot, expect_template, expect_timezone, rep):
    """Under --pilot, the run metadata must match the locked pilot config, so a
    self-consistent but wrong-config run (e.g. a 15-minute dataset) can't pass."""
    if not pilot or meta is None:
        return
    ok = True
    for k, want in schema.PILOT_CONFIG.items():
        got = meta.get(k, None)
        if isinstance(want, bool):
            match = bool(got) == want
        elif isinstance(want, (int, float)):
            try:
                match = abs(float(got) - float(want)) < 1e-9
            except (TypeError, ValueError):
                match = False
        else:
            match = str(got) == str(want)
        if not match:
            rep.err(f"pilot config mismatch: {k}={got!r} (must be {want!r})."); ok = False
    # Trading Hours template must be pinned for an official pilot.
    want_tpl = expect_template or schema.EXPECTED_TRADING_HOURS_TEMPLATE
    if want_tpl is None:
        rep.err("pilot: Trading Hours template is not locked — choose it and pass "
                "--expect-template \"<name>\" (or set EXPECTED_TRADING_HOURS_TEMPLATE)."); ok = False
    elif str(meta.get("trading_hours_template")) != str(want_tpl):
        rep.err(f"pilot: trading_hours_template={meta.get('trading_hours_template')!r} "
                f"!= expected {want_tpl!r}."); ok = False
    # Timezone must be the expected Central ID — a wrong chart timezone shifts the
    # whole session by an hour even when RTH integers look right.
    want_tz = expect_timezone or schema.EXPECTED_TIMEZONE_ID
    if want_tz is None:
        rep.err("pilot: expected timezone is not locked — set EXPECTED_TIMEZONE_ID "
                "or pass --expect-timezone \"<id>\"."); ok = False
    elif str(meta.get("timezone_id")) != str(want_tz):
        rep.err(f"pilot: timezone_id={meta.get('timezone_id')!r} != expected {want_tz!r} "
                "(wrong chart timezone shifts the session)."); ok = False
    if ok:
        rep.note("pilot configuration matches the locked pilot spec (config, template, timezone).")


def check_signal_bar_session(sig, bars, rep):
    """A signal and its own signal bar must carry the same session tag/date."""
    if bars is None:
        return
    b = bars[["instrument", "bar_time", "session_tag", "session_date"]].copy()
    b["_t"] = pd.to_datetime(b["bar_time"], errors="coerce")
    b = b.dropna(subset=["_t"]).drop_duplicates(["instrument", "_t"])
    s = sig[["instrument", "signal_time", "session_tag", "session_date"]].copy()
    s["_t"] = pd.to_datetime(s["signal_time"], errors="coerce")
    m = s.merge(b, on=["instrument", "_t"], how="left", suffixes=("_sig", "_bar"))
    got = m.dropna(subset=["session_tag_bar"])
    bad = int(((got["session_tag_sig"].astype(str) != got["session_tag_bar"].astype(str)) |
               (got["session_date_sig"].astype(str) != got["session_date_bar"].astype(str))).sum())
    if bad:
        rep.err(f"{bad} signal(s) disagree with their signal bar on session tag/date.")
    else:
        rep.note("signal and signal-bar session fields agree.")


def check_rth_cadence(sig, bars, meta, rep):
    """Within RTH the 15s grid should be continuous. Flag interior gaps (a few
    missing bars) during RTH; large gaps are treated as session breaks."""
    if bars is None:
        return
    lo = int(meta["rth_start"]) if meta and "rth_start" in meta else 830
    hi = int(meta["rth_end"]) if meta and "rth_end" in meta else 1500
    bt = pd.to_datetime(bars["bar_time"], errors="coerce").dropna().sort_values()
    hhmm = bt.dt.hour * 100 + bt.dt.minute
    rth = bt[(hhmm >= lo) & (hhmm <= hi)]
    if len(rth) < 3:
        return
    diffs = rth.diff().dt.total_seconds().dropna()
    # interior gap: between 2x and 20x the 15s step (30s..300s); larger = break
    gaps = int(((diffs > 22.5) & (diffs <= 300) & (diffs.index.to_series().diff().notna())).sum())
    interior = int(((diffs > 22.5) & (diffs <= 300)).sum())
    if interior:
        rep.warn(f"{interior} interior gap(s) in the RTH 15s grid (possible missing bars; "
                 "confirm against the trading-hours template).")
    else:
        rep.note("RTH 15s bar cadence continuous.")


def check_tick_fidelity(sig, meta, pilot, rep):
    """MFE/MAE and their timing come from the tick series. Reject fabricated
    (zero-tick) window_complete labels, report full tick statistics, and in pilot
    mode fail on implausibly sparse activity or tick coverage that doesn't span
    the bar range."""
    if "tick_updates" not in sig:
        rep.warn("no tick_updates column — cannot assess tick fidelity "
                 "(older logger). Re-collect with the current logger.")
        return
    tu = pd.to_numeric(sig["tick_updates"], errors="coerce").fillna(0)
    st = pd.to_datetime(sig["signal_time"], errors="coerce")
    wea = pd.to_datetime(sig["window_end_actual"], errors="coerce")
    positive_dur = (wea - st).dt.total_seconds() > 0
    wc = sig["finalize_reason"] == "window_complete"
    # A zero-length window legitimately has 0 ticks; only a REAL (positive-length)
    # window with 0 ticks means the tick series never fed it (fabricated labels).
    dead = int(((tu <= 0) & wc & positive_dur).sum())
    if dead:
        rep.err(f"{dead} window_complete observation(s) with a real window have 0 tick "
                "updates — the tick series did not feed them; MFE/MAE/timing are invalid. "
                "Likely no historical tick data (enable Tick Replay / check feed).")
    zerolen_dead = int(((tu <= 0) & wc & ~positive_dur).sum())
    if zerolen_dead:
        rep.warn(f"{zerolen_dead} zero-length window_complete row(s) with 0 ticks "
                 "(signal at a session boundary; no label signal).")
    wmin = pd.to_numeric(sig["window_minutes"], errors="coerce").replace(0, np.nan)
    per_min = (tu / wmin).replace([np.inf, -np.inf], np.nan).dropna()
    if len(per_min):
        med = float(per_min.median())
        low_frac = float((per_min < schema.PILOT_MIN_TICKS_PER_MIN).mean())
        rep.note(f"tick density: min {tu.min():.0f} / median {med:.1f}/min / max {tu.max():.0f}; "
                 f"{100*low_frac:.1f}% of windows below {schema.PILOT_MIN_TICKS_PER_MIN}/min.")
        if pilot and med < schema.PILOT_MIN_TICKS_PER_MIN:
            rep.err(f"pilot: median tick density {med:.2f}/min is below the "
                    f"{schema.PILOT_MIN_TICKS_PER_MIN}/min floor (ticks likely synthesized).")
    # coverage: metadata tick span should cover the bar span
    if meta:
        rep.note(f"total tick_count (meta): {meta.get('tick_count')}; "
                 f"tick span {meta.get('first_tick_time')} .. {meta.get('last_tick_time')}.")
        try:
            fb = pd.to_datetime(meta.get("first_bar_time")); lb = pd.to_datetime(meta.get("last_bar_time"))
            ft = pd.to_datetime(meta.get("first_tick_time")); lt = pd.to_datetime(meta.get("last_tick_time"))
            if pd.notna(fb) and pd.notna(ft) and (ft > fb + pd.Timedelta(minutes=1) or lt < lb - pd.Timedelta(minutes=1)):
                (rep.err if pilot else rep.warn)(
                    "tick coverage does not span the bar range (missing ticks at the edges).")
        except Exception:
            pass


def check_censoring(sig, rep):
    n = len(sig)
    c = int((sig["right_censored"] == 1).sum())
    rep.note(f"right-censored: {c}/{n} ({100*c/max(n,1):.1f}%).")
    reasons = sig["finalize_reason"].value_counts().to_dict()
    rep.note(f"finalize_reason: {reasons}")
    for reason in ("rth_close", "terminated"):
        rows = sig[sig["finalize_reason"] == reason]
        if len(rows) and (rows["right_censored"] != 1).any():
            rep.err(f"'{reason}' rows exist with right_censored != 1.")
    wc = sig[sig["finalize_reason"] == "window_complete"]
    if len(wc) and (wc["right_censored"] == 1).any():
        rep.err("window_complete rows exist with right_censored == 1 (must be 0).")


def check_join_parity(sig, bars, strict, rep, sample=200):
    if bars is None:
        return
    bars = bars.copy()
    bars["bar_time"] = pd.to_datetime(bars["bar_time"], errors="coerce")
    bars = bars.sort_values("bar_time")
    bt = bars["bar_time"].values
    hi = bars["high"].values
    lo = bars["low"].values

    last_bar = bt[-1] if len(bt) else None
    s = sig.copy()
    s["signal_time"] = pd.to_datetime(s["signal_time"], errors="coerce")
    s["window_end_actual"] = pd.to_datetime(s["window_end_actual"], errors="coerce")
    s = s.sample(min(sample, len(s)), random_state=0)

    exceed = checked = inflated = skipped_eor = 0
    for _, r in s.iterrows():
        tick = float(r["tick_size"]) if r.get("tick_size", 0) else 0.25
        if pd.isna(r["window_end_actual"]) or r["window_end_actual"] <= r["signal_time"]:
            continue  # zero-length window: no forward path to reconstruct
        # end-of-run: wea is a tick time that can fall after the last WRITTEN bar
        # (run stopped mid-interval); the covering bars don't exist to compare.
        if last_bar is not None and np.datetime64(r["window_end_actual"]) > last_bar:
            skipped_eor += 1
            continue
        # Close-stamped bars. The bar that CONTAINS wea closes AFTER wea, so it
        # also holds ticks the logger correctly excluded. Use two envelopes:
        #   inner = bars fully within (signal_time, wea] (close <= wea, side=right)
        #           — the logger saw every tick here, so these bars must NOT reveal
        #           more excursion than logged (else a real understating bug).
        #   outer = inner + partial/containing bar + one boundary neighbour
        #           — logged ticks cannot exceed this, so logged > outer = inflation.
        ref = r["signal_reference_price"]
        lo_i = np.searchsorted(bt, np.datetime64(r["signal_time"]), side="right")
        hi_inner = np.searchsorted(bt, np.datetime64(r["window_end_actual"]), side="right")
        hi_outer = np.searchsorted(bt, np.datetime64(r["window_end_actual"]), side="left") + 2
        def excursions(a, bidx_hi):
            seg_hi, seg_lo = hi[a:bidx_hi], lo[a:bidx_hi]
            if len(seg_hi) == 0:
                return None
            if int(r["is_long"]) == 1:
                return max((seg_hi.max() - ref) / tick, 0.0), max((ref - seg_lo.min()) / tick, 0.0)
            return max((ref - seg_lo.min()) / tick, 0.0), max((seg_hi.max() - ref) / tick, 0.0)
        inner = excursions(lo_i, hi_inner) if hi_inner > lo_i else None
        outer = excursions(lo_i, hi_outer) if hi_outer > lo_i else None
        if inner is not None and (inner[0] > r["mfe_ticks"] + 1.0 or inner[1] > r["mae_ticks"] + 1.0):
            exceed += 1
        if outer is not None and (r["mfe_ticks"] > outer[0] + 1.0 or r["mae_ticks"] > outer[1] + 1.0):
            inflated += 1
        checked += 1

    if checked == 0:
        (rep.err if strict else rep.warn)(
            "join-parity: no overlapping bars for sampled signals "
            "(parity could not be verified).")
    elif inflated:
        rep.err(f"join-parity: {inflated}/{checked} sampled signals have logged MFE/MAE "
                f"exceeding the bar range by >1 tick (inflated / fabricated labels).")
    elif exceed:
        rep.err(f"join-parity: {exceed}/{checked} sampled signals have bars-based "
                f"MFE/MAE exceeding logged by >1 tick (possible logging bug).")
    else:
        note = f"join-parity OK on {checked} sampled signals (logged excursions bounded by bar range)"
        if skipped_eor:
            note += f"; {skipped_eor} end-of-run rows skipped"
        rep.note(note + ".")


def summary(sig, meta, rep):
    rep.note(f"observations: {len(sig)}")
    rep.note(f"instrument: {sig['instrument'].iloc[0]}  tick_size: {sig['tick_size'].iloc[0]}")
    if meta:
        rep.note(f"timezone: {meta.get('timezone_id')}  rth: {meta.get('rth_start')}-{meta.get('rth_end')}  "
                 f"classifier: {meta.get('classifier_session_start')}-{meta.get('classifier_session_end')}")
    rep.note(f"by session: {sig['session_tag'].value_counts().to_dict()}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir")
    ap.add_argument("--signals")
    ap.add_argument("--bars")
    ap.add_argument("--sample", type=int, default=200)
    ap.add_argument("--allow-incomplete", action="store_true",
                    help="downgrade missing-artifact / skipped-check errors to "
                         "warnings (exploratory only; NOT for pilot/production).")
    ap.add_argument("--pilot", action="store_true",
                    help="pilot gate: strict + exhaustive parity (no sampling) + "
                         "locked config + tick-density floor. Use for the 5-day pilot.")
    ap.add_argument("--expect-template", default=None,
                    help="exact Trading Hours template name the pilot must match "
                         "(required for --pilot unless set in schema).")
    ap.add_argument("--expect-timezone", default=None,
                    help="exact timezone_id the pilot must match "
                         "(default: schema.EXPECTED_TIMEZONE_ID, Central Standard Time).")
    args = ap.parse_args()

    strict = not args.allow_incomplete
    pilot = args.pilot
    if pilot:
        strict = True
    print("=" * 64)
    mode = "PILOT (exhaustive)" if pilot else ("STRICT" if strict else "lenient (--allow-incomplete)")
    print(f"v3 data validation (rev 4) — {mode}")
    print("=" * 64)

    rep = Report()
    sig, bars, meta = load(args.signals, args.bars, args.dir, strict, rep)
    if sig is None:
        rep.print(); sys.exit(2)

    check_columns(sig, bars, rep)
    if not any("missing columns" in e for e in rep.errors):
        sample = len(sig) if pilot else args.sample
        check_meta(meta, strict, rep)
        check_meta_matches_files(sig, bars, meta, rep)
        check_meta_counts(sig, bars, meta, rep)
        check_pilot_config(meta, pilot, args.expect_template, args.expect_timezone, rep)
        check_bar_period(sig, bars, meta, rep)
        check_pairing(sig, bars, strict, rep)
        check_bar_integrity(bars, strict, rep)
        check_signal_bar_and_refprice(sig, bars, strict, rep)
        check_signal_bar_session(sig, bars, rep)
        check_window_coverage(sig, bars, rep)
        check_rth_cadence(sig, bars, meta, rep)
        check_final_price_range(sig, bars, rep, sample=sample)
        check_timestamps(sig, strict, rep)
        check_nulls_and_bools(sig, rep)
        check_ids(sig, rep)
        check_score_direction(sig, meta, rep)
        check_direction(sig, rep)
        check_finalize_reason(sig, rep)
        check_final_delta(sig, rep)
        check_minutes_nonneg(sig, rep)
        check_mae_before_mfe(sig, rep)
        check_session_tag(sig, meta, rep)
        check_session_date(sig, rep)
        check_horizons_censoring(sig, rep)
        check_clusters(sig, rep)
        check_session(sig, rep)
        check_composite_filter(sig, rep)
        check_labels(sig, strict, rep)
        check_tick_fidelity(sig, meta, pilot, rep)
        check_censoring(sig, rep)
        check_join_parity(sig, bars, strict, rep, sample=sample)
        summary(sig, meta, rep)

    rep.print()
    sys.exit(1 if rep.errors else 0)


if __name__ == "__main__":
    main()
