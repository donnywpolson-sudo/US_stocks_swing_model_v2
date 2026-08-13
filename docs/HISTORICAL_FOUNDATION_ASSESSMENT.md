# Historical Research Foundation and Outcome Firewall

Status: `PASS_WITH_CAVEATS` for the authorized foundation phase. Source-dependent
eligibility for any later real-outcome phase remains `BLOCKED`.

This record summarizes the evidence in the content-addressed source inventory,
protocol, and readiness gate. Those machine-readable records carry the exact
IDs and detailed source census; this document does not replace them.

## Authorization boundary

This phase did not create or access real forward returns, trade outcomes,
labels, future price paths, performance tables, or a final holdout. It did not
train or evaluate a model on real outcomes, backtest, connect to a broker, or
trade. Synthetic generated outcomes were used only in the isolated mechanics
rehearsal and cannot support an alpha or performance conclusion.

## Preserved automation baseline

- Accepted commit: `e81444af789d11d3471bdc93458ec1b03b648d28`
- Local immutable tag: `frozen-automation-acceptance-2026-08-13-e81444a`
- Acceptance: 2 of 2 sessions, state
  `TWO_SESSION_AUTOMATION_ACCEPTANCE_COMPLETE`
- August 13 capture: `COMPLETE` at `2026-08-13T12:30:54.085556Z`
- Task result: `0`
- Next scheduled run at checkpoint: August 14, 2026, 4:15 AM PDT
- Capture implementation, task definition, and local append-only evidence were
  not changed by the foundation work.

The durable baseline is `config/automation_acceptance_baseline_v1.json`.

## Repository and source findings

The bounded read-only assessment completed once under its authorized recovery
plan. It inspected 78 files and 15,957,599 content bytes, plus 84,791,496 bytes
of selected Parquet source files, within the recorded limits. It made no
network request and did not access outcomes, labels, credentials, evaluation,
or backtest data.

| Source or layer | Classification | Finding |
|---|---|---|
| Accepted identity release | Authoritative for one prospective snapshot only | 14,667 stable vendor asset IDs at one 2026 effective/known time; not historical membership |
| Accepted historical daily bars | Legacy discovery only | Raw observations are current-identity seeded, historical-membership unresolved, and explicitly not PIT safe |
| Alpha Vantage listing captures | Reconstructed candidate | 66,017 ticker-keyed rows, including 4,103 delisted rows; captured in 2026 and not proof of historical knowledge or stable identity |
| Alpaca corporate-action captures | Prospective as-observed evidence | 2,003 rows across several event types; process-date capture does not prove complete effective-event history or publication vintages |
| Accepted XNYS calendar | Qualified session reference | 9,049 sessions from 2000-01-03 through 2035-12-31 with UTC open, close, early-close, package, and version metadata |
| Accepted proxy features | Legacy derived cache | Past-only transformations inherit the historical bars' identity, universe, and PIT blockers |
| Fundamentals, earnings, analyst vintages, PIT sector/industry, PIT shares/market cap | Absent | These inputs remain unavailable and cannot be fabricated or backfilled from current values |

The complete metadata inventory is
`config/historical_source_inventory_v1.json`. No bulk dataset was duplicated.

## Historical-foundation findings

### Stable security identity

The existing bitemporal identity ledger uses stable `asset_id` values and
time-varying symbols. Complete snapshots can emit absence tombstones, and tests
show that later ticker changes or ticker reuse cannot silently rewrite earlier
views. The canonical panel joins by stable ID, not ticker.

Historical readiness is nevertheless `BLOCKED`: only one accepted 2026
identity snapshot was found. The available ticker-keyed listing reconstruction
cannot prove historical ticker reuse, exchange/share-class lineage, mergers,
spin-offs, or historical knowledge time.

### Point-in-time universe and survivorship

The eligibility interface uses stable identity, an explicit T-minus-1
information cutoff, prior observations, evidence hashes, and reason codes. It
rejects current-list substitution and later mutations in synthetic tests.

Historical universe readiness is `BLOCKED`: qualified point-in-time membership
does not exist. The 4,103 reconstructed delisted listing rows characterize some
inactive coverage but do not establish complete survivorship-safe membership,
terminal prices, or delisting returns. Missing terminal evidence remains a
limitation; it is not approximated.

### Corporate actions and adjustment semantics

The foundation now separates raw observed bars, event revisions, exact
effective-event coverage receipts, causally adjusted bars, and lineage. Raw
bars cannot claim adjustments. Adjusted bars must bind their raw source and the
exact visible actions. Future revisions and events do not change prior views.

Historical reconciliation is `BLOCKED`: current captures do not prove complete
effective-event coverage or historical announcement/publication vintages.
Fully adjusted convenience history is therefore not treated as causally safe,
and no global adjustment factor is inferred.

### Sessions, bars, and availability

The canonical session contract is regular trading in `America/New_York`, with
pinned UTC open/close times, early-close validation, holidays, and daylight-
saving behavior. Daily-bar checks cover missing/unexpected sessions,
duplicates, OHLC integrity, zero volume, stale prices, suspected halts, and
action-associated or unexplained extreme gaps.

Every input carries semantics equivalent to `effective_time`,
`published_time`, `received_time`, `usable_time`, `source_revision`, and
`source_identifier`. Consumers enforce `usable_time <= signal_cutoff`.
Completed D0 close information may support a post-close signal; the earliest
default opportunity is the next eligible session. Same-close execution is
rejected.

Session mechanics pass with caveats because historical security-specific halts
and all vendor timestamp meanings are not source-qualified.

## Canonical panel and feature registry

The canonical stock-date interface binds stable identity, date-valid ticker,
exchange/security type, session boundaries, raw and causal-adjusted bar
references, eligibility and reason codes, corporate-action coverage, source
identifiers, usable time, execution floor, blockers, and content-addressed
lineage. A row can be causal-ready only when all required evidence is present.

`config/feature_registry_v1.json` registers the three existing past-only price
features for infrastructure validation. It records their hypotheses, fields,
lookbacks, minimum history, timing and lag rules, missing/outlier policies,
universe and action dependencies, update cadence, leakage risks,
implementation, and version. Outcome access and performance-based feature
ranking are disabled.

## Outcome firewall

The firewall is default-deny at several layers:

- exact configuration flags keep real outcomes, labels, holdout, real
  training/evaluation, backtesting, broker connectivity, and trading disabled;
- only outcome-free foundation namespaces are resolvable;
- known outcome/evaluation path tokens and datasets are denied;
- recursive payload guards reject label/outcome fields;
- exploratory import checks reject outcome, builder, evaluator, executor, and
  trial APIs;
- real-outcome operations have no authorization mechanism in this phase;
- synthetic outcome fixtures require an exact scoped permit and separate
  namespace;
- every gateway decision produces a content-addressed audit event.

The exact policy is `config/outcome_firewall_v1.json`.

## Research protocol and synthetic rehearsal

`config/historical_research_protocol_v1.json` freezes the established D0
post-close, D1-open, D5-close timing, five-session overlap, nested chronological
WFA, half-open purge, five-session embargo, cost schedule, metric hierarchy,
and one-time holdout controls. Material choices not established by repository
evidence remain explicit unresolved decisions and block later registration.
The final holdout is unpopulated and inaccessible to ordinary development.

The deterministic synthetic rehearsal exercises generated dataset
construction, stable identity, dated membership, universe selection, canonical
panel construction, registered features, temporal splits, purge, embargo,
synthetic-only model API plumbing, portfolio/cohort and cost interfaces,
content-addressed artifacts, manifests, and seed control. Rehearsal ID:
`d1a52d45b59fa682071eb1317fdf0ac82fbe49a049a893d9d526a5c37739c149`.
It performs no file write and reads no real market or outcome data.

## Readiness gate

| Gate | Result |
|---|---|
| Capture baseline preserved | PASS |
| Historical source inventory complete | PASS |
| Point-in-time security identity | BLOCKED |
| Survivorship and delisting treatment | BLOCKED |
| Corporate-action reconciliation | BLOCKED |
| Session and timestamp integrity | PASS_WITH_CAVEATS |
| Causal universe construction | BLOCKED |
| Feature prefix invariance | PASS |
| Future-mutation invariance | PASS |
| Availability enforcement | PASS |
| Outcome firewall | PASS |
| Research protocol frozen | PASS_WITH_CAVEATS |
| Synthetic end-to-end rehearsal | PASS |
| Clean worktree and reproducible tests | PASS |

The machine-readable gate is
`config/historical_foundation_readiness_gate_v1.json`. Passing infrastructure
gates never unlocks outcomes. Point-in-time identity, universe, survivorship,
and corporate-action source blockers must be resolved, the open protocol
decisions must be frozen, and a separate explicit authorization is still
required before any real-outcome phase.
