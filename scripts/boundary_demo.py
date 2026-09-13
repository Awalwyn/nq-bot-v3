"""
boundary_demo.py — executable proof of the forward-window boundary rule.

This is a Python reference of the corrected UpdateOpenObservations finalize
logic in SignalLogger.cs (review fix 1). It is an executable spec, not the live
code: the real guarantee is the C# tick loop, but this reproduces its exact
rule so the semantics can be verified on the Mac without NinjaTrader.

RULE: a tick strictly after WindowEnd must not affect MFE, MAE, horizon deltas,
or final delta. Finalization uses the last in-window price.

Run:  uv run python scripts/boundary_demo.py
"""
from __future__ import annotations
import datetime as dt

TICK = 0.25


def finalize(ref, is_long, signal_time, window_end, ticks):
    """Mirror of the corrected C# loop. `ticks` = [(time, price), ...]."""
    mfe = mae = 0.0
    t_mfe = t_mae = None
    last_in_window_price = None
    last_in_window_time = None
    horizons = {1: None, 3: None, 5: None}  # minutes -> delta ticks

    for tt, px in ticks:
        if tt < signal_time:
            continue
        if tt > window_end:            # BOUNDARY FIRST — do NOT apply this tick
            break                      # finalize below with last in-window state
        fav = ((px - ref) if is_long else (ref - px)) / TICK
        adv = ((ref - px) if is_long else (px - ref)) / TICK
        if fav > mfe:
            mfe, t_mfe = fav, (tt - signal_time).total_seconds() / 60.0
        if adv > mae:
            mae, t_mae = adv, (tt - signal_time).total_seconds() / 60.0
        last_in_window_price, last_in_window_time = px, tt
        for m in horizons:
            if horizons[m] is None and tt >= signal_time + dt.timedelta(minutes=m):
                horizons[m] = ((px - ref) if is_long else (ref - px)) / TICK

    final_price = last_in_window_price if last_in_window_price is not None else ref
    final_delta = ((final_price - ref) if is_long else (ref - final_price)) / TICK
    tm_mae = t_mae if mae > 0 else float("inf")
    tm_mfe = t_mfe if mfe > 0 else float("inf")
    return {
        "mfe_ticks": round(mfe, 2), "mae_ticks": round(mae, 2),
        "final_price": final_price, "final_delta_ticks": round(final_delta, 2),
        "window_end_actual": last_in_window_time, "horizons": horizons,
        "mae_before_mfe": int(tm_mae < tm_mfe),
    }


def main():
    ref = 19000.0
    is_long = True
    t0 = dt.datetime(2024, 6, 3, 10, 0, 0)
    window_end = t0 + dt.timedelta(minutes=60)

    # In-window path: MAE -4t at +2m, MFE +20t at +40m, ends at +58m near +12t.
    in_window = [
        (t0 + dt.timedelta(seconds=30),  ref - 1.00),   # -4t
        (t0 + dt.timedelta(minutes=40),  ref + 5.00),   # +20t  <- MFE
        (t0 + dt.timedelta(minutes=58),  ref + 3.00),   # +12t  <- last in-window
    ]
    # A violent spike 1 minute AFTER the window closes. Must be ignored entirely.
    post_window_spike = (t0 + dt.timedelta(minutes=61), ref + 50.00)  # +200t

    base = finalize(ref, is_long, t0, window_end, list(in_window))
    with_spike = finalize(ref, is_long, t0, window_end, list(in_window) + [post_window_spike])

    print("=" * 60)
    print("Forward-window boundary demonstration (fix 1)")
    print("=" * 60)
    print(f"ref={ref}  window_end={window_end:%H:%M:%S}")
    print(f"post-window tick: {post_window_spike[1]}  at +61m  (+200 ticks)\n")
    print(f"{'field':20s} {'in-window only':>16s} {'+ post spike':>16s}")
    for k in ("mfe_ticks", "mae_ticks", "final_delta_ticks", "mae_before_mfe"):
        print(f"{k:20s} {str(base[k]):>16s} {str(with_spike[k]):>16s}")
    print(f"{'window_end_actual':20s} {base['window_end_actual']:%H:%M:%S}{'':>7} "
          f"{with_spike['window_end_actual']:%H:%M:%S}")

    ok = (base["mfe_ticks"] == with_spike["mfe_ticks"]
          and base["mae_ticks"] == with_spike["mae_ticks"]
          and base["final_delta_ticks"] == with_spike["final_delta_ticks"]
          and base["window_end_actual"] == with_spike["window_end_actual"])
    print("\nMFE stayed at +20t (not +200t); final = last in-window price.")
    print("PASS — no tick after WindowEnd affected the labels."
          if ok else "FAIL — post-window tick leaked into labels.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
