"""
make_sample_data.py — generate a synthetic but internally-consistent signals +
bars pair so you can exercise the pipeline (validate.py, later train.py) BEFORE
the ThinkPad produces real data.

This is a DEV/TEST helper only. The data is random-walk noise, not real market
data — it exists so you can confirm your environment and the code run, and see
what a PASSED validation looks like. Delete the output before logging real data.

Usage:
    uv run python scripts/make_sample_data.py --out data/training
    uv run python -m data_pipeline.validate --dir data/training     # -> PASSED
"""

from __future__ import annotations

import argparse
import os
import sys
import datetime as dt

import numpy as np
import pandas as pd

# make the package importable whether run as a module or a script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data_pipeline import schema  # noqa: E402

TICK = 0.25
BARS_PER_MIN = 4          # 15s bars
WINDOW_MIN = 60
WINDOW_BARS = WINDOW_MIN * BARS_PER_MIN


def build(seed: int, n_signals: int):
    rng = np.random.default_rng(seed)

    # ---- one continuous RTH session of 15s bars (09:30–16:00 = 1560 bars) ----
    start = dt.datetime(2024, 6, 3, 9, 30, 0)
    n_bars = int(6.5 * 60 * BARS_PER_MIN)
    times = [start + dt.timedelta(seconds=15 * i) for i in range(n_bars)]

    # random-walk closes; small wicks around each close
    steps = rng.normal(0, 0.75, n_bars).cumsum()
    close = 19000.0 + steps * TICK
    wick = np.abs(rng.normal(0, 2.0, n_bars)) * TICK
    high = close + wick
    low = close - wick
    open_ = np.concatenate([[close[0]], close[:-1]])
    vol = rng.integers(50, 400, n_bars)

    bars = pd.DataFrame({
        "run_id": "run_sample",
        "instrument": "NQ",
        "bar_period": "15s",
        "bar_time": [t.strftime("%Y-%m-%dT%H:%M:%S") for t in times],
        "session_date": "2024-06-03",
        "session_tag": "RTH",
        "open": np.round(open_ / TICK) * TICK,
        "high": np.round(high / TICK) * TICK,
        "low": np.round(low / TICK) * TICK,
        "close": np.round(close / TICK) * TICK,
        "volume": vol,
    })[schema.BARS_COLUMNS]

    hi = bars["high"].values
    lo = bars["low"].values
    cl = bars["close"].values

    # ---- place signals at random bars, label each from the forward window ----
    # leave room so most windows are complete; a few near the end get censored.
    idxs = sorted(rng.choice(range(200, n_bars - 20), size=n_signals, replace=False))
    rows = []
    for k, bi in enumerate(idxs):
        score = int(rng.choice([-8, -7, -6, -5, -4, 4, 5, 6, 7, 8]))
        is_long = 1 if score > 0 else 0
        ref = cl[bi]
        t = times[bi]

        end_bi = min(bi + WINDOW_BARS, n_bars - 1)
        censored = 1 if (bi + WINDOW_BARS) > (n_bars - 1) else 0
        window_end = times[end_bi]

        seg_hi = hi[bi:end_bi + 1]
        seg_lo = lo[bi:end_bi + 1]
        # direction-normalized favorable / adverse, in ticks, positive magnitudes
        if is_long:
            fav_series = (seg_hi - ref) / TICK
            adv_series = (ref - seg_lo) / TICK
        else:
            fav_series = (ref - seg_lo) / TICK
            adv_series = (seg_hi - ref) / TICK
        mfe = max(0.0, float(fav_series.max()))
        mae = max(0.0, float(adv_series.max()))
        min_to_mfe = float(np.argmax(fav_series)) / BARS_PER_MIN
        min_to_mae = float(np.argmax(adv_series)) / BARS_PER_MIN

        # horizon deltas (direction-normalized close vs ref)
        horizon_vals = {}
        for col, mn in schema.HORIZON_COLUMNS.items():
            hb = bi + mn * BARS_PER_MIN
            if hb <= end_bi:
                d = (cl[hb] - ref) / TICK
                horizon_vals[col] = d if is_long else -d
            else:
                horizon_vals[col] = np.nan
        final_d = (cl[end_bi] - ref) / TICK
        final_delta = final_d if is_long else -final_d

        # filters: individual verdicts random; composite = AND of named components
        vol_p = int(rng.integers(0, 2))
        adx_p = int(rng.integers(0, 2))
        composite = int(vol_p == 1 and adx_p == 1)

        r = {c: 0 for c in schema.SIGNALS_COLUMNS}
        r.update({
            "signal_id": f"NQ|15s|{t.strftime('%Y%m%d%H%M%S')}|{'L' if is_long else 'S'}|{score:+d}|seq1",
            "run_id": "run_sample",
            "instrument": "NQ",
            "bar_period": "15s",
            "signal_time": t.strftime("%Y-%m-%dT%H:%M:%S"),
            "session_date": "2024-06-03",
            "session_tag": "RTH",
            "score": score,
            "direction": "LONG" if is_long else "SHORT",
            "is_long": is_long,
            "exact_score_sequence_position": 1,
            "signal_cluster_id": k + 1,        # each signal its own cluster (simple + valid)
            "is_first_signal_in_cluster": 1,
            "bars_since_cluster_start": 0,
            "bars_since_last_same_dir_signal": -1,
            "previous_prediction_score": 0,
            "score_changed_from_prior_bar": 1,
            "is_new_score_extreme": 1,
            "signal_reference_price": ref,
            "volatility_pass": vol_p,
            "atr_recent": round(float(rng.uniform(2, 8)), 2),
            "atr_historical": round(float(rng.uniform(2, 8)), 2),
            "atr_ratio": round(float(rng.uniform(0.5, 1.5)), 3),
            "regime_pass": int(rng.integers(0, 2)),
            "regime_value": round(float(rng.normal(0, 0.3)), 3),
            "adx_pass": adx_p,
            "adx_value": round(float(rng.uniform(10, 40)), 2),
            "ema200_pass": int(rng.integers(0, 2)),
            "ema200_value": round(ref - float(rng.normal(0, 5)), 2),
            "dist_ema200_ticks": round(float(rng.normal(0, 30)), 2),
            "dist_ema200_atr": round(float(rng.normal(0, 2)), 2),
            "ema800_pass": int(rng.integers(0, 2)),
            "ema800_value": round(ref - float(rng.normal(0, 10)), 2),
            "dist_ema800_ticks": round(float(rng.normal(0, 50)), 2),
            "dist_ema800_atr": round(float(rng.normal(0, 3)), 2),
            "sma200_pass": int(rng.integers(0, 2)),
            "sma200_value": round(ref - float(rng.normal(0, 5)), 2),
            "dist_sma200_ticks": round(float(rng.normal(0, 30)), 2),
            "kernel_pass": int(rng.integers(0, 2)),
            "kernel_value": round(ref - float(rng.normal(0, 3)), 2),
            "kernel_gaussian": round(ref - float(rng.normal(0, 3)), 2),
            "kernel_slope_ticks": round(float(rng.normal(0, 4)), 2),
            "price_minus_kernel_ticks": round(float(rng.normal(0, 10)), 2),
            "session_pass": 1,
            "entry_filters_passed": composite,
            "filter_config": "vol+adx",
            "candidate_executed_live": 0,
            "manual_intervention": 0,
            "rsi": round(float(rng.uniform(20, 80)), 2),
            "cci": round(float(rng.normal(0, 100)), 2),
            "dist_vwap_ticks": round(float(rng.normal(0, 40)), 2),
            "minutes_since_rth_open": round((t - start).total_seconds() / 60.0, 1),
            "minute_of_day": t.hour * 60 + t.minute,
            "day_of_week": t.strftime("%A"),
            "candle_run": int(rng.integers(0, 5)),
            "body_to_range": round(float(rng.uniform(0, 1)), 3),
            "volume": int(vol[bi]),
            "volume_ratio": round(float(rng.uniform(0.5, 2.0)), 3),
            "bar_range_ticks": round(float((hi[bi] - lo[bi]) / TICK), 2),
            "mfe_ticks": round(mfe, 2),
            "mae_ticks": round(mae, 2),
            "minutes_to_mfe": round(min_to_mfe, 2),
            "minutes_to_mae": round(min_to_mae, 2),
            "mae_before_mfe": int(min_to_mae <= min_to_mfe),
            "final_delta_ticks": round(final_delta, 2),
            "window_end": window_end.strftime("%Y-%m-%dT%H:%M:%S"),
            "right_censored": censored,
            "window_minutes": WINDOW_MIN,
            "finalize_reason": "window_complete",
        })
        for col, v in horizon_vals.items():
            r[col] = round(v, 2) if not np.isnan(v) else np.nan
        rows.append(r)

    sig = pd.DataFrame(rows)[schema.SIGNALS_COLUMNS]
    return sig, bars


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/training", help="output folder")
    ap.add_argument("--n", type=int, default=60, help="number of synthetic signals")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    sig, bars = build(args.seed, args.n)
    sp = os.path.join(args.out, "signals_run_sample.csv")
    bp = os.path.join(args.out, "bars_run_sample.csv")
    sig.to_csv(sp, index=False)
    bars.to_csv(bp, index=False)
    print(f"wrote {len(sig)} signals -> {sp}")
    print(f"wrote {len(bars)} bars    -> {bp}")
    print("now run:  uv run python -m data_pipeline.validate --dir", args.out)


if __name__ == "__main__":
    main()
