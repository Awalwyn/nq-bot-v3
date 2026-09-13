"""
Canonical schema for the v3 logger output.

SINGLE SOURCE OF TRUTH. Every column here matches, in order, what
ninjascript/SignalLogger.cs writes. If you add/rename a column in the .cs,
change it here in the SAME commit so validate.py and train.py stay in sync.

Column groups:
  IDENTITY   - who/when this observation is. Never fed to the model as a feature.
  CLUSTER    - decision 1 bookkeeping. Used for weighting/dedup at train time.
  FEATURE    - point-in-time, knowable at signal_time. Safe to train on.
  EXECUTION  - decision 3 state flags. Not features; used for later analysis.
  LABEL      - forward-looking outcomes. Targets, never inputs. (leakage if used as X)
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# signals_<run_id>.csv
# ---------------------------------------------------------------------------

IDENTITY = [
    "signal_id",
    "run_id",
    "instrument",          # full contract, e.g. "NQ 12-26" (review fix 4)
    "instrument_master",   # "NQ"
    "expiry",              # contract expiry date, if available
    "tick_size",
    "bar_period",
    "signal_time",
    "session_date",
    "session_tag",
    "score",
    "direction",
    "is_long",
    "exact_score_sequence_position",
]

CLUSTER = [
    "signal_cluster_id",
    "is_first_signal_in_cluster",
    "bars_since_cluster_start",
    "bars_since_last_same_dir_signal",
    "previous_prediction_score",
    "score_changed_from_prior_bar",
    "is_new_score_extreme",
]

# signal_reference_price is the anchor for every label. It is knowable at
# signal_time (it IS the signal-bar close), so it's a feature/context column,
# not a label.
REFERENCE = [
    "signal_reference_price",
]

# NOTE ordering: the logger writes EXECUTION between the filter block and the
# extra features, so we keep two feature sub-lists and place EXECUTION between
# them in SIGNALS_COLUMNS. FEATURE (below) is their union, for model code.
FILTER_FEATURES = [
    # filters: pass flag + raw value(s)
    "volatility_pass", "atr_recent", "atr_historical", "atr_ratio",
    "regime_pass", "regime_value",
    "adx_pass", "adx_value",
    "ema200_pass", "ema200_value", "dist_ema200_ticks", "dist_ema200_atr",
    "ema800_pass", "ema800_value", "dist_ema800_ticks", "dist_ema800_atr",
    "sma200_pass", "sma200_value", "dist_sma200_ticks",
    "kernel_pass", "kernel_value", "kernel_gaussian", "kernel_slope_ticks",
    "price_minus_kernel_ticks",
    "session_pass", "entry_filters_passed", "filter_config",
]

EXECUTION = [
    "candidate_executed_live",
    "manual_intervention",
]

EXTRA_FEATURES = [
    "rsi", "cci", "dist_vwap_ticks", "minutes_since_rth_open", "minute_of_day",
    "day_of_week", "candle_run", "body_to_range", "volume", "volume_ratio",
    "bar_range_ticks",
]

# Union of point-in-time features (safe-to-train grouping).
FEATURE = FILTER_FEATURES + EXTRA_FEATURES

LABEL = [
    "mfe_ticks", "mae_ticks", "minutes_to_mfe", "minutes_to_mae", "mae_before_mfe",
    "delta_1m", "delta_3m", "delta_5m", "delta_10m", "delta_15m", "delta_30m",
    "delta_60m",
    "final_delta_ticks", "final_price",
    "window_end_scheduled",   # signal_time + window_minutes (uncut target)
    "window_end_actual",      # actual finalization time (last in-window tick)
    "right_censored", "window_minutes", "finalize_reason",
]

# Full ordered column list, exactly as written by SignalLogger.cs.
# Order: identity, cluster, reference, filter features, EXECUTION, extra
# features, labels.
SIGNALS_COLUMNS = (
    IDENTITY + CLUSTER + REFERENCE + FILTER_FEATURES + EXECUTION
    + EXTRA_FEATURES + LABEL
)

# Horizon columns and the minutes they correspond to (kept together so
# validate.py can reason about censoring vs available horizon).
HORIZON_COLUMNS = {
    "delta_1m": 1, "delta_3m": 3, "delta_5m": 5, "delta_10m": 10,
    "delta_15m": 15, "delta_30m": 30, "delta_60m": 60,
}

# 0/1 boolean-encoded columns
BOOL_COLUMNS = [
    "is_long",
    "is_first_signal_in_cluster", "score_changed_from_prior_bar", "is_new_score_extreme",
    "volatility_pass", "regime_pass", "adx_pass", "ema200_pass", "ema800_pass",
    "sma200_pass", "kernel_pass", "session_pass", "entry_filters_passed",
    "candidate_executed_live", "manual_intervention",
    "mae_before_mfe", "right_censored",
]

# Columns that must never be NaN (identity + structural). Label columns MAY be
# NaN (e.g. a horizon past a right-censor point).
REQUIRED_NON_NULL = [
    "signal_id", "run_id", "instrument", "tick_size", "signal_time", "session_date",
    "session_tag", "score", "direction", "is_long", "signal_reference_price",
    "signal_cluster_id",
]

# The individual filter pass columns that compose entry_filters_passed,
# keyed by the token that appears in filter_config.
FILTER_COMPONENTS = {
    "vol": "volatility_pass",
    "regime": "regime_pass",
    "adx": "adx_pass",
    "ema200": "ema200_pass",
    "ema800": "ema800_pass",
    "sma200": "sma200_pass",
    "kernel": "kernel_pass",
}

# ---------------------------------------------------------------------------
# bars_<run_id>.csv
# ---------------------------------------------------------------------------

BARS_COLUMNS = [
    "run_id", "instrument", "instrument_master", "expiry", "tick_size",
    "bar_period", "bar_time", "session_date", "session_tag",
    "open", "high", "low", "close", "volume",
]

# ---------------------------------------------------------------------------
# Convenience sets for model code (import these, don't hardcode).
# ---------------------------------------------------------------------------

# Columns that are safe to consider as model inputs (X). Note filter_config,
# day_of_week are categorical/string and need encoding; everything else numeric.
MODEL_FEATURE_CANDIDATES = [c for c in FEATURE if c not in ("filter_config", "day_of_week")]
CATEGORICAL_FEATURES = ["filter_config", "day_of_week"]

# Columns that must NEVER appear in X (would leak the future).
FORBIDDEN_AS_FEATURES = set(LABEL)

VALID_SESSION_TAGS = {"RTH", "OVERNIGHT"}
SCORE_MIN_ABS_DEFAULT = 4
SCORE_MAX_ABS_DEFAULT = 8
