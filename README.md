# US Stocks Swing Model v2

This repository is the independent replacement architecture for the US
stock/ETF swing-research project. It is deliberately separated from the legacy
stock repository and every futures project.

It continues the prebuild's original research mission: test whether causally
available daily price and volume information can support a useful five-session
stock/ETF forecast whose evaluated research behavior remains useful after
explicit cost assumptions and walk-forward checks. V2 changes the architecture
and evidence controls, not that core research question.

In plain English, a forecast is made only after a completed daily session,
enters at the next regular-session open, and ends at the fifth following
session's close. The repository produces underlying stock/ETF research outputs
only. It is not investment advice, a live trading system, an options model, or
proof of profitability.

## What this repo does

- Uses daily OHLCV data: open, high, low, close, and volume.
- Keeps the active research horizon at five exchange sessions (`h5` in plain
  project language).
- Preserves source evidence in immutable, content-addressed releases.
- Keeps identity, corporate actions, availability time, and exchange sessions
  explicit so unavailable future information cannot silently enter research.
- Separates feature, outcome, evaluation, prediction, and monitoring artifacts.
- Provides mechanically tested contracts for registered nested walk-forward
  research, independent stock/ETF and long/short gates, sealed bundles,
  fit-free inference, append-only predictions, and prospective monitoring.
- Fails closed or abstains when required evidence is missing, stale,
  point-in-time unresolved, wrong-epoch, unsealed, or inconsistent.

The five-session contract is:

```text
decision_at = D0 close + provider publication latency
entry_at    = D1 regular-session open
exit_at     = D5 regular-session close
target      = (split-normalized exit price / split-normalized entry price) - 1
```

`D1` through `D5` are pinned exchange sessions, not calendar days. Dividends
are excluded from the price-return target. Missing, halted, delisted, or
action-ambiguous outcomes keep explicit statuses rather than disappearing.

This is conceptually the same five-session target used by the prebuild, but V2
expresses it through explicit sessions, timestamps, immutable releases, and
split-normalized outcomes rather than legacy stage paths or row shifts.

## Current status

Current milestone:

`HISTORICAL_RESEARCH_READY_MECHANICAL_DISCOVERY_ONLY`

The controlled rebuild hash-copied the approved legacy evidence into an
independent v2 vault and built a non-active historical foundation. The
foundation is **legacy discovery evidence only**: it does not establish
point-in-time universe truth, claim alpha, run real-history WFA, seal a
candidate, or issue trades.

The architecture and authorized rebuild boundary are complete. Historical
hypothesis evaluation, real-history WFA, candidate sealing, prospective
confirmation, provider expansion, destructive cutover, external push, live use,
and trading remain unauthorized.

`REBUILD_COMPLETE` therefore means that the controlled architecture,
deterministic rebuild, recovery behavior, and adversarial acceptance boundary
passed. It does **not** mean that alpha, a candidate model, prospective evidence,
manual decision support, options profitability, or trading readiness exists.

## Data flow

```text
as-received source release
  -> validated canonical daily bars + identity/actions
  -> causal eligible universe
  -> feature-only release -----------+
                                      +-> registered nested WFA (not run yet)
  -> outcome-only release -----------+
  -> separately authorized sealed candidate
  -> fit-free prospective inference
  -> append-only predictions
  -> separately matured outcomes
  -> fixed-end prospective confirmation
```

Every accepted data release is immutable and content-addressed. Feature,
outcome, evaluation, inference, and monitoring access are separated by schema
and API.

## Current source roles

| Source | Role |
|---|---|
| Completed migration capsule | Immutable post-migration source of truth for the approved non-active historical foundation |
| Existing HF Data Library parquet | Historical discovery input only, split into separate PiTrading and IEX epochs; non-PIT |
| Existing 780-symbol Alpaca SIP capsule and 30-symbol probe | Failed qualification evidence only; native/checkpoint/snapshot/audit evidence may migrate, while derived Parquet must be regenerated |
| Alpaca Basic | Guarded prospective candidate lane; SIP and IEX are both unqualified until a bounded accepted receipt proves one |
| Nasdaq Trader symbol directory | Preserved public snapshot evidence only; self-hashed acquisition receipts are not trust-eligible, so requalification awaits authenticated provenance |
| Alpha Vantage | Excluded |
| Options data | Excluded from model inputs, outputs, research, and validation |

The approved migration copied 4,911 files (345,845,816 bytes) without moving,
linking, or modifying legacy files. Its authoritative non-active release is
selected only by the exact reviewed migration receipt. The historical
foundation contains 11 content-addressed components and preserves the March
2022 source transition as two physical epochs.

See `config/migration_allowlist.json`, `config/migration_approval.json`, and
`config/sources.json` for the fail-closed contracts.

After migration, the authenticated immutable capsule became the source of
truth. The allowlist, approval, and mutable legacy baseline remain historical
review evidence; routine validation does not rescan or replan from the legacy
checkout.

Content-addressed, non-authorizing `REBUILD_COMPLETE` and mechanical
`HISTORICAL_RESEARCH_READY` receipts live under the ignored accepted-data
vault. A receipt is current only when its repository and release bindings
verify. Historical PIT and exact legacy-trial-census blockers remain in force.
These are deliberately open release prerequisites, not repository-only defects;
their assessment disposition is defined in
[`docs/ASSESSMENT_SCOPE_AND_BLOCKER_DISPOSITIONS.md`](docs/ASSESSMENT_SCOPE_AND_BLOCKER_DISPOSITIONS.md).

## Pipeline in simple terms

1. Qualify and validate daily OHLCV and reference sources within a bounded,
   separately authorized request.
2. Preserve the received bytes and receipts, then publish accepted evidence as
   an immutable release.
3. Canonicalize daily bars, identity, corporate actions, and exchange sessions.
4. Apply causal filters and build the eligible stock/ETF research universe.
5. Build separate feature-only data and five-session outcome-only data.
6. Register a counted hypothesis and chronological walk-forward plan before
   reading the historical results.
7. Train, score, and evaluate only after separate authorization, with all
   fitted transformations confined to the appropriate training fold.
8. Gate stock-long, stock-short, ETF-long, and ETF-short evidence separately
   and preserve failed or inconclusive results.
9. Seal a passing candidate only through its own approval.
10. Collect genuinely prospective predictions before outcomes mature.
11. Consider manual decision support only after the prospective and operational
    gates pass.

Failure and inconclusive evidence are valid results. They must remain visible;
they do not justify weakening a gate or silently changing the hypothesis.

## Important files

- [`AGENTS.md`](AGENTS.md): agent workflow, safety rules, validation, and
  approval boundaries.
- [`docs/REBUILD_CONSTITUTION.md`](docs/REBUILD_CONSTITUTION.md): binding
  scientific and design contract.
- [`docs/HISTORICAL_RESEARCH_HARNESS.md`](docs/HISTORICAL_RESEARCH_HARNESS.md):
  historical-research mechanics and claims boundary.
- [`PROJECT_OUTLINE.md`](PROJECT_OUTLINE.md): project identity, lifecycle,
  limitations, and roadmap.
- [`CODEX_HANDOFF.md`](CODEX_HANDOFF.md): short mutable continuation state, not
  proof or authorization.
- `config/`: source, migration, environment, authorization, and policy
  contracts.
- `src/`: implemented package behavior.
- `tests/`: synthetic and adversarial contract checks.

When documents appear to disagree, do not silently choose the easier rule.
Inspect the current implementation and preserve the binding scientific and
safety boundary while reporting the inconsistency.

## How to use this with Codex

For a read-only status check:

```text
Work only in C:\Users\donny\Desktop\US_stocks_swing_model_v2. Read AGENTS.md,
PROJECT_OUTLINE.md, CODEX_HANDOFF.md, and the binding documents they identify.
Reconcile the current status against Git and repository evidence. Do not change
files or run providers, data builds, WFA, training, or evaluation.
```

For a narrow documentation change:

```text
Make only the requested documentation change in the Desktop v2 repository.
Preserve the Constitution, current milestone, source roles, and authorization
boundaries. Review the diff and run git diff --check. Do not change code,
config, data, reports, tests, locks, or CODEX_HANDOFF.md.
```

Before provider, data, or historical-research work:

```text
Produce a bounded plan only. State the command family, exact scope and request
or run limit, timeout or stop condition, expected outputs and locations,
tracked/ignored disposition, required authorization, and forbidden actions.
Do not execute yet.
```

## Data and secret safety

- The legacy repository is read-only evidence and is never a runtime fallback.
- Do not edit, move, link, or discover active inputs from mutable legacy paths.
- Do not read, print, copy, move, track, or commit `api.env`, `.env` files,
  credentials, tokens, or private keys.
- Do not commit raw data or generated artifacts unless an exact task and
  authorization explicitly require it.
- Do not refresh, overwrite, delete, stage, or commit `data/**`,
  `artifacts/**`, or `reports/generated/**` as a side effect of validation.
- Provider and copy CLIs remain dry-run or plan-only by default and require
  action-specific authorization to execute.

## Local validation

The exact Python 3.11.9 runtime and numerical/data dependencies are pinned in
`requirements.lock`, the Windows CPython 3.11 wheel hashes in
`requirements.sha256.lock`, and the environment contract in
`config/environment.lock.json`. `pyproject.toml` defines the project
dependencies; there is no legacy `requirements.txt` authority. Runtime
validation compares every distribution in the complete executable project
closure, including transitive dependencies; unrelated globally installed tools
are outside that closure. See `docs/DEPENDENCY_CLOSURE_POLICY.md`.

```powershell
git diff --check
python -m pytest -q <targeted-test-path>
python -m us_stocks_swing_model_v2.cli.hash_copy --config config/migration_allowlist.json
python -m us_stocks_swing_model_v2.cli.qualify_free_sources --plan-only
python -m us_stocks_swing_model_v2.cli.qualify_free_sources --plan-only --nasdaq-only
python -m us_stocks_swing_model_v2.cli.qualify_free_sources --nasdaq-only --emit-authorization-requests C:\absolute\new\request-directory
python -m us_stocks_swing_model_v2.cli.build_historical_foundation --help
python -m us_stocks_swing_model_v2.cli.assess_mechanical_readiness --help
```

Run validation in proportion to the task. Documentation-only work needs a
reviewed diff, consistency checks, and `git diff --check`. Ask before running
the full `python -m pytest -q` suite unless the task already authorizes it.
Provider and copy CLIs are dry-run/plan-only by default. Execution requires
explicit flags, bounded scope, and the exact required authorization. Foundation
CLI operation is plan-only; its mutating mechanics are synthetic-fixture-only
and root-bound. External authorization verification is RSA-public-key-only;
shared-secret receipts are rejected, and no authority is active while the
checked registry is `NOT_CONFIGURED`. See
`docs/EXTERNAL_AUTHORIZATION.md`. Network capture and offline detached-
attestation verification are separate operations; an unattested capture is
never qualified. Free-source qualification `--plan-only` performs no filesystem
writes; authorization-request files require the explicit
`--emit-authorization-requests` mode. Provider execution also consumes a
separate externally signed request authorization before opening a connection. See
`docs/NETWORK_ACQUISITION_ATTESTATION.md`. Readiness
commands operate only on accepted local releases and make no provider or
historical-research calls.

## Completion language

- `REBUILD_COMPLETE`: architecture, deterministic rebuild, recovery, and
  adversarial acceptance tests pass; no alpha or execution claim follows.
- `HISTORICAL_RESEARCH_READY`: the registered discovery harness is mechanically
  complete for its non-authorizing scope; real-history execution still requires
  separate authorization. The readiness contract deliberately has no generic
  positive `ready` flag and cannot be read as candidate, live, or deployment
  readiness.
- `CANDIDATE_SEALED`: a separately authorized candidate is frozen.
- `PROSPECTIVE_EVIDENCE_PENDING`, `PROSPECTIVE_PASS`, `FAIL`, or
  `INCONCLUSIVE`: genuinely new evidence state.
- `MANUAL_DECISION_SUPPORT_READY`: fit-free inference and operational gates
  pass. It still does not validate an options strategy or authorize trading.

## Useful reference documents

- [`docs/REBUILD_CONSTITUTION.md`](docs/REBUILD_CONSTITUTION.md): claims,
  evidence classes, sources, causal contracts, research firewall, gates,
  inference, prospective confirmation, and completion boundaries.
- [`docs/HISTORICAL_RESEARCH_HARNESS.md`](docs/HISTORICAL_RESEARCH_HARNESS.md):
  five-session samples, purge/embargo, nested WFA, dependence-aware statistics,
  costs, controls, independent sleeves, and readiness limits.
- [`docs/AUDIT_TRACEABILITY.md`](docs/AUDIT_TRACEABILITY.md): mapping from
  release-blocking findings to code, synthetic tests, gates, and milestone
  evidence.
- [`docs/LEDGER_RECOVERY_JOURNAL_LIFECYCLE.md`](docs/LEDGER_RECOVERY_JOURNAL_LIFECYCLE.md):
  exact replay, rejected-journal quarantine, retry, and evidence-retention
  semantics for append-only ledgers.
