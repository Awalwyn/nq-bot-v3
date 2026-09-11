# 0001 — Signal logging: broad-and-raw collection

Status: accepted

## Context
Before deciding which signals, filters, or clusters are "good," we need the
broadest clean raw dataset. Premature filtering throws away the evidence needed
to answer those questions later.

## Decisions
1. **Trigger** — one observation per completed 15s bar with Lorentzian score
   in +4..+8 or -4..-8. No dedup. Consecutive in-band bars each log. Cluster
   fields (signal_cluster_id, is_first_signal_in_cluster, bars_since_cluster_start,
   bars_since_last_same_dir_signal, previous_prediction_score,
   score_changed_from_prior_bar, is_new_score_extreme, exact_score_sequence_position)
   are recorded so dedup/weighting/autocorrelation are handled at train time.
   A cluster ends on a neutral score (-3..+3) or a flip to the opposite direction.
2. **Filters** — each filter stored individually (pass + raw value) plus a
   composite entry_filters_passed. Filters never gate logging.
3. **Execution states** — entry_filters_passed, candidate_executed_live,
   manual_intervention kept as three separate fields.
4. **Reference price** — the completed 15s signal-bar close. All research labels
   measured from it; live fill/slippage handled separately later.
5. **Direction-normalized outcomes** — favorable/adverse relative to signal
   direction; MFE/MAE as positive tick magnitudes; raw path preserved (bars file).
6. **Sessions** — log all sessions, tag RTH/OVERNIGHT; train RTH-only first.
7. **IDs** — deterministic signal_id (stable across replays) + a per-run run_id.

## Consequence
The dataset is intentionally huge and mostly discarded later. Reconstructability
is preserved via two joinable files (signals + bars). See ninjascript/SignalLogger.cs
and data_pipeline/schema.py.
