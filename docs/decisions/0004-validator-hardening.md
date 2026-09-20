# 0004 — Validator hardening (review round 3)

Status: accepted · Phase 1 · supersedes parts of 0003's validator section

## Context

Mutation testing of the rev-2 validator found three ways corrupt or incomplete
data could pass:

1. **Subset pairing.** Pairing accepted a bars file whose values were a superset
   of the signals' — so a bars file carrying an extra (rogue) run passed with
   exit 0.
2. **Lenient-by-default.** Missing bars file and missing run metadata produced
   warnings and exit 0, silently skipping pairing and path-parity.
3. **Swallowed bar-write failures.** `WriteBarRow` caught and discarded write
   exceptions, so a truncated/damaged bars file could reach validation with no
   record of the failure.

## Decision

Validation is **strict by default**. `--allow-incomplete` downgrades
missing-artifact and skipped-check errors to warnings for throwaway exploration
only; the pilot and full-history runs use the default.

**Fix 1 — exact pairing.** `run_id`, `instrument`, `bar_period`, `tick_size`
must each be a single unique value within each file and equal across files.
No fall-back to an unrelated newest bars file when no exact-run match exists.

**Fix 2 — strict artifacts + timestamps.** Missing bars, missing metadata, a
skipped pairing/parity check, unparseable signal timestamps, and any label time
beyond the window are errors (non-zero exit).

**Fix 3 — bar-path integrity + clean completion.** The validator now checks:
duplicate / non-chronological / unparseable bar timestamps; invalid OHLC
(high below open/close/low, or low above open/close); negative volume, missing
values, nonpositive tick size; a missing signal bar at `signal_time`; a
`signal_reference_price` differing from the signal bar close by more than
`REF_PRICE_TOLERANCE_TICKS` (0.5); and `window_complete` rows with no covering
bars. The logger no longer swallows bar-write errors — it counts them and, on
clean termination, rewrites the run metadata with `completion_status`,
`signal_count`, `bar_count`, `signal_write_errors`, `bar_write_errors`. Strict
validation rejects any run not `completed` or with non-zero write errors.

## Metadata additions (logger + schema)

`completion_status` (`running` at open → `completed` on clean Terminate),
`signal_count`, `bar_count`, `signal_write_errors`, `bar_write_errors`.
Listed in `schema.META_REQUIRED_KEYS`; strict mode errors if absent.

## Acceptance tests

`scripts/mutation_tests.py` — clean dataset passes strict (exit 0); each damaged
dataset exits non-zero with the expected error:

| Case | Result |
|---|---|
| clean synthetic | exit 0, reports completed + 0 write errors |
| bars has extra rogue run | pairing MISMATCH, non-zero |
| bars missing (strict) | non-zero; `--allow-incomplete` passes |
| metadata missing (strict) | non-zero |
| duplicate bar timestamp | non-zero |
| invalid OHLC | non-zero |
| reference-price drift | non-zero |
| run left "running" | non-zero |
| metadata reports write errors | non-zero |
| post-window price spike | labels unchanged (boundary_demo) |

11/11 pass. Also fixed a latent bug in the synthetic generator (open = prior
close, but high/low were built around close only, so gapped bars produced
invalid OHLC) — high/low now bracket both open and close.

## Consequence

The clean, `completed` run with matched files is the only shape that passes the
default gate. This is the approval gate for the Phase 1 pilot before the full
historical collection.
