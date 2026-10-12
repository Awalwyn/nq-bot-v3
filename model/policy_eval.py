"""
policy_eval.py — Phase 2 steps 5-6 ON DEV: learned exit bands vs flat, per
walk-forward fold, scored with the realistic replay (net of costs).

Per fold (same folds as walkforward.py):
  1. fit the MFE and MAE heads on the fit span (chosen config), early-stop on valid
  2. predict valid-slice and test-month RTH entries (out of sample for the model)
  3. on the VALID slice only, choose:
       learned: TP = a * pred_MFE, SL = b * pred_MAE   (clamped 8..800)
       best flat: TP, SL from the flat grid
  4. score on the TEST month: learned vs flat 200/400 vs in-fold best flat,
     per trade, net of costs, plus the paired (learned - flat) difference
Pooled over the 5 test months with a day-block CI. The holdout is never read.
"""
from __future__ import annotations
import json, os, sys, time
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from model import dataset, entries as entries_mod, replay, walkforward as wf  # noqa: E402

A = [0.25, 0.5, 0.75, 1.0, 1.5]
B = [0.5, 0.75, 1.0, 1.5, 2.0, 3.0]
FLAT = [20, 40, 60, 80, 100, 150, 200, 300, 400, 600, 800]
CLAMP = (8.0, 800.0)


def net_grid(ent, bars, tps, sls):
    g = replay.flat_grid(ent, bars, tps, sls)          # gross ticks, (n, kt, ks)
    tp_hit = None  # costs: commission always; stop slippage when the exit is not the TP
    c = replay.COSTS
    if np.ndim(tps) == 2:
        tp_lv = np.asarray(tps)[:, :, None]
    else:
        tp_lv = np.asarray(tps)[None, :, None]
    is_tp = np.isclose(g, tp_lv) | (g > tp_lv)          # TP fill (incl. favorable gap)
    return g - c["commission_rt_ticks"] - np.where(is_tp, 0.0, c["stop_slip_ticks"])


def rth_window(df):
    hhmm = df["signal_time"].dt.hour * 100 + df["signal_time"].dt.minute
    return df[(df["session_tag"] == "RTH") & (hhmm >= 830) & (hhmm < 1500)]


def run(dev_path, bars_path, out_dir, population="cluster_first"):
    import xgboost as xgb
    t0 = time.time()
    dev = dataset.load_clean(dev_path)
    bars = replay.Bars.from_file(bars_path)
    ent = entries_mod.to_entries(dev, policy="first")
    folds = wf.make_folds(ent)
    live = replay.select_entries(dev, "filtered_flip", "RTH", (830, 1500))
    cfg = {"mfe_ticks": dict(max_depth=3, min_child_weight=200), "mae_ticks": dict(max_depth=3, min_child_weight=200)}
    rows, rep = [], {"folds": [], "A": A, "B": B, "flat_grid": FLAT, "costs": replay.COSTS, "clamp": CLAMP}
    for f in folds:
        fit = ent[ent["session_date"].isin(f["fit"])]
        val = ent[ent["session_date"].isin(f["valid"])]
        models = {}
        for h in ("mfe_ticks", "mae_ticks"):
            fh = fit[(fit["right_censored"] == 0) & fit[h].notna()]
            vh = val[(val["right_censored"] == 0) & val[h].notna()]
            models[h] = wf.fit_head(xgb, fh[wf.FEATURES], fh[h], vh[wf.FEATURES], vh[h], cfg[h])
        def pred(df):
            return (np.expm1(models["mfe_ticks"].predict(df[wf.FEATURES])),
                    np.expm1(models["mae_ticks"].predict(df[wf.FEATURES])))
        vsel = rth_window(dev[dev["session_date"].isin(f["valid"]) & (dev["is_first_signal_in_cluster"] == 1)]).reset_index(drop=True)
        tsel = rth_window(dev[dev["session_date"].isin(f["test"]) & (dev["is_first_signal_in_cluster"] == 1)]).reset_index(drop=True)
        # --- choose on VALID ---
        pm, pa = pred(vsel)
        tpl = np.clip(np.outer(pm, A), *CLAMP); sll = np.clip(np.outer(pa, B), *CLAMP)
        gl = net_grid(vsel, bars, tpl, sll); ml = np.nanmean(gl, axis=0)
        ia, ib = np.unravel_index(np.nanargmax(ml), ml.shape)
        gf = net_grid(vsel, bars, FLAT, FLAT); mf = np.nanmean(gf, axis=0)
        it, isl = np.unravel_index(np.nanargmax(mf), mf.shape)
        choice = {"a": A[ia], "b": B[ib], "valid_net": round(float(ml[ia, ib]), 2),
                  "best_flat": (FLAT[it], FLAT[isl]), "best_flat_valid_net": round(float(mf[it, isl]), 2)}
        # --- score on TEST ---
        def score(sel, tag):
            pm, pa = pred(sel)
            tp_l = np.clip(pm * A[ia], *CLAMP); sl_l = np.clip(pa * B[ib], *CLAMP)
            res = {}
            for name, tp, sl in [("learned", tp_l, sl_l), ("flat200_400", 200, 400),
                                 ("best_flat_in_fold", FLAT[it], FLAT[isl])]:
                tr = replay.simulate(sel, bars, tp, sl)
                tr["policy"] = name; tr["month"] = f["month"]; tr["population"] = tag
                rows.append(tr)
                res[name] = replay.summarize(tr, name)
            return res
        fr = {"month": f["month"], "choice": choice,
              "test_cluster_first_rth": score(tsel, "cluster_first"),
              "test_live_approx": score(live[live["session_date"].isin(f["test"])].reset_index(drop=True), "filtered_flip")}
        rep["folds"].append(fr)
        print(f["month"], json.dumps(choice), {k: v["net_mean_ticks"] for k, v in fr["test_cluster_first_rth"].items()},
              {k: (v["trades"], v["net_mean_ticks"]) for k, v in fr["test_live_approx"].items()},
              f"({time.time()-t0:.0f}s)", flush=True)
    allt = pd.concat(rows, ignore_index=True)
    allt.to_parquet(os.path.join(out_dir, "policy_eval_trades_dev.parquet"))
    pooled = {}
    for pop in ("cluster_first", "filtered_flip"):
        a = allt[allt["population"] == pop]
        pooled[pop] = {p: replay.summarize(a[a["policy"] == p], p) for p in a["policy"].unique()}
        # paired difference learned - flat, day-block CI
        w = a.pivot_table(index=["signal_id", "session_date"], columns="policy", values="net_ticks").reset_index()
        for base in ("flat200_400", "best_flat_in_fold"):
            d = w.assign(diff=w["learned"] - w[base])
            m, lo, hi = replay.day_block_ci(d, "diff")
            pooled[pop][f"learned_minus_{base}"] = {"mean": round(m, 2), "ci95": [round(lo, 2), round(hi, 2)], "trades": int(len(d))}
    rep["pooled_test_months"] = pooled
    with open(os.path.join(out_dir, "policy_eval_dev.json"), "w") as fh:
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
    print(json.dumps({k: {p: (v.get("trades"), v.get("net_mean_ticks"), v.get("net_ci95")) if "net_mean_ticks" in v else v
                          for p, v in d.items()} for k, d in r["pooled_test_months"].items()}, indent=1))
