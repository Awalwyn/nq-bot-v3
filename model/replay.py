"""
replay.py — realistic trade replay on the 15s bars (Phase 2; ADR 0014/0015).

Every exit policy, flat or learned, is scored by THIS module so the comparison
is like for like. Rules, all as the live bot trades:

  * Entry: the strategy runs OnBarClose and sends a market order, so the fill is
    the OPEN of the next bar (entry_fill="next_open"). The audit measured
    next-open minus signal close: median 0, p90 6 ticks.
  * TP/SL are measured from the FILL price (NinjaTrader SetProfitTarget /
    SetStopLoss in ticks).
  * No forced close (IsExitOnSessionCloseStrategy = false). A trade runs until
    TP, SL or its own timeout, through the 16:00 halt and weekends, up to
    `max_hold_days` (then it is closed and flagged).
  * Gap-through: if a bar OPENS beyond a barrier, the fill is that open (worse
    for a stop, better for a limit target).
  * Same-bar tie: if the open is beyond a barrier, that barrier filled first.
    Otherwise SL first (conservative).
  * Timeout (learned policy): exit at the close of the first bar stamped at or
    after the deadline. If the deadline falls inside a halt, exit at the
    reopen bar's open.
  * Position rules: max open contracts, contracts per entry, and whether
    opposite signals are ignored while in a position. Applied chronologically
    after each trade's own outcome is known (stops/targets are per entry, so
    outcomes don't interact).

Entry populations (`select_entries`):
  * "cluster_first": first signal of every same-direction run (the training table).
  * "filtered_flip": approximates v2 live (StartLongTrade/StartShortTrade). An
    entry fires when a signal that passes all 7 entry filters points the
    opposite way from the last filter-passing signal. PENDING Matthew's
    confirmation of the live rule.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TICK = 0.25
BAR_S = 15

# Trading costs, in ticks (NQ: 1 tick = $5 per contract). Defaults are
# deliberately conservative and are recorded with every result:
#   commission_rt_ticks: round-trip commission + fees (~$4.50-5 per contract)
#   stop_slip_ticks:     extra slippage on market exits (stop, timeout, horizon);
#                        limit targets get none
COSTS = {"commission_rt_ticks": 1.0, "stop_slip_ticks": 1.0}


class Bars:
    def __init__(self, bars: pd.DataFrame, tick: float = TICK):
        b = bars.sort_values("bar_time")
        self.t = b["bar_time"].values.astype("datetime64[ns]")
        self.o = b["open"].values.astype(float)
        self.h = b["high"].values.astype(float)
        self.l = b["low"].values.astype(float)
        self.c = b["close"].values.astype(float)
        self.tick = tick
        self.n = len(self.t)

    @classmethod
    def from_file(cls, path: str):
        b = pd.read_parquet(path) if path.endswith(".parquet") else \
            pd.read_csv(path, encoding="utf-8-sig", parse_dates=["bar_time"])
        b["bar_time"] = pd.to_datetime(b["bar_time"])
        return cls(b)


# ---------------------------------------------------------------------------
# entry populations
# ---------------------------------------------------------------------------

def select_entries(signals: pd.DataFrame, population: str = "cluster_first",
                   session: str | None = "RTH", trade_window=(830, 1500)) -> pd.DataFrame:
    s = signals.sort_values("signal_time")
    if population == "cluster_first":
        e = s[s["is_first_signal_in_cluster"] == 1]
    elif population == "filtered_flip":
        f = s[s["entry_filters_passed"] == 1]
        flip = f["is_long"].ne(f["is_long"].shift())
        e = f[flip]
    elif population == "all_signals":
        e = s
    else:
        raise ValueError(population)
    if session:
        e = e[e["session_tag"] == session]
    if trade_window:
        hhmm = e["signal_time"].dt.hour * 100 + e["signal_time"].dt.minute
        e = e[(hhmm >= trade_window[0]) & (hhmm < trade_window[1])]
    return e.reset_index(drop=True)


# ---------------------------------------------------------------------------
# single-trade engine
# ---------------------------------------------------------------------------

def _first_at_least(cm: np.ndarray, level: float) -> int:
    """First index where the running max reaches `level` (len(cm) if never)."""
    return int(np.searchsorted(cm, level, side="left"))


def _resolve(bars: Bars, a: int, z: int, entry: float, is_long: bool,
             tp: float, sl: float, deadline_idx: int | None, deadline_gap: bool):
    """Return (exit_idx, exit_px, reason, gap_fill)."""
    h, l, o = bars.h[a:z], bars.l[a:z], bars.o[a:z]
    if is_long:
        fav, adv = (h - entry) / bars.tick, (entry - l) / bars.tick
        tp_px, sl_px = entry + tp * bars.tick, entry - sl * bars.tick
    else:
        fav, adv = (entry - l) / bars.tick, (h - entry) / bars.tick
        tp_px, sl_px = entry - tp * bars.tick, entry + sl * bars.tick
    i_tp = _first_at_least(np.maximum.accumulate(fav), tp)
    i_sl = _first_at_least(np.maximum.accumulate(adv), sl)
    n = len(fav)
    i_to = (deadline_idx - a) if deadline_idx is not None else n
    i = min(i_tp, i_sl, i_to)
    if i >= n:
        return a + n - 1, bars.c[a + n - 1], "horizon", False
    op = o[i]
    open_past_tp = (op >= tp_px) if is_long else (op <= tp_px)
    open_past_sl = (op <= sl_px) if is_long else (op >= sl_px)
    if i == i_tp or i == i_sl:
        if open_past_sl and i == i_sl:
            return a + i, op, "sl", True
        if open_past_tp and i == i_tp:
            return a + i, op, "tp", True
        if i == i_sl:
            return a + i, sl_px, "sl", False
        return a + i, tp_px, "tp", False
    # timeout
    if deadline_gap:
        return a + i, op, "timeout", True
    return a + i, bars.c[a + i], "timeout", False


def simulate(entries: pd.DataFrame, bars: Bars, tp, sl, timeout_min=None,
             entry_fill: str = "next_open", max_hold_days: float = 7.0,
             costs: dict | None = None) -> pd.DataFrame:
    """Replay every entry independently. tp/sl/timeout_min may be scalars or
    per-entry arrays (a learned policy). Returns one row per entry."""
    n = len(entries)
    tp = np.broadcast_to(np.asarray(tp, float), (n,))
    sl = np.broadcast_to(np.asarray(sl, float), (n,))
    to = None if timeout_min is None else np.broadcast_to(np.asarray(timeout_min, float), (n,))
    t0 = entries["signal_time"].values.astype("datetime64[ns]")
    ref = entries["signal_reference_price"].values.astype(float)
    isl = entries["is_long"].values == 1
    a_idx = np.searchsorted(bars.t, t0, side="right")
    max_bars = int(max_hold_days * 24 * 3600 / BAR_S)
    out = {k: np.empty(n, dtype=object) if k == "reason" else np.full(n, np.nan)
           for k in ("entry_px", "exit_px", "pnl_ticks", "hold_min", "reason")}
    out["exit_time"] = np.empty(n, dtype="datetime64[ns]")
    out["entry_time"] = np.empty(n, dtype="datetime64[ns]")
    flags = {k: np.zeros(n, bool) for k in ("gap_fill", "past_60m", "thru_halt", "thru_weekend")}
    for k in range(n):
        a = a_idx[k]
        if a >= bars.n:
            out["reason"][k] = "no_data"
            continue
        entry = bars.o[a] if entry_fill == "next_open" else ref[k]
        z = min(a + max_bars, bars.n)
        d_idx, d_gap = None, False
        if to is not None and np.isfinite(to[k]) and to[k] < max_hold_days * 1440:
            deadline = bars.t[a] - np.timedelta64(BAR_S, "s") + np.timedelta64(int(to[k] * 60), "s")
            d_idx = int(np.searchsorted(bars.t, deadline, side="left"))
            if d_idx >= z:
                d_idx = None
            else:
                d_gap = (bars.t[d_idx] - deadline) > np.timedelta64(BAR_S, "s")
        i, px, reason, gap = _resolve(bars, a, z, entry, isl[k], tp[k], sl[k], d_idx, d_gap)
        pnl = ((px - entry) if isl[k] else (entry - px)) / bars.tick
        out["entry_px"][k], out["exit_px"][k], out["pnl_ticks"][k] = entry, px, pnl
        out["reason"][k] = reason
        out["entry_time"][k] = bars.t[a] - np.timedelta64(BAR_S, "s")  # fill at that bar's open
        out["exit_time"][k] = bars.t[i]
        hold = (bars.t[i] - t0[k]) / np.timedelta64(1, "m")
        out["hold_min"][k] = hold
        flags["gap_fill"][k] = gap
        flags["past_60m"][k] = hold > 60
        seg = bars.t[a:i + 1]
        if len(seg) > 1:
            dts = np.diff(seg).astype("timedelta64[s]").astype(int)
            flags["thru_halt"][k] = (dts >= 3000).any()
            flags["thru_weekend"][k] = (dts >= 40 * 3600).any()
    c = {**COSTS, **(costs or {})}
    market_exit = np.isin(out["reason"], ["sl", "timeout", "horizon"])
    out["net_ticks"] = out["pnl_ticks"] - c["commission_rt_ticks"] - np.where(market_exit, c["stop_slip_ticks"], 0.0)
    res = pd.DataFrame({
        "signal_id": entries["signal_id"].values,
        "signal_time": entries["signal_time"].values,
        "session_date": entries["session_date"].values,
        "session_tag": entries["session_tag"].values,
        "is_long": isl.astype(int), "score": entries["score"].values,
        "tp_ticks": tp, "sl_ticks": sl,
        "timeout_min": to if to is not None else np.nan,
        **out, **flags})
    return res


# ---------------------------------------------------------------------------
# position rules (chronological)
# ---------------------------------------------------------------------------

def apply_position_rules(trades: pd.DataFrame, max_contracts: int = 7,
                         contracts_per_entry: int = 1, ignore_opposite: bool = True,
                         entries_per_direction: int | None = None) -> pd.DataFrame:
    """Mark which trades the bot could actually take. entries_per_direction
    mimics NinjaTrader's EntriesPerDirection (v2 live = 1)."""
    t = trades.sort_values("signal_time").copy()
    taken = np.zeros(len(t), bool)
    open_exit, open_dir = [], []  # parallel lists of open trades
    st = t["signal_time"].values
    et = t["exit_time"].values
    dr = t["is_long"].values
    ok = t["reason"].values != "no_data"
    for k in range(len(t)):
        if not ok[k]:
            continue
        keep = [j for j, x in enumerate(open_exit) if x > st[k]]
        open_exit = [open_exit[j] for j in keep]
        open_dir = [open_dir[j] for j in keep]
        if ignore_opposite and any(d != dr[k] for d in open_dir):
            continue
        same = sum(1 for d in open_dir if d == dr[k])
        if entries_per_direction is not None and same >= entries_per_direction:
            continue
        if (len(open_dir) + 1) * contracts_per_entry > max_contracts:
            continue
        taken[k] = True
        open_exit.append(et[k]); open_dir.append(dr[k])
    t["taken"] = taken
    t["contracts"] = np.where(taken, contracts_per_entry, 0)
    return t


# ---------------------------------------------------------------------------
# summaries
# ---------------------------------------------------------------------------

def day_block_ci(trades: pd.DataFrame, col: str = "pnl_ticks", n_boot: int = 2000,
                 seed: int = 11, alpha: float = 0.05):
    """Mean per trade with a bootstrap CI that resamples whole SESSIONS, so
    same-day trades (which move together) are not treated as independent."""
    g = trades.groupby("session_date")[col].agg(["sum", "count"])
    if len(g) < 2:
        return np.nan, np.nan, np.nan
    rng = np.random.default_rng(seed)
    s, c = g["sum"].values, g["count"].values
    idx = rng.integers(0, len(g), size=(n_boot, len(g)))
    means = s[idx].sum(1) / c[idx].sum(1)
    return float(s.sum() / c.sum()), float(np.quantile(means, alpha / 2)), \
        float(np.quantile(means, 1 - alpha / 2))


def summarize(trades: pd.DataFrame, label: str = "") -> dict:
    t = trades[trades.get("taken", True) == True] if "taken" in trades else trades  # noqa: E712
    t = t[t["reason"] != "no_data"]
    if t.empty:
        return {"label": label, "trades": 0}
    mean, lo, hi = day_block_ci(t, "pnl_ticks")
    nmean, nlo, nhi = day_block_ci(t, "net_ticks") if "net_ticks" in t else (np.nan,) * 3
    t = t.sort_values("exit_time")
    pcol = "net_ticks" if "net_ticks" in t else "pnl_ticks"
    eq = (t[pcol] * t.get("contracts", 1)).cumsum()
    dd = float((eq - eq.cummax()).min())
    daily = (t[pcol] * t.get("contracts", 1)).groupby(t["session_date"]).sum()
    return {
        "label": label, "trades": int(len(t)), "sessions": int(t["session_date"].nunique()),
        "mean_ticks": round(mean, 2), "ci95": [round(lo, 2), round(hi, 2)],
        "net_mean_ticks": round(nmean, 2), "net_ci95": [round(nlo, 2), round(nhi, 2)],
        "win_rate": round(float((t["pnl_ticks"] > 0).mean()), 3),
        "total_net_ticks": round(float((t[pcol] * t.get("contracts", 1)).sum()), 1),
        "max_drawdown_ticks": round(dd, 1),
        "worst_day_ticks": round(float(daily.min()), 1),
        "exit_mix": t["reason"].value_counts(normalize=True).round(3).to_dict(),
        "hold_min_median": round(float(t["hold_min"].median()), 1),
        "past_60m_pct": round(100 * float(t["past_60m"].mean()), 1),
        "thru_halt_pct": round(100 * float(t["thru_halt"].mean()), 1),
        "thru_weekend_pct": round(100 * float(t["thru_weekend"].mean()), 2),
        "gap_fill_pct": round(100 * float(t["gap_fill"].mean()), 2),
    }


# ---------------------------------------------------------------------------
# flat grid (for "best flat on dev"): first-passage per entry, all pairs at once
# ---------------------------------------------------------------------------

def flat_grid(entries: pd.DataFrame, bars: Bars, tps, sls, entry_fill="next_open",
              max_hold_days: float = 7.0) -> pd.DataFrame:
    """Per-trade P&L for every (tp, sl) pair, computed from one first-passage
    pass per entry. Same tie/gap rules as simulate(). No position rules (those
    depend on exit times; apply them to the chosen pair with simulate())."""
    tps = np.asarray(tps, float); sls = np.asarray(sls, float)
    per_entry = tps.ndim == 2  # (n_entries, k) levels -> a learned/scaled policy grid
    t0 = entries["signal_time"].values.astype("datetime64[ns]")
    ref = entries["signal_reference_price"].values.astype(float)
    isl = entries["is_long"].values == 1
    a_idx = np.searchsorted(bars.t, t0, side="right")
    max_bars = int(max_hold_days * 24 * 3600 / BAR_S)
    kt = tps.shape[-1]; ks = sls.shape[-1]
    pnl = np.full((len(entries), kt, ks), np.nan)
    tps_all, sls_all = tps, sls
    for k in range(len(entries)):
        a = a_idx[k]
        if a >= bars.n:
            continue
        z = min(a + max_bars, bars.n)
        entry = bars.o[a] if entry_fill == "next_open" else ref[k]
        h, l, o, c = bars.h[a:z], bars.l[a:z], bars.o[a:z], bars.c[a:z]
        if isl[k]:
            fav, adv, ofav = (h - entry), (entry - l), (o - entry)
        else:
            fav, adv, ofav = (entry - l), (h - entry), (entry - o)
        fav, adv, ofav = fav / bars.tick, adv / bars.tick, ofav / bars.tick
        tps = tps_all[k] if per_entry else tps_all
        sls = sls_all[k] if per_entry else sls_all
        itp = np.searchsorted(np.maximum.accumulate(fav), tps, side="left")
        isl_ = np.searchsorted(np.maximum.accumulate(adv), sls, side="left")
        n = len(fav)
        last = ((c[-1] - entry) if isl[k] else (entry - c[-1])) / bars.tick
        I, J = itp[:, None], isl_[None, :]
        i = np.minimum(I, J)
        never = i >= n
        ic = np.minimum(i, n - 1)
        of = ofav[ic]                                   # open, in favorable ticks
        tp_m, sl_m = tps[:, None], sls[None, :]
        sl_hit = (J == i)
        tp_hit = (I == i)
        # gap-through and tie rules (same as _resolve)
        open_past_sl = of <= -sl_m
        open_past_tp = of >= tp_m
        res = np.where(sl_hit & open_past_sl, of,
              np.where(tp_hit & open_past_tp, of,
              np.where(sl_hit, -sl_m, tp_m)))
        pnl[k] = np.where(never, last, res)
    return pnl
