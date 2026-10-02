#!/usr/bin/env python3
"""
mutation_tests.py — acceptance suite for validate.py (rev 3).

Generates a clean synthetic dataset, confirms it passes STRICT validation with
exit 0, then produces a series of deliberately damaged datasets and confirms the
validator rejects each with a non-zero exit code and the expected error. Also
confirms the boundary demonstration still passes.

Run:  python3 scripts/mutation_tests.py
Exit code is 0 only if every case behaves as required.
"""
from __future__ import annotations
import os, shutil, subprocess, sys, glob, json
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = "/tmp/mut"


TPL = "CME US Index Futures ETH"   # matches the synthetic meta (Central-time ETH)

def run_validator(dir_path, allow_incomplete=False, pilot=False, expect_template=None):
    cmd = [sys.executable, "-m", "data_pipeline.validate", "--dir", dir_path]
    if allow_incomplete:
        cmd.append("--allow-incomplete")
    if pilot:
        cmd.append("--pilot")
    if expect_template:
        cmd += ["--expect-template", expect_template]
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def files(dir_path):
    sig = glob.glob(os.path.join(dir_path, "signals_*.csv"))[0]
    bars = glob.glob(os.path.join(dir_path, "bars_*.csv"))[0]
    meta = glob.glob(os.path.join(dir_path, "*.meta.json"))[0]
    return sig, bars, meta


def fresh_clean(name):
    d = os.path.join(WORK, name)
    if os.path.exists(d):
        shutil.rmtree(d)
    os.makedirs(d)
    subprocess.run([sys.executable, "scripts/make_sample_data.py", "--out", d],
                   cwd=ROOT, capture_output=True, text=True, check=True)
    return d


RESULTS = []
def record(name, ok, detail):
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} — {detail}")


def expect_pass(name, dir_path, allow_incomplete=False, must_contain=None):
    rc, out = run_validator(dir_path, allow_incomplete)
    ok = (rc == 0) and (must_contain is None or must_contain in out)
    record(name, ok, f"exit={rc} (expected 0)" + ("" if ok or must_contain is None
           else f"; missing '{must_contain}'"))


def expect_fail(name, dir_path, signature, allow_incomplete=False):
    rc, out = run_validator(dir_path, allow_incomplete)
    ok = (rc != 0) and (signature.lower() in out.lower())
    detail = f"exit={rc} (expected nonzero)"
    if rc != 0 and not ok:
        detail += f"; expected error signature '{signature}' not found"
    record(name, ok, detail)


def main():
    if os.path.exists(WORK):
        shutil.rmtree(WORK)
    os.makedirs(WORK)
    print("=" * 68)
    print("validator acceptance + mutation suite (rev 3)")
    print("=" * 68)

    # 1) clean dataset passes strict, and reports clean completion
    clean = fresh_clean("clean")
    expect_pass("clean dataset passes strict", clean,
                must_contain="run metadata OK (completed")

    # 2) bars file contains an extra rogue run -> pairing error
    d = fresh_clean("rogue_run")
    sig, bars, meta = files(d)
    b = pd.read_csv(bars, dtype={"run_id": str})
    rogue = b.tail(3).copy()
    rogue["run_id"] = "run_ROGUE"
    pd.concat([b, rogue], ignore_index=True).to_csv(bars, index=False)
    expect_fail("bars has extra rogue run", d, "pairing MISMATCH")

    # 3) bars file missing -> strict error (and allow-incomplete passes)
    d = fresh_clean("no_bars")
    _, bars, _ = files(d)
    os.remove(bars)
    expect_fail("bars missing (strict)", d, "bars file")
    expect_pass("bars missing (--allow-incomplete)", d, allow_incomplete=True)

    # 4) run metadata missing -> strict error
    d = fresh_clean("no_meta")
    _, _, meta = files(d)
    os.remove(meta)
    expect_fail("metadata missing (strict)", d, "metadata")

    # 5) duplicate bar timestamp -> error
    d = fresh_clean("dup_bar")
    _, bars, _ = files(d)
    b = pd.read_csv(bars, dtype={"run_id": str})
    b = pd.concat([b, b.iloc[[100]]], ignore_index=True)
    b.to_csv(bars, index=False)
    expect_fail("duplicate bar timestamp", d, "duplicate bar timestamp")

    # 6) invalid OHLC (high below low) -> error
    d = fresh_clean("bad_ohlc")
    _, bars, _ = files(d)
    b = pd.read_csv(bars, dtype={"run_id": str})
    b.loc[150, "high"] = b.loc[150, "low"] - 5.0
    b.to_csv(bars, index=False)
    expect_fail("invalid OHLC relationship", d, "invalid OHLC")

    # 7) signal_reference_price differs from signal bar close -> error
    d = fresh_clean("ref_drift")
    sig, _, _ = files(d)
    s = pd.read_csv(sig, dtype={"signal_id": str, "run_id": str})
    s.loc[0, "signal_reference_price"] = s.loc[0, "signal_reference_price"] + 1.25
    s.to_csv(sig, index=False)
    expect_fail("reference price drift", d, "reference_price")

    # 8) run left 'running' (not cleanly terminated) -> error
    d = fresh_clean("running")
    _, _, meta = files(d)
    m = json.load(open(meta)); m["completion_status"] = "running"
    json.dump(m, open(meta, "w"), indent=2)
    expect_fail("run not cleanly completed", d, "completion_status")

    # 9) metadata reports write errors -> error
    d = fresh_clean("write_errs")
    _, _, meta = files(d)
    m = json.load(open(meta)); m["bar_write_errors"] = 7
    json.dump(m, open(meta, "w"), indent=2)
    expect_fail("nonzero write errors", d, "bar_write_errors")

    # 9b) window_complete rows with zero tick updates -> error (fabricated labels)
    d = fresh_clean("dead_ticks")
    sig, _, _ = files(d)
    s = pd.read_csv(sig, dtype={"signal_id": str, "run_id": str})
    s.loc[s["finalize_reason"] == "window_complete", "tick_updates"] = 0
    s.to_csv(sig, index=False)
    expect_fail("window_complete with 0 tick updates", d, "0 tick updates")

    # ---- round 4: one case per corruption Matthew listed ----
    def clean_sig(name):
        d = fresh_clean(name); sig, bars, meta = files(d)
        return d, sig, bars, meta

    # deleted bar row -> meta count no longer reconciles
    d, sig, bars, meta = clean_sig("deleted_bar")
    b = pd.read_csv(bars, dtype={"run_id": str}); b.iloc[:-1].to_csv(bars, index=False)
    expect_fail("deleted bar row", d, "bar_count")

    # wrong 15-minute timeframe
    d, sig, bars, meta = clean_sig("wrong_tf")
    for f in (sig, bars):
        x = pd.read_csv(f, dtype={"run_id": str}); x["bar_period"] = "15m"; x.to_csv(f, index=False)
    m = json.load(open(meta)); m["bar_period"] = "15m"; json.dump(m, open(meta, "w"))
    expect_fail("wrong 15m timeframe", d, "wrong timeframe")

    # non-numeric volume
    d, sig, bars, meta = clean_sig("nonnum_vol")
    b = pd.read_csv(bars, dtype={"run_id": str}); b["volume"] = b["volume"].astype(object)
    b.loc[50, "volume"] = "NaNaN"; b.to_csv(bars, index=False)
    expect_fail("non-numeric volume", d, "non-numeric")

    # MFE inflated by 1000 ticks
    d, sig, bars, meta = clean_sig("mfe_inflated")
    s = pd.read_csv(sig, dtype={"signal_id": str, "run_id": str})
    s.loc[0, "mfe_ticks"] = s.loc[0, "mfe_ticks"] + 1000; s.to_csv(sig, index=False)
    expect_fail("MFE inflated by 1000 ticks", d, "inflated")

    # fabricated final delta
    d, sig, bars, meta = clean_sig("fab_delta")
    s = pd.read_csv(sig, dtype={"signal_id": str, "run_id": str})
    s.loc[0, "final_delta_ticks"] = s.loc[0, "final_delta_ticks"] + 50; s.to_csv(sig, index=False)
    expect_fail("fabricated final delta", d, "fabricated final delta")

    # negative minutes_to_mfe
    d, sig, bars, meta = clean_sig("neg_minutes")
    s = pd.read_csv(sig, dtype={"signal_id": str, "run_id": str})
    s.loc[s["mfe_ticks"] > 0, "minutes_to_mfe"] = -3.0; s.to_csv(sig, index=False)
    expect_fail("negative minutes_to_mfe", d, "negative minutes")

    # invalid finalize_reason
    d, sig, bars, meta = clean_sig("bad_reason")
    s = pd.read_csv(sig, dtype={"signal_id": str, "run_id": str})
    s.loc[0, "finalize_reason"] = "made_up"; s.to_csv(sig, index=False)
    expect_fail("invalid finalize_reason", d, "invalid finalize_reason")

    # reversed direction text
    d, sig, bars, meta = clean_sig("rev_dir")
    s = pd.read_csv(sig, dtype={"signal_id": str, "run_id": str})
    s["direction"] = s["direction"].map({"LONG": "SHORT", "SHORT": "LONG"}); s.to_csv(sig, index=False)
    expect_fail("reversed direction text", d, "reversed vs is_long")

    # falsified session tag
    d, sig, bars, meta = clean_sig("fake_tag")
    s = pd.read_csv(sig, dtype={"signal_id": str, "run_id": str})
    s.loc[0, "session_tag"] = "OVERNIGHT" if s.loc[0, "session_tag"] == "RTH" else "RTH"
    s.to_csv(sig, index=False)
    expect_fail("falsified session tag", d, "session_tag inconsistent")

    # falsified session date
    d, sig, bars, meta = clean_sig("fake_date")
    s = pd.read_csv(sig, dtype={"signal_id": str, "run_id": str})
    s.loc[0, "session_date"] = "2020-01-01"; s.to_csv(sig, index=False)
    expect_fail("falsified session date", d, "session_date not within")

    # flipped mae_before_mfe
    d, sig, bars, meta = clean_sig("flip_mbm")
    s = pd.read_csv(sig, dtype={"signal_id": str, "run_id": str})
    m2 = (s["mfe_ticks"] > 0) & (s["mae_ticks"] > 0)
    idx = s[m2].index[0]
    s.loc[idx, "mae_before_mfe"] = 1 - int(s.loc[idx, "mae_before_mfe"]); s.to_csv(sig, index=False)
    expect_fail("flipped mae_before_mfe", d, "mae_before_mfe")

    # future horizon populated after censoring
    d, sig, bars, meta = clean_sig("future_horizon")
    s = pd.read_csv(sig, dtype={"signal_id": str, "run_id": str})
    cens = s[s["finalize_reason"] != "window_complete"].index[0]
    s.loc[cens, "delta_60m"] = 7.0; s.to_csv(sig, index=False)
    expect_fail("future horizon after censor", d, "beyond the covered window")

    # fabricated final price
    d, sig, bars, meta = clean_sig("fab_price")
    s = pd.read_csv(sig, dtype={"signal_id": str, "run_id": str})
    s.loc[0, "final_price"] = 99999.0; s.to_csv(sig, index=False)
    expect_fail("fabricated final price", d, "final_price outside")

    # signal-count mismatch in metadata
    d, sig, bars, meta = clean_sig("sig_count")
    m = json.load(open(meta)); m["signal_count"] = m["signal_count"] + 5
    json.dump(m, open(meta, "w")); expect_fail("metadata signal-count mismatch", d, "signal_count")

    # infinite OHLC value
    d, sig, bars, meta = clean_sig("inf_ohlc")
    b = pd.read_csv(bars, dtype={"run_id": str}); b["high"] = b["high"].astype(object)
    b.loc[100, "high"] = "inf"; b.to_csv(bars, index=False)
    expect_fail("infinite OHLC value", d, "infinite")

    # inflated MAE (separate from MFE)
    d, sig, bars, meta = clean_sig("mae_inflated")
    s = pd.read_csv(sig, dtype={"signal_id": str, "run_id": str})
    s.loc[0, "mae_ticks"] = s.loc[0, "mae_ticks"] + 1000; s.to_csv(sig, index=False)
    expect_fail("MAE inflated by 1000 ticks", d, "inflated")

    # simulated writer-close failure recorded in metadata -> failed status
    d, sig, bars, meta = clean_sig("close_fail")
    m = json.load(open(meta)); m["close_write_errors"] = 2; m["completion_status"] = "failed"
    json.dump(m, open(meta, "w")); expect_fail("writer-close failure (failed status)", d, "close_write_errors")

    # clean dataset passes --pilot (template pinned) -> exit 0
    d = fresh_clean("clean_pilot")
    rc, out = run_validator(d, pilot=True, expect_template=TPL)
    record("clean dataset passes --pilot", rc == 0, f"exit={rc} (expected 0)")

    # template is pinned in schema, so --pilot enforces it even without the flag
    rc, out = run_validator(d, pilot=True)
    record("pilot uses pinned schema template (clean passes)", rc == 0,
           f"exit={rc} (expected 0)")

    # tick coverage does not span the bar range (pilot)
    d, sig, bars, meta = clean_sig("tick_gap")
    m = json.load(open(meta)); m["last_tick_time"] = "2024-06-03T10:00:00"
    json.dump(m, open(meta, "w"))
    rc, out = run_validator(d, pilot=True, expect_template=TPL)
    record("tick range not covering bars (pilot)", rc != 0 and "span" in out,
           f"exit={rc} (expected nonzero)")

    # each locked pilot value, when altered, must fail --pilot
    for key, badval, label in [
        ("window_minutes", 30, "window_minutes"),
        ("neighbors_count", 99, "neighbors_count"),
        ("max_bars_back", 100, "max_bars_back"),
        ("classifier_session_start", 0, "classifier_session_start"),
        ("classifier_session_end", 2359, "classifier_session_end"),
        ("rth_start", 0, "rth_start"),
    ]:
        d, sig, bars, meta = clean_sig("pilot_" + label)
        m = json.load(open(meta)); m[key] = badval; json.dump(m, open(meta, "w"))
        rc, out = run_validator(d, pilot=True, expect_template=TPL)
        record(f"pilot lock: {label}", rc != 0 and "pilot config" in out,
               f"exit={rc} (expected nonzero)")

    # wrong trading-hours template must fail --pilot
    d, sig, bars, meta = clean_sig("pilot_tpl")
    m = json.load(open(meta)); m["trading_hours_template"] = "Completely Wrong"
    json.dump(m, open(meta, "w"))
    rc, out = run_validator(d, pilot=True, expect_template=TPL)
    record("pilot lock: trading_hours_template", rc != 0 and "trading_hours_template" in out,
           f"exit={rc} (expected nonzero)")

    # window_complete marked right_censored
    d, sig, bars, meta = clean_sig("wc_censored")
    s = pd.read_csv(sig, dtype={"signal_id": str, "run_id": str})
    wc = s[s["finalize_reason"] == "window_complete"].index[0]
    s.loc[wc, "right_censored"] = 1; s.to_csv(sig, index=False)
    expect_fail("window_complete marked censored", d, "window_complete rows exist with right_censored")

    # 10) boundary demonstration still passes
    p = subprocess.run([sys.executable, "scripts/boundary_demo.py"],
                       cwd=ROOT, capture_output=True, text=True)
    record("boundary demo (post-window ticks ignored)", p.returncode == 0,
           f"exit={p.returncode} (expected 0)")

    print("-" * 68)
    n_fail = sum(1 for _, ok, _ in RESULTS if not ok)
    print(f"{len(RESULTS)-n_fail}/{len(RESULTS)} cases passed.")
    print("ACCEPTANCE SUITE " + ("PASSED" if n_fail == 0 else "FAILED"))
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
