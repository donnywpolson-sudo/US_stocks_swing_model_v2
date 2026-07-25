# US Stocks Swing Model v2

This is the independent replacement architecture for the US stock/ETF swing
research project. It is deliberately separated from the legacy repository and
from the futures project.

The controlled rebuild has hash-copied the approved legacy evidence into an
independent, content-addressed v2 vault and built a non-active historical
foundation. The foundation is **legacy discovery evidence only**: it does not
establish point-in-time universe truth, claim alpha, run historical WFA, seal a
candidate, or issue trades.

## Data flow

```text
as-received source release
  -> validated canonical daily bars + identity/actions
  -> causal eligible universe
  -> feature-only release -----------+
                                      +-> registered nested WFA (not run yet)
  -> outcome-only release -----------+
  -> sealed candidate
  -> fit-free prospective inference
  -> append-only predictions
  -> separately matured outcomes
```

Every accepted data release is immutable and content-addressed. Feature,
outcome, evaluation, and inference access are separated by both schema and API.

## Current source roles

| Source | Role |
|---|---|
| Existing HF Data Library parquet | Primary copied historical discovery input, split into separate PiTrading and IEX epochs; non-PIT |
| Existing 780-symbol Alpaca SIP capsule and 30-symbol probe | Failed qualification evidence only; native/checkpoint/snapshot/audit evidence may migrate, derived Parquet must be regenerated |
| Alpaca Basic | Guarded prospective candidate lane; not qualified because credentials were unavailable to this process |
| Nasdaq Trader symbol directories | Qualified public as-received snapshot evidence; not active until the Alpaca asset join and accepted identity release exist |
| Alpha Vantage | Excluded |
| Options data | Excluded |

The approved migration copied 4,911 files (345,845,816 bytes) without moving or
linking legacy files. Its authoritative non-active release is selected only by
the exact reviewed migration receipt. The historical foundation contains 11
content-addressed components and keeps the March 2022 source transition as two
physical epochs. See `config/migration_allowlist.json`,
`config/migration_approval.json`, and `config/sources.json` for the fail-closed
contracts.

After that migration completed, the authenticated immutable migration release
became the source of truth. The allowlist, approval, and legacy baseline remain
historical review evidence; routine validation does not rescan or replan from
the mutable legacy checkout.

`REBUILD_COMPLETE` and mechanical `HISTORICAL_RESEARCH_READY` are represented
by content-addressed, non-authorizing receipts under the ignored accepted-data
vault. A receipt is current only when its repository HEAD/tree and all release
bindings match the clean checkout. These milestones leave the historical PIT
and exact-census blockers in force.

## Local validation

The exact Python 3.11.9 runtime and numerical/data dependencies are pinned in
`requirements.lock`, the Windows-cp311 wheel hashes in
`requirements.sha256.lock`, and `config/environment.lock.json`.

```powershell
python -m pytest -q
python -m us_stocks_swing_model_v2.cli.hash_copy --config config/migration_allowlist.json
python -m us_stocks_swing_model_v2.cli.qualify_free_sources --plan-only
python -m us_stocks_swing_model_v2.cli.qualify_free_sources --plan-only --nasdaq-only
python -m us_stocks_swing_model_v2.cli.build_historical_foundation --help
python -m us_stocks_swing_model_v2.cli.assess_mechanical_readiness --help
```

Provider and copy CLIs are dry-run/plan-only by default. Hash-copy output is
concise unless `--detailed-plan` is requested. Execution modes require explicit
flags and environment authorization tokens. Copying additionally requires an
exact reviewed approval plus an externally signed authorization that binds
config, inventory, plan, migration-code hashes, counts, and bytes; production
UTC is mandatory. Foundation and readiness commands operate only on accepted
local releases and make no provider or research calls.

## Completion language

- `REBUILD_COMPLETE`: architecture, deterministic rebuild, recovery, and
  adversarial acceptance tests pass.
- `HISTORICAL_RESEARCH_READY`: the registered research harness is ready; this
  is not an alpha claim.
- `CANDIDATE_SEALED`: a separately authorized candidate is frozen.
- `PROSPECTIVE_EVIDENCE_PENDING`, `PROSPECTIVE_PASS`, `FAIL`, or
  `INCONCLUSIVE`: genuinely new evidence state.
- `MANUAL_DECISION_SUPPORT_READY`: fit-free inference and operational gates
  pass. It still does not validate an options strategy.
