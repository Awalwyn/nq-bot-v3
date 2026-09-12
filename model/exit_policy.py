"""
exit_policy.py — deterministic map from model predictions to an exit.

Given the model's per-signal predictions (expected favorable/adverse excursion
and timing), produce concrete exit parameters: take-profit, stop, timeout, and
an optional trail. Deterministic and dependency-light on purpose — this same
logic gets ported to C# to run live inside NinjaTrader, so keep it portable.

ALL band coefficients below are PLACEHOLDERS. Their real values are fit on real
data in a later step (that's the whole reason the logger collects raw
excursions). The STRUCTURE — how a prediction becomes an exit, and how it plugs
into the replay evaluator — is what's built and tested here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ExitDecision:
    tp_ticks: float
    sl_ticks: float
    timeout_min: float | None = None
    trail_ticks: float | None = None


@dataclass
class ExitPolicyConfig:
    # ---- PLACEHOLDER coefficients (tune on real data) --------------------
    capture_fraction: float = 0.70   # aim to bank this fraction of expected MFE
    stop_multiple: float = 1.10      # stop a bit beyond expected MAE
    timeout_multiple: float = 2.00   # allow ~2x the expected time-to-MFE
    # clamps keep any single prediction from producing an absurd exit
    min_tp: float = 8.0
    max_tp: float = 400.0
    min_sl: float = 8.0
    max_sl: float = 600.0
    min_timeout_min: float = 5.0
    max_timeout_min: float = 60.0
    use_trail: bool = False
    trail_fraction: float = 0.50     # trail distance as fraction of tp


class ExitPolicy:
    def __init__(self, config: ExitPolicyConfig | None = None):
        self.cfg = config or ExitPolicyConfig()

    def decide(self, pred: dict) -> ExitDecision:
        """pred keys: expected_mfe_ticks, expected_mae_ticks,
        expected_minutes_to_mfe. Missing keys fall back to the clamp minimums."""
        c = self.cfg
        mfe = float(pred.get("expected_mfe_ticks", c.min_tp))
        mae = float(pred.get("expected_mae_ticks", c.min_sl))
        tmin = float(pred.get("expected_minutes_to_mfe", c.min_timeout_min))

        tp = _clamp(mfe * c.capture_fraction, c.min_tp, c.max_tp)
        sl = _clamp(mae * c.stop_multiple, c.min_sl, c.max_sl)
        timeout = _clamp(tmin * c.timeout_multiple, c.min_timeout_min, c.max_timeout_min)
        trail = round(tp * c.trail_fraction, 2) if c.use_trail else None

        return ExitDecision(round(tp, 2), round(sl, 2), round(timeout, 2), trail)

    def as_decide_fn(self, predict_fn):
        """Adapt to evaluate.replay's decide signature: row -> (tp, sl, timeout_bars).

        predict_fn(row) -> predictions dict. Later this wraps the trained model;
        for testing it can wrap any stand-in.
        """
        from model.evaluate import BARS_PER_MIN

        def decide(row):
            d = self.decide(predict_fn(row))
            tb = int(d.timeout_min * BARS_PER_MIN) if d.timeout_min else None
            return d.tp_ticks, d.sl_ticks, tb
        decide.__name__ = "learned_exit"
        return decide


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


if __name__ == "__main__":
    # structural test: a few example predictions -> exits
    pol = ExitPolicy()
    for pred in [
        {"expected_mfe_ticks": 40, "expected_mae_ticks": 15, "expected_minutes_to_mfe": 8},
        {"expected_mfe_ticks": 5,  "expected_mae_ticks": 3,  "expected_minutes_to_mfe": 1},
        {"expected_mfe_ticks": 800, "expected_mae_ticks": 900, "expected_minutes_to_mfe": 90},
    ]:
        print(pred, "->", pol.decide(pred))
