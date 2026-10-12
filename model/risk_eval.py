"""
risk_eval.py — is the learned exit's lower risk worth anything beyond what a
smaller flat stop, or plain volatility scaling, already gives? (ADR 0016 follow-up)

Per walk-forward fold (dev only; holdout never read), all choices on the
VALIDATION slice, all scoring on the unseen TEST month, net of costs:

  learned     TP = a*pred_MFE,  SL = b*pred_MAE           (XGBoost heads)
  vol_scaled  TP = a*volMFE,    SL = b*volMAE             (median MFE/MAE by ATR
                                                           decile of the fit span:
                                                           no machine learning)
  flat_matched TP, SL = the learned policy's MEAN TP / SL on the validation slice
  flat200_400  the live baseline

Reported per policy: mean net ticks/trade (day-block CI), per-trade std, daily
std, mean/daily-std ratio, worst day, max drawdown, and how often a day loses
more than a prop-style daily limit.
"""
from __future__ import annotations
import json, os, sys, time
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from model import dataset, entries as entries_mod, replay, walkforward as wf  # noqa: E402
from model.policy_eval import A, B, CLAMP, net_grid, rth_window  # noqa: E402

DAILY_LIMIT_TICKS = [-400, -800]   # $2,000 / $4,000 per contract-day (NQ $5/tick)


def risk_summary(t: pd.DataFrame, label: str) -> dict:
    s = replay.summarize(t, label)
    daily = t.groupby("session_date")["net_ticks"].sum()
    s.update({
        "std_per_trade": round(float(t["net_ticks"].std()), 1),
        "daily_std": round(float(daily.std()), 1),
        "mean_over_daily_std": round(float(daily.mean() / daily.std()), 3) if daily.std() > 0 else None,
        **{f"days_below_{abs(l)}": int((daily < l).sum()) for l in DAILY_LIMIT_TICKS},
        "days": int(len(daily)),
    })
    return s


def choose_ab(sel, bars, pm, pa):
    tpl = np.clip(np.outer(pm, A), *CLAMP); sll = np.clip(np.outer(pa, B), *CLAMP)
    m = np.nanmean(net_grid(sel, bars, tpl, sll), axis=0)
    i, j = np.unravel_index(np.nanargmax(m), m.shape)
    return A[i], B[j]


def run(dev_path, bars_path, out_dir):
    import xgboost as xgb
    t0 = time.time()
    dev = dataset.load_clean(dev_path)
    bars = replay.Bars.from_file(bars_path)
    ent = entries_mod.to_entries(dev, policy="first")
    folds = wf.make_folds(ent)
    live = replay.select_entries(dev, "filtered_flip", "RTH", (830, 1500))
    cfg = dict(max_depth=3, min_child_weight=200)
    rows, rep = [], {"folds": [], "daily_limits_ticks": DAILY_LIMIT_TICKS, "costs": replay.COSTS}
    for f in folds:
        fit = ent[ent["session_date"].isin(f["fit"])]
        val = ent[ent["session_date"].isin(f["valid"])]
        heads, fitsets = {}, {}
        for h in ("mfe_ticks", "mae_ticks"):
            fh = fit[(fit["right_censored"] == 0) & fit[h].notna()]
            vh = val[(val["right_censored"] == 0) & val[h].notna()]
            heads[h] = wf.fit_head(xgb, fh[wf.FEATURES], fh[h], vh[wf.FEATURES], vh[h], cfg)
            fitsets[h] = fh

        def learned(df):
            return (np.expm1(heads["mfe_ticks"].predict(df[wf.FEATURES])),
                    np.expm1(heads["mae_ticks"].predict(df[wf.FEATURES])))

        def volscaled(df):
            return (wf.vol_bucket_median(fitsets["mfe_ticks"], fitsets["mfe_ticks"]["mfe_ticks"], df),
                    wf.vol_bucket_median(fitsets["mae_ticks"], fitsets["mae_ticks"]["mae_ticks"], df))

        firsts = dev[dev["is_first_signal_in_cluster"] == 1]
        vsel = rth_window(firsts[firsts["session_date"].isin(f["valid"])]).reset_index(drop=True)
        tsel = rth_window(firsts[firsts["session_date"].isin(f["test"])]).reset_index(drop=True)

        # --- all choices on VALID ---
        lp = learned(vsel); vp = volscaled(vsel)
        la, lb = choose_ab(vsel, bars, *lp)
        va, vb = choose_ab(vsel, bars, *vp)
        m_tp = float(np.mean(np.clip(lp[0] * la, *CLAMP))); m_sl = float(np.mean(np.clip(lp[1] * lb, *CLAMP)))
        choice = {"learned_ab": (la, lb), "vol_ab": (va, vb),
                  "flat_matched": (round(m_tp), round(m_sl))}

        def score(sel, tag):
            out = {}
            pl = learned(sel); pv = volscaled(sel)
            for name, tp, sl in [
                ("learned", np.clip(pl[0] * la, *CLAMP), np.clip(pl[1] * lb, *CLAMP)),
                ("vol_scaled", np.clip(pv[0] * va, *CLAMP), np.clip(pv[1] * vb, *CLAMP)),
                ("flat_matched", round(m_tp), round(m_sl)),
                ("flat200_400", 200, 400)]:
                tr = replay.simulate(sel, bars, tp, sl)
                tr["policy"], tr["month"], tr["population"] = name, f["month"], tag
                rows.append(tr)
                out[name] = risk_summary(tr, name)
            return out
        fr = {"month": f["month"], "choice": choice, "cluster_first": score(tsel, "cluster_first"),
              "live_approx": score(live[live["session_date"].isin(f["test"])].reset_index(drop=True), "filtered_flip")}
        rep["folds"].append(fr)
        print(f["month"], choice, {k: (v["net_mean_ticks"], v["std_per_trade"], v["worst_day_ticks"])
                                   for k, v in fr["cluster_first"].items()}, f"({time.time()-t0:.0f}s)", flush=True)

    allt = pd.concat(rows, ignore_index=True)
    allt.to_parquet(os.path.join(out_dir, "risk_eval_trades_dev.parquet"))
    pooled = {}
    for pop in ("cluster_first", "filtered_flip"):
        a = allt[allt["population"] == pop]
        pooled[pop] = {p: risk_summary(a[a["policy"] == p], p) for p in a["policy"].unique()}
        w = a.pivot_table(index=["signal_id", "session_date"], columns="policy", values="net_ticks").reset_index()
        for x, y in [("learned", "flat_matched"), ("learned", "vol_scaled"), ("vol_scaled", "flat_matched")]:
            d = w.assign(diff=w[x] - w[y])
            m, lo, hi = replay.day_block_ci(d, "diff")
            pooled[pop][f"{x}_minus_{y}"] = {"mean": round(m, 2), "ci95": [round(lo, 2), round(hi, 2)]}
    rep["pooled_test_months"] = pooled
    with open(os.path.join(out_dir, "risk_eval_dev.json"), "w") as fh:
        json.dump(rep, fh, indent=1, default=str)
    return rep


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", required=True); ap.add_argument("--bars", required=True)
    ap.add_argument("--out", default="reports")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    r = run(a.dev, a.bars, a.out)
    keys = ["trades", "net_mean_ticks", "net_ci95", "std_per_trade", "daily_std", "mean_over_daily_std",
            "worst_day_ticks", "max_drawdown_ticks", "days_below_400", "days_below_800", "days"]
    for pop, d in r["pooled_test_months"].items():
        print("\n==", pop)
        for p, v in d.items():
            print(f"  {p:28s}", {k: v[k] for k in keys if k in v} if "trades" in v else v)
