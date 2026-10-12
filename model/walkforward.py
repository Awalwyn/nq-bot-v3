"""
walkforward.py — Phase 2 step 4: train the three heads on DEV with monthly
walk-forward folds and every overfitting guard in the spec (PHASE2_DATA_SPEC §4-5).

Folds (expanding window, by CME session):
    test month M in Feb..Jun 2026
    train   = all dev sessions before M, minus 1 embargo session at the end
    valid   = the last VALID_SESSIONS sessions of train (early stopping ONLY)
    fit     = train minus valid
The test month is never used for fitting, early stopping or model choice.

Heads: mfe_ticks, mae_ticks, minutes_to_mfe — fit on log1p(target), censored
rows excluded. Each head must beat predict-the-mean AND a volatility-bucket
median (MAE, original units) in >= 4 of 5 folds.

Guards:
  * label-shuffle: same pipeline on shuffled targets -> skill must vanish
  * date canary: a day-index feature added on purpose -> must not rank high
  * train vs valid gap per fold
  * the question that matters (ADR 0015): does predicted MFE - MAE rank the
    realized trade outcome on the test month? (Spearman + quintile spread of
    the realistic flat 200/400 net P&L)

The holdout is never read here.
"""

from __future__ import annotations

import json
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data_pipeline import schema   # noqa: E402
from model import dataset, entries as entries_mod  # noqa: E402

HEADS = ["mfe_ticks", "mae_ticks", "minutes_to_mfe"]
TEST_MONTHS = ["2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]
EMBARGO_SESSIONS = 1
VALID_SESSIONS = 20
FEATURES = list(schema.MODEL_FEATURE_CANDIDATES)

# Pre-declared grid (spec §5). Chosen by MEAN validation score across folds.
GRID = [
    dict(max_depth=3, min_child_weight=200),
    dict(max_depth=3, min_child_weight=50),
    dict(max_depth=5, min_child_weight=200),
    dict(max_depth=5, min_child_weight=50),
]
BASE = dict(n_estimators=3000, learning_rate=0.05, subsample=0.8, colsample_bytree=0.8,
            reg_lambda=5.0, objective="reg:squarederror", tree_method="hist",
            early_stopping_rounds=100, n_jobs=2, random_state=7)


def spearman(a, b):
    a = pd.Series(a).rank().values; b = pd.Series(b).rank().values
    if np.std(a) == 0 or np.std(b) == 0:
        return np.nan
    return float(np.corrcoef(a, b)[0, 1])


def make_folds(df: pd.DataFrame):
    sessions = np.array(sorted(df["session_date"].unique()), dtype=object)
    folds = []
    for m in TEST_MONTHS:
        test_s = sessions[[s.startswith(m) for s in sessions]]
        before = sessions[sessions < min(test_s)]
        train_s = before[:-EMBARGO_SESSIONS] if EMBARGO_SESSIONS else before
        valid_s = train_s[-VALID_SESSIONS:]
        fit_s = train_s[:-VALID_SESSIONS]
        folds.append({"month": m, "fit": set(fit_s), "valid": set(valid_s), "test": set(test_s),
                      "fit_range": (fit_s[0], fit_s[-1]), "valid_range": (valid_s[0], valid_s[-1]),
                      "test_range": (test_s[0], test_s[-1])})
    return folds


def vol_bucket_median(train_x, train_y, test_x, col="atr_recent", q=10):
    edges = np.unique(np.quantile(train_x[col], np.linspace(0, 1, q + 1)))
    tb = np.clip(np.searchsorted(edges, train_x[col], side="right") - 1, 0, len(edges) - 2)
    med = pd.Series(train_y.values).groupby(tb).median()
    sb = np.clip(np.searchsorted(edges, test_x[col], side="right") - 1, 0, len(edges) - 2)
    return pd.Series(sb).map(med).fillna(float(np.median(train_y))).values


def fit_head(xgb, Xf, yf, Xv, yv, params):
    m = xgb.XGBRegressor(**{**BASE, **params})
    m.fit(Xf, np.log1p(yf), eval_set=[(Xv, np.log1p(yv))], verbose=False)
    return m


def run(dev_path, flat_trades_path=None, out_dir="reports", quick=False):
    import xgboost as xgb
    t_start = time.time()
    dev = dataset.load_clean(dev_path)
    ent = entries_mod.to_entries(dev, policy="first")
    ent["day_index"] = (pd.to_datetime(ent["session_date"]) - pd.Timestamp("2025-10-01")).dt.days
    folds = make_folds(ent)
    grid = GRID[:1] if quick else GRID
    rep = {"features": FEATURES, "n_entries": int(len(ent)), "folds": [], "grid": grid,
           "base_params": {k: v for k, v in BASE.items()}}

    # optional realized outcomes for the separation test (flat 200/400, net)
    flat = pd.read_parquet(flat_trades_path)[["signal_id", "net_ticks"]] if flat_trades_path else None

    # ---- 1. choose the config on VALIDATION only (mean across folds) -------
    val_scores = {h: np.zeros(len(grid)) for h in HEADS}
    fitted = {}  # (head, gi, fold) -> model
    for fi, f in enumerate(folds):
        fit = ent[ent["session_date"].isin(f["fit"])]
        val = ent[ent["session_date"].isin(f["valid"])]
        for h in HEADS:
            fh = fit[(fit["right_censored"] == 0) & fit[h].notna()]
            vh = val[(val["right_censored"] == 0) & val[h].notna()]
            for gi, g in enumerate(grid):
                m = fit_head(xgb, fh[FEATURES], fh[h], vh[FEATURES], vh[h], g)
                pv = np.expm1(m.predict(vh[FEATURES]))
                val_scores[h][gi] += np.mean(np.abs(pv - vh[h].values)) / len(folds)
                fitted[(h, gi, fi)] = m
        print(f"fold {f['month']} fitted ({time.time() - t_start:.0f}s)", flush=True)
    chosen = {h: int(np.argmin(val_scores[h])) for h in HEADS}
    rep["validation_mae_by_config"] = {h: [round(x, 2) for x in val_scores[h]] for h in HEADS}
    rep["chosen_config"] = {h: grid[chosen[h]] for h in HEADS}

    # ---- 2. score the chosen config on each TEST month ----------------------
    for fi, f in enumerate(folds):
        fit = ent[ent["session_date"].isin(f["fit"])]
        val = ent[ent["session_date"].isin(f["valid"])]
        test = ent[ent["session_date"].isin(f["test"])]
        fr = {"month": f["month"], "fit_range": f["fit_range"], "valid_range": f["valid_range"],
              "test_range": f["test_range"], "n_fit": int(len(fit)), "n_test": int(len(test)), "heads": {}}
        preds = {}
        for h in HEADS:
            m = fitted[(h, chosen[h], fi)]
            fh = fit[(fit["right_censored"] == 0) & fit[h].notna()]
            vh = val[(val["right_censored"] == 0) & val[h].notna()]
            th = test[(test["right_censored"] == 0) & test[h].notna()]
            p_fit = np.expm1(m.predict(fh[FEATURES]))
            p_val = np.expm1(m.predict(vh[FEATURES]))
            p_test = np.expm1(m.predict(th[FEATURES]))
            preds[h] = pd.Series(np.expm1(m.predict(test[FEATURES])), index=test.index)
            mae = lambda p, y: float(np.mean(np.abs(p - y)))
            b_mean = np.full(len(th), fh[h].mean())
            b_vol = vol_bucket_median(fh, fh[h], th)
            hr = {
                "n_test": int(len(th)),
                "best_iteration": int(m.best_iteration),
                "mae_fit": round(mae(p_fit, fh[h].values), 2),
                "mae_valid": round(mae(p_val, vh[h].values), 2),
                "mae_test": round(mae(p_test, th[h].values), 2),
                "mae_test_predict_mean": round(mae(b_mean, th[h].values), 2),
                "mae_test_vol_bucket_median": round(mae(b_vol, th[h].values), 2),
                "spearman_test": round(spearman(p_test, th[h].values), 3),
                "spearman_test_vol_bucket": round(spearman(b_vol, th[h].values), 3),
            }
            hr["beats_mean"] = hr["mae_test"] < hr["mae_test_predict_mean"]
            hr["beats_vol_bucket"] = hr["mae_test"] < hr["mae_test_vol_bucket_median"]

            # label-shuffle guard: same pipeline, shuffled targets
            rng = np.random.default_rng(fi)
            ys = fh[h].values.copy(); rng.shuffle(ys)
            yvs = vh[h].values.copy(); rng.shuffle(yvs)
            ms = fit_head(xgb, fh[FEATURES], pd.Series(ys), vh[FEATURES], pd.Series(yvs), grid[chosen[h]])
            ps = np.expm1(ms.predict(th[FEATURES]))
            hr["shuffle_spearman_test"] = round(spearman(ps, th[h].values), 3)

            # importance (gain) top 8
            imp = pd.Series(m.get_booster().get_score(importance_type="gain"))
            hr["top_gain"] = imp.sort_values(ascending=False).head(8).round(1).to_dict()
            fr["heads"][h] = hr

        # ---- the question that matters: does predicted edge rank outcomes? --
        rth = test[(test["session_tag"] == "RTH")]
        hhmm = rth["signal_time"].dt.hour * 100 + rth["signal_time"].dt.minute
        rth = rth[(hhmm >= 830) & (hhmm < 1500)]
        edge = (preds["mfe_ticks"] - preds["mae_ticks"]).loc[rth.index]
        realized_60m = rth["final_delta_ticks"]
        sep = {"n_rth": int(len(rth)),
               "spearman_pred_edge_vs_60m_move": round(spearman(edge, realized_60m), 3)}
        if flat is not None:
            j = rth[["signal_id"]].assign(edge=edge.values).merge(flat, on="signal_id")
            if len(j) >= 50:
                j["q"] = pd.qcut(j["edge"].rank(method="first"), 5, labels=False)
                qm = j.groupby("q")["net_ticks"].mean()
                sep["flat200_400_net_by_pred_edge_quintile"] = qm.round(1).to_dict()
                sep["top_minus_bottom_quintile"] = round(float(qm.iloc[-1] - qm.iloc[0]), 1)
                sep["spearman_pred_edge_vs_flat_net"] = round(spearman(j["edge"], j["net_ticks"]), 3)
        fr["separation_rth"] = sep
        rep["folds"].append(fr)
        print(f"fold {f['month']} scored ({time.time() - t_start:.0f}s)", flush=True)

    # ---- date canary (chosen config, last fold) ------------------------------
    f = folds[-1]
    fit = ent[ent["session_date"].isin(f["fit"])]
    val = ent[ent["session_date"].isin(f["valid"])]
    canary = {}
    for h in HEADS:
        fh = fit[(fit["right_censored"] == 0) & fit[h].notna()]
        vh = val[(val["right_censored"] == 0) & val[h].notna()]
        cols = FEATURES + ["day_index"]
        m = fit_head(xgb, fh[cols], fh[h], vh[cols], vh[h], grid[chosen[h]])
        imp = pd.Series(m.get_booster().get_score(importance_type="gain")).sort_values(ascending=False)
        canary[h] = {"day_index_rank": int(list(imp.index).index("day_index") + 1) if "day_index" in imp else None,
                     "of": int(len(imp)),
                     "day_index_gain_share": round(float(imp.get("day_index", 0) / imp.sum()), 3)}
    rep["date_canary_last_fold"] = canary

    # ---- pass/fail summary ----------------------------------------------------
    summ = {}
    for h in HEADS:
        rows = [fr["heads"][h] for fr in rep["folds"]]
        summ[h] = {"folds_beating_mean": sum(r["beats_mean"] for r in rows),
                   "folds_beating_vol_bucket": sum(r["beats_vol_bucket"] for r in rows),
                   "mean_spearman_test": round(float(np.mean([r["spearman_test"] for r in rows])), 3),
                   "mean_shuffle_spearman": round(float(np.mean([r["shuffle_spearman_test"] for r in rows])), 3)}
        summ[h]["passes_4_of_5"] = (summ[h]["folds_beating_mean"] >= 4 and summ[h]["folds_beating_vol_bucket"] >= 4)
    seps = [fr["separation_rth"] for fr in rep["folds"]]
    summ["separation"] = {
        "spearman_edge_vs_60m_by_fold": [s["spearman_pred_edge_vs_60m_move"] for s in seps],
        "top_minus_bottom_quintile_by_fold": [s.get("top_minus_bottom_quintile") for s in seps]}
    rep["summary"] = summ
    rep["runtime_s"] = round(time.time() - t_start, 1)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "walkforward_report.json"), "w") as fh:
        json.dump(rep, fh, indent=1, default=str)
    return rep


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", required=True)
    ap.add_argument("--flat-trades", help="parquet from replay.simulate (flat 200/400, dev RTH) for the separation test")
    ap.add_argument("--out", default="reports")
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()
    r = run(a.dev, a.flat_trades, a.out, a.quick)
    print(json.dumps(r["summary"], indent=1, default=str))
