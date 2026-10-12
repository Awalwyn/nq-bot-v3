# 0018 — Paper trading v1: three exit policies side by side

Status: accepted · 12 Oct 2026

## Context

Dev results (ADRs 0016–0017) show no expectancy edge for learned exits, and only a risk benefit that a volatility lookup also delivers. The team's real results (passed evals and prefunded thresholds) disagree with the replay's near-zero edge, and the live entry rule has only been approximated. Avery chose to move forward and settle both questions in paper trading, adjusting from there.

## Decisions

1. **Paper trade three exit policies side by side on identical entries.** Each runs as its own instance of `NQBotV3Paper` on its own Sim account:
   - **Flat:** TP 200 / SL 400.
   - **VolScaled:** TP = 0.75 · MFE_table[ATR decile], SL = 3.0 · MAE_table[ATR decile]. The table comes from RTH dev entries.
   - **Learned:** TP = 1.0 · expm1(MFE head), SL = 3.0 · expm1(MAE head).
   - All values are rounded to whole ticks and clamped to [8, 800].
2. **Entries are the real live rule, not an approximation.** The strategy uses `MLLorentzianClassification.StartLongTrade` / `StartShortTrade` with the v2 settings (7 filters, ±4 thresholds, 08:30–15:00). `EntriesPerDirection = 1` by default, with no forced close. Position rules are parameters until Matthew confirms the live setup.
3. **Features are computed by code copied from SignalLogger.cs**, the exact formulas the model was trained on.
4. **The models ship as generated C#, not ONNX.** `model/trees_to_csharp.py` compiles the depth-3 trees (610 and 758) into `NQBotV3Model.cs`, which avoids adding ONNX Runtime DLLs to NT8.
   - Parity on 3,000 real dev rows, 100 of them with missing values, compiled and run with mono: max |C# − XGBoost| = 0.0021 ticks (MFE) and 0.0032 ticks (MAE).
   - ONNX files are still exported, with parity of 0.003 ticks, for reference.
5. **How the frozen settings were chosen** (`model/build_paper.py`, dev only, holdout unused):
   - **Heads:** fit on all dev entries except the last 20 sessions, which were used for early stopping.
   - **a, b:** the best pooled out-of-fold test-month net ticks. Learned scored +3.4 and vol-scaled +6.0. Those numbers were chosen on the same months they are scored on, so they are optimistic. Paper trading is the real test.
   - **Grid edge:** b = 3.0 sits at the edge of the grid. Wide stops were preferred on dev, consistent with the flat grid.
6. **Logging.** Every taken or skipped signal, entry fill and exit fill goes to `C:\ProgramData\nqbotv3\data\paper\paper_<policy>_<account>_<start>.csv`. Each row carries all 30 features, the predictions, the TP/SL and a historical/realtime flag, so paper trades can be reconciled bar by bar with the replay engine and the logger.
7. **Frozen artifacts** live in `model_artifacts/paper_v1/`: the config, both models as XGBoost JSON, and the parity report. `ninjascript/NQBotV3Paper.cs` is rendered from the template by `scripts/render_paper_strategy.py`.

## What paper trading must answer

1. **Do paper entries match the logger and replay on the same bars?** Check the features, entry time and price. This also gives the real live entry population.
2. **Do paper fills match the replay's fills for the same entries?** Check gap and tie handling and slippage.
3. **After enough trades, which policy has better net expectancy and drawdown?** There are about 7 entries a day, so a firm answer takes 2–3 months. The first weeks validate the mechanics.
