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
    "tick_updates",           # in-window tick updates seen; data-fidelity signal
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

# ---------------------------------------------------------------------------
# Strict-validation contracts (review round 3 — validator hardening)
# ---------------------------------------------------------------------------

# Keys the run metadata MUST carry for strict (pilot / production) validation.
# The logger writes all of these; strict mode errors if any are absent.
META_REQUIRED_KEYS = [
    "run_id", "instrument_full", "instrument_master", "tick_size", "bar_period",
    "timezone_id", "trading_hours_template", "min_abs_score", "max_abs_score",
    "window_minutes", "created_utc", "completed_utc", "completion_status",
    "termination_reason", "signal_count", "bar_count", "tick_count",
    "signal_write_errors", "bar_write_errors", "close_write_errors",
    "first_bar_time", "last_bar_time", "first_tick_time", "last_tick_time",
]

# The value each run is expected to have collected on. A run on any other
# timeframe (e.g. a 15-minute chart) fails strict validation.
EXPECTED_BAR_PERIOD = "15s"

# Locked pilot configuration. Under --pilot, the run metadata must match all of
# these exactly, so a fully self-consistent but wrong-config dataset (e.g. a
# 15-minute run) cannot pass just because its three files agree with each other.
PILOT_CONFIG = {
    "instrument_master": "NQ",
    "tick_size": 0.25,
    "bar_period": "15s",
    "min_abs_score": 4,
    "max_abs_score": 8,
    "window_minutes": 60,
    "cut_at_rth_close": True,
    "log_all_sessions": True,
    "write_bars_file": True,
    "rth_start": 830,
    "rth_end": 1500,
    "classifier_session_start": 830,
    "classifier_session_end": 1500,
    "neighbors_count": 8,
    "max_bars_back": 2000,
}

# The exact Trading Hours template the live bot uses. Chart timezone is Central;
# the entry model runs 08:30–15:00 CT, so the logger RTH is 0830–1500 to match.
EXPECTED_TRADING_HOURS_TEMPLATE = "CME US Index Futures ETH"

# The timezone NinjaTrader must record for a Central-time chart. --pilot rejects
# any other timezone_id (e.g. an Eastern chart would shift the whole session an
# hour). Confirmed against the real pilot metadata. Override with --expect-timezone.
EXPECTED_TIMEZONE_ID = "Central Standard Time"

# Pilot fails if the median tick activity is below this (tick updates per
# window-minute). Documented threshold rather than eyeballing the output.
PILOT_MIN_TICKS_PER_MIN = 2.0

# Allowed enumerations — anything else is a corrupted/fabricated value.
VALID_FINALIZE_REASONS = {"window_complete", "rth_close", "terminated"}
VALID_DIRECTIONS = {"long", "short"}

# final_delta_ticks must equal (final_price - reference)/tick * dir within this.
FINAL_DELTA_TOLERANCE_TICKS = 0.5

# A run is only acceptable to strict validation when it finished cleanly.
META_COMPLETION_OK = "completed"

# signal_reference_price must equal the signal bar's close within this many
# ticks. It IS that close by construction, so any real drift is a logging bug.
REF_PRICE_TOLERANCE_TICKS = 0.5

# Bar columns that must be present and non-null on every row.
BARS_REQUIRED_NON_NULL = [
    "run_id", "instrument", "bar_period", "tick_size", "bar_time",
    "open", "high", "low", "close", "volume",
]
