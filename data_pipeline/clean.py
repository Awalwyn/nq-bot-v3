"""
clean.py — Phase 2, step 1: turn a validated raw run into a clean, split,
training-ready dataset. Deterministic; every row removed is logged with a reason.

Principles (see docs/decisions/0013-phase2-cleaning-and-holdout.md):

  1. Rows are dropped because the DATA is broken, never because an OUTCOME is
     extreme. Every integrity rule here compares two independent measurements
     (tick-replay labels vs the 15s bars file) or checks coverage; none of them
     looks at how big a move was.
  2. A bad print contaminates more than its own row: features computed after it
     (ATR, range, kernel...) can carry it. So a gross failure quarantines its
     session from the first window that could have touched it to session end.
  3. The bot does not force-close at 15:00, so the logger's 15:00 RTH cut is
     NOT the real outcome. Those windows are rebuilt to the full 60 minutes from
     the bars file (which runs to the 16:00 halt). The rebuild procedure is
     validated against tick labels on uncensored rows and the agreement is
     written to the manifest.
  4. The holdout is split off HERE, physically, before any analysis. It lives in
     its own file and dataset.load_clean() refuses it without allow_holdout=True.

Usage:
    python -m data_pipeline.clean \
        --signals signals_<run>.csv|.parquet --bars bars_<run>.csv|.parquet \
        --meta <run>.meta.json --out data/clean
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data_pipeline import schema  # noqa: E402

# ---------------------------------------------------------------------------
# configuration (recorded verbatim in the manifest)
# ---------------------------------------------------------------------------
CONFIG = {
    # bar/tick agreement: a row "fails" if |bar label - tick label| exceeds this
    "xsource_tol_ticks": 4,
    # a failure this large is a corrupt print -> quarantine the session from the
    # first window that could have touched it to session end
    "gross_fail_ticks": 12,
    # CME trade date rolls at 17:00 Central (data timestamps are Central, ADR 0010)
    "cme_roll_hour_ct": 17,
    # extension of 15:00-cut RTH windows: require bars to cover the full window
    # with no internal hole longer than this
    "extend_max_internal_gap_s": 300,
    # locked holdout: CME sessions on/after this date
    "holdout_start_session": "2026-07-01",
    # embargo: sessions removed between dev and holdout
    "embargo_sessions": ["2026-06-30"],
    # rows used to validate the bar-rebuild procedure against tick labels
    "rebuild_validation_rows": 20000,
}

# Columns removed from the clean dataset entirely, so they cannot be used by
# accident. (Absolute price levels stay in the file for audit, but are excluded
# from features in schema.EXCLUDED_FEATURES.)
DROP_COLUMNS = {
    "dist_vwap_ticks": "VWAP reset on the wrong session boundary (template 'Default 24 x 5'); "
                       "re-add only when NT8 computes it identically live",
}

HORIZONS = [1, 3, 5, 10, 15, 30, 60]


# ---------------------------------------------------------------------------
# io
# ---------------------------------------------------------------------------

def _read(path: str, **kw) -> pd.DataFrame:
    if path.endswith(".parquet"):
        return pd.read_parquet(path)
    return pd.read_csv(path, encoding="utf-8-sig", low_memory=False, **kw)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def load_inputs(signals_path, bars_path):
    s = _read(signals_path, dtype={"signal_id": str, "run_id": str})
    for c in ("signal_time", "window_end_scheduled", "window_end_actual"):
        s[c] = pd.to_datetime(s[c], errors="coerce")
    b = _read(bars_path)
    b["bar_time"] = pd.to_datetime(b["bar_time"], errors="coerce")
    b = b.sort_values("bar_time").reset_index(drop=True)
    return s, b


# ---------------------------------------------------------------------------
# bar-path helpers
# ---------------------------------------------------------------------------

class BarPath:
    """Fast forward-window lookups on the close-stamped 15s bars."""

    def __init__(self, bars: pd.DataFrame, tick: float):
        self.t = bars["bar_time"].values.astype("datetime64[ns]")
        self.o = bars["open"].values.astype(float)
        self.h = bars["high"].values.astype(float)
        self.l = bars["low"].values.astype(float)
        self.c = bars["close"].values.astype(float)
        self.tick = tick

    def span(self, t0, t1):
        """Bars covering (t0, t1]: close-stamped, so bar_time in (t0, t1]."""
        i0 = np.searchsorted(self.t, np.datetime64(t0, "ns"), side="right")
        i1 = np.searchsorted(self.t, np.datetime64(t1, "ns"), side="right")
        return i0, i1

    def excursion(self, i0, i1, ref, is_long):
        if i1 <= i0:
            return np.nan, np.nan
        hh, ll = self.h[i0:i1].max(), self.l[i0:i1].min()
        if is_long:
            return max((hh - ref) / self.tick, 0.0), max((ref - ll) / self.tick, 0.0)
        return max((ref - ll) / self.tick, 0.0), max((hh - ref) / self.tick, 0.0)

    def first_price_after(self, ts, max_wait_s=60):
        """Approximates the logger's 'first tick at/after ts': open of the first
        bar stamped after ts (bars are close-stamped)."""
        i = np.searchsorted(self.t, np.datetime64(ts, "ns"), side="right")
        if i >= len(self.t):
            return np.nan
        if (self.t[i] - np.datetime64(ts, "ns")) > np.timedelta64(15 + max_wait_s, "s"):
            return np.nan
        return self.o[i]

    def full_labels(self, t0, ref, is_long, window_min=60):
        """Rebuild every label the logger writes, from bars, over the full window.
        Returns dict or None if coverage is insufficient."""
        t0 = np.datetime64(t0, "ns")
        ws = t0 + np.timedelta64(window_min, "m")
        i0, i1 = self.span(t0, ws)
        if i1 <= i0:
            return None
        times = self.t[i0:i1]
        # coverage: last bar must be the one stamped at the window end, and no
        # internal hole longer than the configured limit
        if times[-1] != ws:
            return None
        gaps = np.diff(np.concatenate([[t0], times])).astype("timedelta64[s]").astype(int)
        if gaps.max() > CONFIG["extend_max_internal_gap_s"]:
            return None
        h, l = self.h[i0:i1], self.l[i0:i1]
        sgn = 1.0 if is_long else -1.0
        fav = (h - ref) / self.tick if is_long else (ref - l) / self.tick
        adv = (ref - l) / self.tick if is_long else (h - ref) / self.tick
        k_mfe = int(np.argmax(fav)); k_mae = int(np.argmax(adv))
        mfe = max(fav[k_mfe], 0.0); mae = max(adv[k_mae], 0.0)
        mins = lambda k: float((times[k] - t0) / np.timedelta64(1, "s")) / 60.0
        m_mfe = mins(k_mfe) if mfe > 0 else 0.0
        m_mae = mins(k_mae) if mae > 0 else 0.0
        out = {
            "mfe_ticks": mfe, "mae_ticks": mae,
            "minutes_to_mfe": m_mfe, "minutes_to_mae": m_mae,
            "mae_before_mfe": int(mae > 0 and (mfe == 0 or m_mae < m_mfe)),
        }
        for hm in HORIZONS:
            p = self.first_price_after(t0 + np.timedelta64(hm, "m"))
            out[f"delta_{hm}m"] = sgn * (p - ref) / self.tick if not np.isnan(p) else np.nan
        # logger convention: final_price = LAST in-window tick = close of the
        # bar stamped at the window end (verified 97.6% exact vs tick labels)
        fp = self.c[i1 - 1]
        out["final_price"] = fp
        out["final_delta_ticks"] = sgn * (fp - ref) / self.tick
        out["window_end_actual"] = pd.Timestamp(ws)
        return out


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------

def cme_session_date(ts: pd.Series) -> pd.Series:
    """CME trade date: the session that opens 17:00 CT belongs to the next day."""
    return (ts + pd.Timedelta(hours=24 - CONFIG["cme_roll_hour_ct"])).dt.strftime("%Y-%m-%d")


def cross_source_check(s: pd.DataFrame, bp: BarPath) -> pd.DataFrame:
    """Rebuild MFE/MAE from bars over exactly the span the logger observed and
    compare with the tick labels. Pure integrity check: it compares two
    measurements of the same thing; it never looks at outcome size."""
    t0 = s["signal_time"].values.astype("datetime64[ns]")
    t1 = s["window_end_actual"].dt.ceil("15s").values.astype("datetime64[ns]")
    i0 = np.searchsorted(bp.t, t0, side="right")
    i1 = np.searchsorted(bp.t, t1, side="right")
    ref = s["signal_reference_price"].values
    il = s["is_long"].values == 1
    mfe_b = np.full(len(s), np.nan); mae_b = np.full(len(s), np.nan)
    for k in range(len(s)):
        a, z = i0[k], i1[k]
        if z > a:
            mfe_b[k], mae_b[k] = bp.excursion(a, z, ref[k], il[k])
    out = pd.DataFrame(index=s.index)
    out["nbars_logged_span"] = (i1 - i0)
    out["xs_dmfe"] = np.abs(mfe_b - s["mfe_ticks"].values)
    out["xs_dmae"] = np.abs(mae_b - s["mae_ticks"].values)
    out["xs_dmax"] = out[["xs_dmfe", "xs_dmae"]].max(axis=1)
    return out


def validate_rebuild(s: pd.DataFrame, bp: BarPath, n: int, seed: int = 7) -> dict:
    """Run the full-label bar rebuild on uncensored, integrity-passing rows and
    report agreement with the tick labels. This is the evidence that the
    extension of 15:00-cut rows is trustworthy."""
    pool = s[(s["finalize_reason"] == "window_complete") & (s["_fail"] == "")]
    sample = pool.sample(min(n, len(pool)), random_state=seed)
    rows = []
    for t0, ref, il, wm in zip(sample["signal_time"], sample["signal_reference_price"],
                               sample["is_long"], sample["window_minutes"]):
        rows.append(bp.full_labels(t0, ref, il == 1, int(wm)))
    ok = [i for i, r in enumerate(rows) if r is not None]
    rb = pd.DataFrame([rows[i] for i in ok], index=sample.index[ok])
    ref_rows = sample.loc[rb.index]
    rep = {"sampled": int(len(sample)), "rebuilt": int(len(rb))}
    for col, tol in [("mfe_ticks", 1), ("mae_ticks", 1), ("minutes_to_mfe", 0.25),
                     ("minutes_to_mae", 0.25), ("delta_15m", 2), ("delta_60m", 2),
                     ("final_delta_ticks", 2)]:
        d = (rb[col] - ref_rows[col]).abs().dropna()
        rep[col] = {"within_tol_pct": round(100 * float((d <= tol).mean()), 3),
                    "tol": tol, "median_abs": float(d.median()),
                    "p99_abs": float(d.quantile(0.99))}
    return rep


def extend_rth_close(s: pd.DataFrame, bp: BarPath) -> pd.Series:
    """Rebuild 15:00-cut RTH windows to the full 60 minutes from bars.
    Returns a status Series: 'extended', 'extend_failed', or ''."""
    status = pd.Series("", index=s.index, dtype=object)
    cand = s[(s["finalize_reason"] == "rth_close") & (s["_fail"] == "")]
    label_cols = ["mfe_ticks", "mae_ticks", "minutes_to_mfe", "minutes_to_mae",
                  "mae_before_mfe", "final_price", "final_delta_ticks",
                  "window_end_actual"] + [f"delta_{h}m" for h in HORIZONS]
    updates = {c: {} for c in label_cols}
    for idx, t0, ref, il, wm in zip(cand.index, cand["signal_time"],
                                    cand["signal_reference_price"], cand["is_long"],
                                    cand["window_minutes"]):
        r = bp.full_labels(t0, ref, il == 1, int(wm))
        if r is None:
            status[idx] = "extend_failed"
            continue
        status[idx] = "extended"
        for c in label_cols:
            updates[c][idx] = r[c]
    for c in label_cols:
        if updates[c]:
            u = pd.Series(updates[c])
            if s[c].dtype.kind in "iu":
                s[c] = s[c].astype(float)
            s.loc[u.index, c] = u.values
    ext = status == "extended"
    s.loc[ext, "right_censored"] = 0
    return status


# ---------------------------------------------------------------------------
# main pipeline
# ---------------------------------------------------------------------------

def run(signals_path, bars_path, meta_path, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    hold_dir = os.path.join(out_dir, "holdout_LOCKED")
    os.makedirs(hold_dir, exist_ok=True)

    meta = json.load(open(meta_path)) if meta_path else {}
    s, b = load_inputs(signals_path, bars_path)
    tick = float(s["tick_size"].iloc[0])
    bp = BarPath(b, tick)
    n_raw = len(s)
    log = []  # (signal_id, reason)

    # -- session date on the CME basis (ADR 0010: timestamps are Central) ----
    s["session_date_logger"] = s["session_date"]
    s["session_date"] = cme_session_date(s["signal_time"])

    # -- integrity: cross-source agreement ------------------------------------
    xs = cross_source_check(s, bp)
    s = s.join(xs)
    s["_fail"] = ""
    nob = s["nbars_logged_span"] == 0
    s.loc[nob, "_fail"] = "no_bars_in_window"
    dis = (~nob) & (s["xs_dmax"] > CONFIG["xsource_tol_ticks"])
    s.loc[dis, "_fail"] = "bar_tick_disagree"

    # -- quarantine: gross failures contaminate the rest of their session ----
    gross = s[(s["xs_dmax"] > CONFIG["gross_fail_ticks"])]
    quarantine = {}
    for sess, g in gross.groupby("session_date"):
        t_first = g["signal_time"].min()
        quarantine[sess] = {"first_failing_signal": str(t_first), "gross_rows": int(len(g)),
                            "max_disagreement_ticks": float(g["xs_dmax"].max())}
        wm = pd.to_timedelta(s["window_minutes"], unit="m")
        q = (s["session_date"] == sess) & ((s["signal_time"] + wm) >= t_first) & (s["_fail"] == "")
        s.loc[q, "_fail"] = "quarantined_session_tail"

    # -- whole-cluster rule: if a cluster's FIRST signal fails, the entry is
    #    unusable — drop the cluster so entries.py can't promote a later row ---
    first = s[s["is_first_signal_in_cluster"] == 1]
    bad_clusters = set(first.loc[first["_fail"] != "", "signal_cluster_id"])
    cl = s["signal_cluster_id"].isin(bad_clusters) & (s["_fail"] == "")
    s.loc[cl, "_fail"] = "cluster_entry_failed"

    # -- validate the rebuild procedure, then extend the 15:00-cut rows -------
    rebuild_report = validate_rebuild(s, bp, CONFIG["rebuild_validation_rows"])
    s["label_source"] = "tick"
    ext_status = extend_rth_close(s, bp)
    s.loc[ext_status == "extended", "label_source"] = "bars_extended_60m"
    s.loc[ext_status == "extended", "finalize_reason"] = "rth_close_extended"

    # -- drop failed rows ------------------------------------------------------
    failed = s[s["_fail"] != ""]
    log.append(failed[["signal_id", "signal_time", "session_date", "_fail", "xs_dmax"]]
               .rename(columns={"_fail": "reason"}))
    s = s[s["_fail"] == ""].copy()

    # -- derived point-in-time features ----------------------------------------
    s["is_rth"] = (s["session_tag"] == "RTH").astype("int8")

    # -- remove unusable columns ------------------------------------------------
    s = s.drop(columns=[c for c in DROP_COLUMNS if c in s.columns] + ["_fail"])

    # -- split: dev | embargo | holdout (by CME session, by cluster entry time) -
    # assign each cluster to the split of its first signal, so no cluster
    # straddles the boundary
    cl_sess = (s.sort_values("signal_time").groupby("signal_cluster_id")["session_date"]
                .first())
    s["cluster_session"] = s["signal_cluster_id"].map(cl_sess)
    hs = CONFIG["holdout_start_session"]
    emb = set(CONFIG["embargo_sessions"])
    s["split"] = np.where(s["cluster_session"] >= hs, "holdout",
                          np.where(s["cluster_session"].isin(emb), "embargo", "dev"))
    emb_rows = s[s["split"] == "embargo"]
    log.append(emb_rows[["signal_id", "signal_time", "session_date"]].assign(
        reason="embargo", xs_dmax=np.nan))

    dev = s[s["split"] == "dev"].sort_values("signal_time").reset_index(drop=True)
    hold = s[s["split"] == "holdout"].sort_values("signal_time").reset_index(drop=True)

    # -- sanity: label windows of dev never reach the first holdout signal ----
    if len(hold):
        dev_last_end = dev["window_end_actual"].max()
        assert dev_last_end < hold["signal_time"].min(), \
            f"LEAK: dev label window ends {dev_last_end} >= first holdout signal"

    run_id = s["run_id"].iloc[0]
    dev_path = os.path.join(out_dir, f"signals_dev_{run_id}.parquet")
    hold_path = os.path.join(hold_dir, f"signals_HOLDOUT_{run_id}.parquet")
    dev.to_parquet(dev_path, index=False)
    hold.to_parquet(hold_path, index=False)
    drop_log = pd.concat(log, ignore_index=True)
    drop_log.to_csv(os.path.join(out_dir, f"drop_log_{run_id}.csv"), index=False)

    # -- manifest ----------------------------------------------------------------
    def entries_of(df):
        return df[df["is_first_signal_in_cluster"] == 1]

    def summary(df):
        e = entries_of(df)
        return {
            "signals": int(len(df)), "entries": int(len(e)),
            "sessions": int(df["session_date"].nunique()),
            "first_signal": str(df["signal_time"].min()), "last_signal": str(df["signal_time"].max()),
            "entries_by_session_tag": e["session_tag"].value_counts().to_dict(),
            "entries_by_finalize": e["finalize_reason"].value_counts().to_dict(),
            "entries_right_censored": int(e["right_censored"].sum()),
        }

    manifest = {
        "created_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_id": run_id,
        "inputs": {
            "signals": {"path": os.path.basename(signals_path), "sha256": _sha256(signals_path)},
            "bars": {"path": os.path.basename(bars_path), "sha256": _sha256(bars_path)},
            "meta": {"path": os.path.basename(meta_path) if meta_path else None,
                     "signal_count": meta.get("signal_count")},
        },
        "config": CONFIG,
        "dropped_columns": DROP_COLUMNS,
        "raw_signals": n_raw,
        "drops_by_reason_signals": drop_log["reason"].value_counts().to_dict(),
        "drops_by_reason_entries": (
            drop_log.merge(pd.DataFrame({"signal_id": first["signal_id"]}), on="signal_id")
            ["reason"].value_counts().to_dict()),
        "quarantined_sessions": quarantine,
        "rth_close_extension": ext_status[ext_status != ""].value_counts().to_dict(),
        "rebuild_validation": rebuild_report,
        "dev": summary(dev),
        "holdout": summary(hold),
        "outputs": {"dev": os.path.basename(dev_path),
                    "holdout": "holdout_LOCKED/" + os.path.basename(hold_path)},
    }
    with open(os.path.join(out_dir, f"manifest_{run_id}.json"), "w") as f:
        json.dump(manifest, f, indent=2, default=str)
    return manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", required=True)
    ap.add_argument("--bars", required=True)
    ap.add_argument("--meta")
    ap.add_argument("--out", default="data/clean")
    a = ap.parse_args()
    m = run(a.signals, a.bars, a.meta, a.out)
    print(json.dumps({k: m[k] for k in ("raw_signals", "drops_by_reason_signals",
                                        "drops_by_reason_entries", "quarantined_sessions",
                                        "rth_close_extension", "rebuild_validation",
                                        "dev", "holdout")}, indent=2, default=str))


if __name__ == "__main__":
    main()
