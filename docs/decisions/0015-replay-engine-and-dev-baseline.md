# 0015 — Realistic replay engine + the flat baseline on dev

Status: accepted · 11 Oct 2026 · dev only (the holdout was not read)

## Replay rules (`model/replay.py`, used for every policy)

- **Entry:** fill at the next bar's open (the bot runs OnBarClose and sends a market order).
- **TP/SL:** measured from the fill price.
- **No forced close:** a trade runs through the 16:00 halt and weekends, up to a 7-day cap.
- **Gap-through:** a bar that opens past a barrier fills at that open.
- **Same-bar tie:** a barrier the open is already past wins; otherwise SL first.
- **Timeout:** exit at the close of the first bar at or after the deadline, or at the reopen open if the deadline falls in a halt.
- **Position rules:** applied chronologically:
  - max contracts
  - contracts per entry
  - ignore opposite signals
  - EntriesPerDirection
- **Entry populations:**
  - `cluster_first`: the training table.
  - `filtered_flip`: the v2-live approximation, pending Matthew.
- **CIs:** day-block bootstrap.

## Validation

- **Matches the audit's independent replay** (flat 200/400, dev RTH):

  | Measure | This engine | Audit |
  |---|---|---|
  | Held past 60 min | 41.3% | 41.2% |
  | Held through the halt | 19.1% | 19.0% |
  | Held through a weekend | 4.3% | 4.28% |

- **The grid engine equals the trade-by-trade engine exactly**, for 200/400 and 40/40.
- **The timeout path is sane:** a 5-min timeout gives 100% timeouts with p99 hold of 5.0 min, and an infinite timeout equals no timeout.

## Result: flat 200/400 on dev RTH (Oct 13 – Jun 29), no commissions

Mean ticks per trade, with the day-block 95% CI:

| Entry population | No position limit | v2 as coded (1 position) | Max 7 contracts |
|---|---|---|---|
| cluster_first (40,013 entries) | +2.5 [−4.3, +9.2] | +1.0 [−12.1, +13.5] | −7.1 [−21.6, +7.0] |
| filtered_flip (1,260 entries) | −8.5 [−20.1, +5.3] | +3.1 [−22.9, +29.2] | −10.9 [−41.1, +21.7] |

- **No configuration shows an edge distinguishable from zero.** That holds before commissions (~1 tick round trip) and slippage on stops.
- **Month by month:**
  - cluster_first ranges from −5.3 to +14.3.
  - filtered_flip is negative in 6 of 9 months.
- **Stacking to 7 contracts makes it worse.** Extra same-direction entries add into moves that are going against us.
- **Best flat on dev:**
  - cluster_first: TP 400 / SL 800 at +17.2 [−3.3, +38.1]. It holds trades a median of 255 min, 50% through the halt, with a −62,000-tick worst day across all entries. That is regime drift, not a usable setting.
  - filtered_flip: TP 100 / SL 800 at +8.3 [−13.0, +29.1].
  - Both are in-sample and not significant.
- **The pilot's +7.2 ticks/entry** (5 days, 60-min truncated) is superseded.

## Consequences

- **What v3 has to do changes.** A per-trade exit can only add value here if the model can tell, at entry, which trades will run and which won't. That is, if predicted MFE vs MAE separates winners from losers on dev folds. The training report must show this directly, not just head accuracy.
- **Open decisions** (after Matthew confirms the live entry rule):
  - whether v3 may "skip" a trade (an exit so tight it is effectively no trade)
  - whether stacking up to 7 contracts should continue
- **Commissions and stop slippage** become tester parameters before any final number is quoted.
