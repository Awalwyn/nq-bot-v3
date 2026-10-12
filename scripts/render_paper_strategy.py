"""Render ninjascript/NQBotV3Paper.cs from the template + a frozen paper_config.json.

    python scripts/render_paper_strategy.py --config <paper_v1>/paper_config.json
"""
import argparse, json, os
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def fmt(v): return repr(float(v))

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--config", required=True)
    ap.add_argument("--out", default=os.path.join(HERE, "ninjascript", "NQBotV3Paper.cs"))
    a = ap.parse_args()
    c = json.load(open(a.config)); P = c["policies"]; t = P["vol_scaled"]["table"]
    vals = {
        "VERSION": c["version"],
        "FLAT_TP": fmt(P["flat"]["tp_ticks"]), "FLAT_SL": fmt(P["flat"]["sl_ticks"]),
        "LEARNED_A": fmt(P["learned"]["a"]), "LEARNED_B": fmt(P["learned"]["b"]),
        "VOL_A": fmt(P["vol_scaled"]["a"]), "VOL_B": fmt(P["vol_scaled"]["b"]),
        "CLAMP_LO": fmt(c["clamp_ticks"][0]), "CLAMP_HI": fmt(c["clamp_ticks"][1]),
        "VOL_EDGES": ", ".join(fmt(x) for x in t["atr_edges"]),
        "VOL_MFE": ", ".join(fmt(x) for x in t["mfe_median_ticks"]),
        "VOL_MAE": ", ".join(fmt(x) for x in t["mae_median_ticks"]),
    }
    src = open(os.path.join(HERE, "ninjascript", "templates", "NQBotV3Paper.cs.tmpl")).read()
    for k, v in vals.items():
        src = src.replace("{{" + k + "}}", v)
    assert "{{" not in src, "unfilled placeholder"
    with open(a.out, "w", newline="\n") as fh: fh.write(src)
    print("wrote", a.out, vals)

if __name__ == "__main__": main()
