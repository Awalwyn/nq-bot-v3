"""
make_sample_data.py — synthetic but schema-consistent signals + bars + meta.

DEV/TEST helper only (random-walk noise, not real market data). Rev 2 matches
the reviewed logger schema and produces all three finalize reasons so the
validator and downstream code can be exercised before real data exists:
  window_complete · rth_close (RTH-censored) · terminated (data ended).

Usage:
    uv run python scripts/make_sample_data.py --out data/training
    uv run python -m data_pipeline.validate --dir data/training
"""
from __future__ import annotations
import argparse, json, os, sys, datetime as dt
import numpy as np, pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data_pipeline import schema  # noqa: E402

TICK = 0.25
BPM = 4                      # 15s bars per minute
WMIN = 60
INSTRUMENT = "NQ 12-26"
MASTER = "NQ"
EXPIRY = "2026-12-19"
RUN_ID = "run_sample"
RTH_START, RTH_END = 930, 1600


def _hhmm(t): return t.hour * 100 + t.minute
def _in_rth(t): return RTH_START <= _hhmm(t) <= RTH_END


def build(seed, n_signals):
    rng = np.random.default_rng(seed)
    # bars: 09:30 -> 16:30 (RTH to 16:00, then overnight to 16:30 for terminated case)
    start = dt.datetime(2024, 6, 3, 9, 30, 0)
    n_bars = int(7.0 * 60 * BPM)  # 7 hours
    times = [start + dt.timedelta(seconds=15 * i) for i in range(n_bars)]
    close = 19000.0 + rng.normal(0, 0.75, n_bars).cumsum() * TICK
    cl = np.round(close / TICK) * TICK
    op = np.concatenate([[cl[0]], cl[:-1]])          # open = prior close
    wick = np.abs(rng.normal(0, 2.0, n_bars)) * TICK
    # high/low must bracket BOTH open and close, or OHLC is invalid
    top = np.maximum(op, cl)
    bot = np.minimum(op, cl)
    hi = np.round((top + wick) / TICK) * TICK
    lo = np.round((bot - wick) / TICK) * TICK

    bars = pd.DataFrame({
        "run_id": RUN_ID, "instrument": INSTRUMENT, "instrument_master": MASTER,
        "expiry": EXPIRY, "tick_size": TICK, "bar_period": "15s",
        "bar_time": [t.strftime("%Y-%m-%dT%H:%M:%S") for t in times],
        "session_date": "2024-06-03",
        "session_tag": ["RTH" if _in_rth(t) else "OVERNIGHT" for t in times],
        "open": op, "high": hi, "low": lo, "close": cl,
        "volume": rng.integers(50, 400, n_bars),
    })[schema.BARS_COLUMNS]

    rth_close_dt = dt.datetime(2024, 6, 3, 16, 0, 0)
    last_bar_dt = times[-1]

    # choose signal bars: spread across the day, plus a few late for censoring
    idxs = sorted(set(rng.choice(range(40, n_bars - 4), size=n_signals, replace=False)))
    rows = []
    for k, bi in enumerate(idxs):
        score = int(rng.choice([-8, -7, -6, -5, -4, 4, 5, 6, 7, 8]))
        is_long = 1 if score > 0 else 0
        ref = cl[bi]
        t = times[bi]
        scheduled = t + dt.timedelta(minutes=WMIN)

        # effective end + reason (mirrors logger semantics)
        if _in_rth(t) and scheduled > rth_close_dt:
            actual_end, reason, censored = rth_close_dt, "rth_close", 1
        elif scheduled > last_bar_dt:
            actual_end, reason, censored = last_bar_dt, "terminated", 1
        else:
            actual_end, reason, censored = scheduled, "window_complete", 0

        # in-window path starts AFTER the signal bar (exclude its intrabar move)
        end_bi = min(int(np.searchsorted([tt.timestamp() for tt in times],
                                         actual_end.timestamp(), side="right")) - 1, n_bars - 1)
        j0, j1 = bi + 1, end_bi
        if j1 < j0:
            j1 = j0
        seg_hi, seg_lo, seg_cl = hi[j0:j1 + 1], lo[j0:j1 + 1], cl[j0:j1 + 1]
        if is_long:
            favs, advs = (seg_hi - ref) / TICK, (ref - seg_lo) / TICK
        else:
            favs, advs = (ref - seg_lo) / TICK, (seg_hi - ref) / TICK
        mfe = max(0.0, float(favs.max())) if len(favs) else 0.0
        mae = max(0.0, float(advs.max())) if len(advs) else 0.0
        min_to_mfe = (float(np.argmax(favs)) + 1) / BPM if len(favs) and mfe > 0 else np.nan
        min_to_mae = (float(np.argmax(advs)) + 1) / BPM if len(advs) and mae > 0 else np.nan
        final_price = cl[end_bi]
        final_delta = ((final_price - ref) if is_long else (ref - final_price)) / TICK

        horizons = {}
        for col, mn in schema.HORIZON_COLUMNS.items():
            hb = bi + mn * BPM
            if hb <= end_bi:
                d = (cl[hb] - ref) / TICK
                horizons[col] = round(d if is_long else -d, 2)
            else:
                horizons[col] = np.nan

        # mae_before_mfe = t_final_mae < t_final_mfe (+inf if not occurred)
        tm_mae = min_to_mae if mae > 0 else np.inf
        tm_mfe = min_to_mfe if mfe > 0 else np.inf
        mae_before = int(tm_mae < tm_mfe)

        vol_p, adx_p = int(rng.integers(0, 2)), int(rng.integers(0, 2))
        r = {c: 0 for c in schema.SIGNALS_COLUMNS}
        r.update({
            "signal_id": f"{INSTRUMENT}|15s|{t.strftime('%Y%m%d%H%M%S')}|{'L' if is_long else 'S'}|{score:+d}|seq1",
            "run_id": RUN_ID, "instrument": INSTRUMENT, "instrument_master": MASTER,
            "expiry": EXPIRY, "tick_size": TICK, "bar_period": "15s",
            "signal_time": t.strftime("%Y-%m-%dT%H:%M:%S"), "session_date": "2024-06-03",
            "session_tag": "RTH" if _in_rth(t) else "OVERNIGHT",
            "score": score, "direction": "LONG" if is_long else "SHORT", "is_long": is_long,
            "exact_score_sequence_position": 1,
            "signal_cluster_id": k + 1, "is_first_signal_in_cluster": 1,
            "bars_since_cluster_start": 0, "bars_since_last_same_dir_signal": -1,
            "previous_prediction_score": 0, "score_changed_from_prior_bar": 1, "is_new_score_extreme": 1,
            "signal_reference_price": ref,
            "volatility_pass": vol_p, "atr_recent": 4.0, "atr_historical": 4.0, "atr_ratio": 1.0,
            "regime_pass": int(rng.integers(0, 2)), "regime_value": round(float(rng.normal(0, .3)), 3),
            "adx_pass": adx_p, "adx_value": round(float(rng.uniform(10, 40)), 2),
            "ema200_pass": int(rng.integers(0, 2)), "ema200_value": ref, "dist_ema200_ticks": 0, "dist_ema200_atr": 0,
            "ema800_pass": int(rng.integers(0, 2)), "ema800_value": ref, "dist_ema800_ticks": 0, "dist_ema800_atr": 0,
            "sma200_pass": int(rng.integers(0, 2)), "sma200_value": ref, "dist_sma200_ticks": 0,
            "kernel_pass": int(rng.integers(0, 2)), "kernel_value": ref, "kernel_gaussian": ref,
            "kernel_slope_ticks": 0, "price_minus_kernel_ticks": 0,
            "session_pass": 1 if _in_rth(t) else 0,
            "entry_filters_passed": int(vol_p == 1 and adx_p == 1), "filter_config": "vol+adx",
            "candidate_executed_live": 0, "manual_intervention": 0,
            "rsi": 50, "cci": 0, "dist_vwap_ticks": 0,
            "minutes_since_rth_open": round((t - dt.datetime(2024, 6, 3, 9, 30)).total_seconds() / 60, 1),
            "minute_of_day": t.hour * 60 + t.minute, "day_of_week": t.strftime("%A"),
            "candle_run": 1, "body_to_range": 0.5, "volume": int(bars["volume"].iloc[bi]),
            "volume_ratio": 1.0, "bar_range_ticks": round((hi[bi] - lo[bi]) / TICK, 2),
            "mfe_ticks": round(mfe, 2), "mae_ticks": round(mae, 2),
            "minutes_to_mfe": round(min_to_mfe, 2) if mfe > 0 else np.nan,
            "minutes_to_mae": round(min_to_mae, 2) if mae > 0 else np.nan,
            "mae_before_mfe": mae_before,
            "final_delta_ticks": round(final_delta, 2), "final_price": round(final_price, 2),
            "window_end_scheduled": scheduled.strftime("%Y-%m-%dT%H:%M:%S"),
            "window_end_actual": actual_end.strftime("%Y-%m-%dT%H:%M:%S"),
            "right_censored": censored, "window_minutes": WMIN, "finalize_reason": reason,
            "tick_updates": int(max(1, ((actual_end - t).total_seconds() / 60.0) *
                                    (rng.uniform(30, 120) if _in_rth(t) else rng.uniform(5, 20)))),
        })
        r.update(horizons)
        rows.append(r)

    sig = pd.DataFrame(rows)[schema.SIGNALS_COLUMNS]
    return sig, bars


def write_meta(out, n_signals, n_bars):
    meta = {
        "run_id": RUN_ID, "created_utc": "2024-06-03T21:00:00Z", "logger": "sample-generator",
        "instrument_full": INSTRUMENT, "instrument_master": MASTER, "expiry": EXPIRY,
        "tick_size": TICK, "bar_period": "15s", "timezone_id": "US Eastern Standard Time",
        "rth_start": RTH_START, "rth_end": RTH_END,
        "classifier_session_start": 830, "classifier_session_end": 1500,
        "min_abs_score": 4, "max_abs_score": 8, "window_minutes": WMIN,
        "cut_at_rth_close": True, "log_all_sessions": True,
        # clean-completion metadata (validator rev 3 strict mode)
        "completion_status": "completed",
        "signal_count": int(n_signals), "bar_count": int(n_bars),
        "signal_write_errors": 0, "bar_write_errors": 0,
        "horizon_minutes": list(schema.HORIZON_COLUMNS.values()),
    }
    json.dump(meta, open(os.path.join(out, RUN_ID + ".meta.json"), "w"), indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/training")
    ap.add_argument("--n", type=int, default=80)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    sig, bars = build(args.seed, args.n)
    sig.to_csv(os.path.join(args.out, f"signals_{RUN_ID}.csv"), index=False)
    bars.to_csv(os.path.join(args.out, f"bars_{RUN_ID}.csv"), index=False)
    write_meta(args.out, len(sig), len(bars))
    print(f"wrote {len(sig)} signals, {len(bars)} bars, meta -> {args.out}")
    print("finalize_reason mix:", sig["finalize_reason"].value_counts().to_dict())


if __name__ == "__main__":
    main()
