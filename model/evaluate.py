"""
evaluate.py — replay exit strategies over the logged price paths and compare.

This is the framework that answers the project's core question: *does a learned
per-signal exit beat a flat fixed exit on the same entries?* We build it BEFORE
we have a model so the success metric is fixed in advance and can't be
retrofitted to whatever result we happen to get.

How it works:
  * A "strategy" is a decide function: signal_row -> (tp_ticks, sl_ticks,
    timeout_bars). The flat baseline returns constants; a learned exit will later
    return per-signal values from model predictions. Same replay for both.
  * For each signal we slice the bars file to its forward window and walk it,
    resolving TP/SL by first touch (SL assumed first on an ambiguous same-bar
    touch — conservative), else timeout, else final close.
  * We aggregate expectancy (mean ticks/trade, win rate, total) overall and by
    score bucket and session.

The flat tp/sl defaults are PLACEHOLDERS. The real baseline numbers and any
learned policy are decided on real data, not here.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data_pipeline import schema  # noqa: E402
from model import dataset  # noqa: E402

TICK = 0.25
BARS_PER_MIN = 4  # 15s bars


# ---------------------------------------------------------------------------
# single-trade simulation over a forward path
# ---------------------------------------------------------------------------

def simulate_trade(ref, is_long, seg_high, seg_low, seg_close,
                   tp_ticks, sl_ticks, timeout_bars):
    """Walk one forward path; return (pnl_ticks_dirnorm, reason, bars_held)."""
    if is_long:
        tp_price = ref + tp_ticks * TICK
        sl_price = ref - sl_ticks * TICK
    else:
        tp_price = ref - tp_ticks * TICK
        sl_price = ref + sl_ticks * TICK

    n = len(seg_close)
    for i in range(n):
        hi, lo = seg_high[i], seg_low[i]
        hit_tp = (hi >= tp_price) if is_long else (lo <= tp_price)
        hit_sl = (lo <= sl_price) if is_long else (hi >= sl_price)
        if hit_sl:  # conservative: SL wins an ambiguous same-bar touch
            exit_px = sl_price
            reason = "sl"
            return _pnl(ref, exit_px, is_long), reason, i + 1
        if hit_tp:
            exit_px = tp_price
            return _pnl(ref, exit_px, is_long), "tp", i + 1
        if timeout_bars and (i + 1) >= timeout_bars:
            return _pnl(ref, seg_close[i], is_long), "timeout", i + 1
    # neither barrier, no timeout: exit at last available close
    last = seg_close[-1] if n else ref
    return _pnl(ref, last, is_long), "window_end", n


def _pnl(ref, exit_px, is_long):
    return ((exit_px - ref) if is_long else (ref - exit_px)) / TICK


# ---------------------------------------------------------------------------
# strategies (decide functions)
# ---------------------------------------------------------------------------

def flat(tp_ticks, sl_ticks, timeout_min=None):
    """A fixed exit for every signal. timeout_min optional."""
    tb = int(timeout_min * BARS_PER_MIN) if timeout_min else None

    def decide(_row):
        return tp_ticks, sl_ticks, tb
    decide.__name__ = f"flat_{tp_ticks}tp_{sl_ticks}sl"
    return decide


# ---------------------------------------------------------------------------
# replay + summarize
# ---------------------------------------------------------------------------

def replay(signals: pd.DataFrame, bars: pd.DataFrame, decide) -> pd.DataFrame:
    bars = bars.copy()
    bars["bar_time"] = pd.to_datetime(bars["bar_time"], errors="coerce")
    bars = bars.sort_values("bar_time").reset_index(drop=True)
    bt = bars["bar_time"].values
    hi = bars["high"].values
    lo = bars["low"].values
    cl = bars["close"].values

    s = signals.copy()
    s["signal_time"] = pd.to_datetime(s["signal_time"], errors="coerce")
    s["window_end_actual"] = pd.to_datetime(s["window_end_actual"], errors="coerce")

    out = []
    for _, r in s.iterrows():
        # start after the signal bar closes (ref is that bar's close); end at the
        # observation's actual (possibly censored) window end.
        lo_i = np.searchsorted(bt, np.datetime64(r["signal_time"]), side="right")
        hi_i = np.searchsorted(bt, np.datetime64(r["window_end_actual"]), side="right")
        if hi_i <= lo_i:
            continue
        tp_ticks, sl_ticks, timeout_bars = decide(r)
        pnl, reason, held = simulate_trade(
            r["signal_reference_price"], int(r["is_long"]) == 1,
            hi[lo_i:hi_i], lo[lo_i:hi_i], cl[lo_i:hi_i],
            tp_ticks, sl_ticks, timeout_bars)
        out.append({
            "signal_id": r["signal_id"], "score": r["score"],
            "session_tag": r["session_tag"], "pnl_ticks": pnl,
            "reason": reason, "bars_held": held,
        })
    return pd.DataFrame(out)


def summarize(results: pd.DataFrame) -> dict:
    if results.empty:
        return {"trades": 0}
    p = results["pnl_ticks"]
    return {
        "trades": len(results),
        "mean_ticks": round(float(p.mean()), 3),
        "median_ticks": round(float(p.median()), 3),
        "win_rate_pct": round(float((p > 0).mean() * 100), 1),
        "total_ticks": round(float(p.sum()), 1),
        "exit_mix": results["reason"].value_counts(normalize=True).round(2).to_dict(),
    }


def compare(signals, bars, strategies: dict) -> pd.DataFrame:
    """strategies: {name: decide_fn}. Returns a summary table, prints it."""
    rows = []
    for name, decide in strategies.items():
        res = replay(signals, bars, decide)
        s = summarize(res)
        s["strategy"] = name
        rows.append(s)
    tbl = pd.DataFrame(rows).set_index("strategy")
    cols = ["trades", "mean_ticks", "median_ticks", "win_rate_pct", "total_ticks"]
    print(tbl[cols].to_string())
    return tbl


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", help="folder with signals_*.csv and bars_*.csv")
    ap.add_argument("--tp", type=float, default=200, help="PLACEHOLDER flat TP ticks")
    ap.add_argument("--sl", type=float, default=400, help="PLACEHOLDER flat SL ticks")
    args = ap.parse_args()

    sig = dataset.load_signals(dir_path=args.dir)
    bars_path = dataset._latest(args.dir, "bars")
    bars = pd.read_csv(bars_path)

    print(f"loaded {len(sig)} signals, {len(bars)} bars")
    print("\nbaseline comparison (flat exits — PLACEHOLDER tp/sl):")
    compare(sig, bars, {
        "flat_200_400": flat(200, 400),
        "flat_100_200": flat(100, 200),
        "flat_50_100_15mto": flat(50, 100, timeout_min=15),
    })
    print("\n(these numbers are on SYNTHETIC data — framework proof only.)")
