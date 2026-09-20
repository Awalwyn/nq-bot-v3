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


def run_validator(dir_path, allow_incomplete=False):
    cmd = [sys.executable, "-m", "data_pipeline.validate", "--dir", dir_path]
    if allow_incomplete:
        cmd.append("--allow-incomplete")
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
