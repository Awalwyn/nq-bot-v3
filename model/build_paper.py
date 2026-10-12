"""
build_paper.py — freeze everything the paper-trading strategy needs (ADR 0018).

Outputs (to --out, e.g. C:\\ProgramData\\nqbotv3\\models\\paper_v1):
  mfe_head.onnx / mae_head.onnx   XGBoost heads, input = float32[1, n_features]
                                  in FEATURE ORDER, output = log1p(ticks)
  paper_config.json               everything the C# side needs:
      feature_order, output transform (expm1), clamps,
      policy "learned":    TP = a * expm1(mfe_out), SL = b * expm1(mae_out)
      policy "vol_scaled": TP = a * MFE_table[decile(atr_recent)], SL = b * MAE_table[...]
      policy "flat":       TP 200 / SL 400
  parity_report.json              ONNX vs XGBoost on real dev rows

How the frozen numbers are chosen (dev only; holdout never read):
  * heads: fit on all dev entries except the last 20 sessions (early stopping
    on those), with the pre-declared config chosen in ADR 0016
  * a, b: the pair with the best mean net ticks over the five walk-forward TEST
    months, using each month's out-of-fold predictions (the same numbers the
    ADR 0016/0017 tables came from), RTH 08:30-15:00, cluster-first entries
  * vol table: ATR deciles + median MFE/MAE of uncensored RTH 08:30-15:00 dev entries
"""
from __future__ import annotations
import argparse, datetime as dt, json, os, sys
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from model import dataset, entries as entries_mod, replay, walkforward as wf  # noqa: E402
from model.policy_eval import A, B, CLAMP, net_grid, rth_window  # noqa: E402

CFG = dict(max_depth=3, min_child_weight=200)
FLAT = (200, 400)


def vol_table(fit: pd.DataFrame, q: int = 10):
    # Built from RTH 08:30-15:00 entries only: the live bot trades RTH, and
    # all-session deciles put almost every RTH trade in the top bucket.
    fit = rth_window(fit)
    edges = np.unique(np.quantile(fit["atr_recent"], np.linspace(0, 1, q + 1)))
    b = np.clip(np.searchsorted(edges, fit["atr_recent"], side="right") - 1, 0, len(edges) - 2)
    unc = fit["right_censored"] == 0
    mfe = fit[unc].groupby(b[unc.values])["mfe_ticks"].median()
    mae = fit[unc].groupby(b[unc.values])["mae_ticks"].median()
    return {"atr_edges": [float(x) for x in edges],
            "mfe_median_ticks": [float(mfe.get(i, np.nan)) for i in range(len(edges) - 1)],
            "mae_median_ticks": [float(mae.get(i, np.nan)) for i in range(len(edges) - 1)],
            "rule": "bucket = clamp(searchsorted(atr_edges, atr_recent, right) - 1, 0, n-1)"}


def vol_predict(tbl, atr):
    e = np.array(tbl["atr_edges"])
    b = np.clip(np.searchsorted(e, atr, side="right") - 1, 0, len(e) - 2)
    return np.array(tbl["mfe_median_ticks"])[b], np.array(tbl["mae_median_ticks"])[b]


def choose_pooled(dev, ent, bars, xgb):
    """a,b for learned and vol_scaled from pooled out-of-fold test months."""
    folds = wf.make_folds(ent)
    grids = {"learned": [], "vol_scaled": []}
    firsts = dev[dev["is_first_signal_in_cluster"] == 1]
    for f in folds:
        fit = ent[ent["session_date"].isin(f["fit"])]
        val = ent[ent["session_date"].isin(f["valid"])]
        hd = {}
        for h in ("mfe_ticks", "mae_ticks"):
            fh = fit[(fit["right_censored"] == 0) & fit[h].notna()]
            vh = val[(val["right_censored"] == 0) & val[h].notna()]
            hd[h] = wf.fit_head(xgb, fh[wf.FEATURES], fh[h], vh[wf.FEATURES], vh[h], CFG)
        tsel = rth_window(firsts[firsts["session_date"].isin(f["test"])]).reset_index(drop=True)
        pm = np.expm1(hd["mfe_ticks"].predict(tsel[wf.FEATURES]))
        pa = np.expm1(hd["mae_ticks"].predict(tsel[wf.FEATURES]))
        vt = vol_table(fit)
        vm, va = vol_predict(vt, tsel["atr_recent"].values)
        for name, (m, a) in {"learned": (pm, pa), "vol_scaled": (vm, va)}.items():
            g = net_grid(tsel, bars, np.clip(np.outer(m, A), *CLAMP), np.clip(np.outer(a, B), *CLAMP))
            grids[name].append(g)
        print("  fold", f["month"], "done", flush=True)
    out = {}
    for name, gs in grids.items():
        m = np.nanmean(np.concatenate(gs, axis=0), axis=0)
        i, j = np.unravel_index(np.nanargmax(m), m.shape)
        out[name] = {"a": A[i], "b": B[j], "pooled_oof_net_ticks": round(float(m[i, j]), 2),
                     "grid_net_ticks": np.round(m, 2).tolist()}
    return out


def export_head(model, n, path, X_real):
    from onnxmltools import convert_xgboost
    from onnxmltools.convert.common.data_types import FloatTensorType
    import onnxruntime as ort
    import copy
    m = copy.deepcopy(model)
    m.get_booster().feature_names = None
    onx = convert_xgboost(m, initial_types=[("input", FloatTensorType([None, n]))])
    with open(path, "wb") as fh:
        fh.write(onx.SerializeToString())
    sess = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
    Xf = X_real.astype(np.float32)
    o = np.array(sess.run(None, {"input": Xf})[0]).ravel()
    x = model.predict(X_real).ravel()
    return {"rows": int(len(Xf)), "max_abs_diff_log": float(np.max(np.abs(o - x))),
            "max_abs_diff_ticks": float(np.max(np.abs(np.expm1(o) - np.expm1(x))))}


def run(dev_path, bars_path, out_dir):
    import xgboost as xgb
    os.makedirs(out_dir, exist_ok=True)
    dev = dataset.load_clean(dev_path)
    bars = replay.Bars.from_file(bars_path)
    ent = entries_mod.to_entries(dev, policy="first")

    print("choosing a,b on pooled out-of-fold test months ...", flush=True)
    choice = choose_pooled(dev, ent, bars, xgb)

    print("fitting final heads on all dev ...", flush=True)
    sessions = sorted(ent["session_date"].unique())
    valid_s = set(sessions[-wf.VALID_SESSIONS:])
    fit = ent[~ent["session_date"].isin(valid_s)]
    val = ent[ent["session_date"].isin(valid_s)]
    heads, parity = {}, {}
    rng = np.random.default_rng(3)
    for h, fname in (("mfe_ticks", "mfe_head.onnx"), ("mae_ticks", "mae_head.onnx")):
        fh = fit[(fit["right_censored"] == 0) & fit[h].notna()]
        vh = val[(val["right_censored"] == 0) & val[h].notna()]
        heads[h] = wf.fit_head(xgb, fh[wf.FEATURES], fh[h], vh[wf.FEATURES], vh[h], CFG)
        Xr = ent[wf.FEATURES].iloc[rng.choice(len(ent), 5000, replace=False)].to_numpy(dtype=np.float64)
        parity[h] = export_head(heads[h], len(wf.FEATURES), os.path.join(out_dir, fname), Xr)
        parity[h]["best_iteration"] = int(heads[h].best_iteration)
        heads[h].save_model(os.path.join(out_dir, fname.replace(".onnx", ".xgb.json")))
    vt = vol_table(ent)
    cfg = {
        "version": "paper_v1",
        "created_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "trained_on": {"dev_sessions": [sessions[0], sessions[-1]], "entries": int(len(ent)),
                       "holdout_used": False},
        "feature_order": wf.FEATURES,
        "onnx": {"input_name": "input", "input_shape": ["batch", len(wf.FEATURES)], "dtype": "float32",
                 "output": "log1p(ticks); apply expm1"},
        "clamp_ticks": list(CLAMP),
        "round_ticks": "round to nearest whole tick, then clamp",
        "policies": {
            "flat": {"tp_ticks": FLAT[0], "sl_ticks": FLAT[1]},
            "learned": {"a": choice["learned"]["a"], "b": choice["learned"]["b"],
                        "tp": "a * expm1(mfe_head)", "sl": "b * expm1(mae_head)"},
            "vol_scaled": {"a": choice["vol_scaled"]["a"], "b": choice["vol_scaled"]["b"],
                           "tp": "a * mfe_median_ticks[bucket]", "sl": "b * mae_median_ticks[bucket]",
                           "table": vt},
        },
        "selection_evidence": {k: {kk: vv for kk, vv in v.items() if kk != "grid_net_ticks"} for k, v in choice.items()},
        "grid_axes": {"a": A, "b": B},
    }
    json.dump(cfg, open(os.path.join(out_dir, "paper_config.json"), "w"), indent=1)
    json.dump({"parity": parity, "grids": {k: v["grid_net_ticks"] for k, v in choice.items()}},
              open(os.path.join(out_dir, "parity_report.json"), "w"), indent=1)
    return cfg, parity


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", required=True); ap.add_argument("--bars", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    cfg, parity = run(a.dev, a.bars, a.out)
    print(json.dumps({"policies": {k: {kk: vv for kk, vv in v.items() if kk != "table"} for k, v in cfg["policies"].items()},
                      "selection": cfg["selection_evidence"], "parity": parity}, indent=1))
