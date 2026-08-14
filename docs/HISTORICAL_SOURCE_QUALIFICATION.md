# Historical Source Qualification V1

Status: `BLOCKED` by mandatory external source deficiencies. Source-independent
contract, admission, quarantine, identity-interval, raw-bar, causal-universe,
lineage, and firewall controls are implemented. No source-qualified V1 panel
has been built or relabeled from legacy data.

The detailed content-addressed report is
`config/historical_source_qualification_report_v1.json`; the machine-readable
gate is `config/historical_source_readiness_gate_v2.json`.

## Independent local evidence audit

The frozen one-shot audit completed with exit code 0 in 125.7 seconds. It made
zero writes and network requests and read no credential, outcome, label,
training, evaluation, or backtest path. It verified every manifest-declared
file in three selected releases: 1,720,542,877 payload bytes in total.

The historical bar release contains 13,724,185 rows for 9,321 current-seeded
asset IDs from 2016-01-04 through 2026-07-10. All 13,724,185 rows are explicitly
`LEGACY_DISCOVERY`; all carry
`CURRENT_IDENTITY_SEEDED_PIT_UNRESOLVED`; zero claim historical membership and
zero claim point-in-time safety. The scan found zero invalid OHLC rows, zero
invalid or negative-volume rows, and 436,652 zero-volume rows. Duplicate and
missing-session qualification remains blocked because there are no qualified
historical listing intervals; basic numerical integrity cannot cure the source
semantics.

The accepted identity release verifies 14,667 unique stable vendor IDs but has
only one snapshot, effective and known on 2026-07-30. The calendar verifies
9,049 unique XNYS sessions from 2000-01-03 through 2035-12-31, 79 early closes,
zero duplicate sessions, and zero invalid boundaries. The audit receipt is
`config/historical_source_qualification_audit_result_v1.json`.

## Evidence classification

| Existing evidence | Classification | Admission result |
|---|---|---|
| One accepted 2026 identity snapshot | Stable vendor IDs at one recorded snapshot only | `BLOCKED` for historical identity |
| Alpaca historical daily bars | Raw observations, but current-identity seeded and PIT unresolved | `QUARANTINED` |
| Alpha Vantage listing-status captures | 2026 ticker-keyed reconstruction with active and delisted rows | `BLOCKED` |
| Alpaca process-date corporate actions | Prospective as-received evidence with incomplete effective-event and historical-publication coverage | `BLOCKED` |
| Accepted XNYS 4.13.2 calendar | Pinned sessions and UTC regular-session boundaries | qualified component only |
| Discovery proxy features | Derived from unqualified historical bars and identity | `QUARANTINED` |

Repository evidence does not document a qualified historical license
classification for the candidate provider packages. No current snapshot,
ticker-only list, current-survivor cohort, adjusted convenience series, or
price-gap inference is substituted.

## Exact external blockers

- No stable historical security-master package proves ticker reuse, share-class
  identity, historical ticker validity, historical exchange/listing validity,
  security type, and historical revisions across the bar range.
- No raw daily OHLCV package combines documented raw/session semantics with a
  survivorship-safe active-and-delisted source universe and the required
  historical identity.
- No corporate-action package proves complete effective-event scope, historical
  publication/usable times, and revision history across the same securities and
  sessions.
- No delisting/terminal-event package proves complete stable-ID-linked inactive
  coverage and source terms without deriving an outcome.
- Provider license/storage semantics for a future historical package must be
  explicitly classified as permitting local research use.

The accepted calendar does not cure any of these security-level deficiencies.
Real-source prefix, mutation, universe, current-snapshot-poisoning, and revision
tests remain blocked because there is no admissible real-source bundle on which
to run them. Their deterministic source-independent mechanics are tested with
small fixtures; fixture success is not source qualification.

## Legacy quarantine

The existing files were not deleted, moved, overwritten, or reclassified.
`config/legacy_historical_data_quarantine_v1.json` keeps them available for
format, schema, interface, operational, and outcome-free diagnostics while
denying them to any real-research panel, label, training, validation,
evaluation, or backtest path.

## Outcome boundary

The outcome firewall remains default deny. This phase created no forward
returns, real labels, realized outcomes, performance artifacts, or holdout
path; performed no real training, evaluation, or backtest; and added no outcome
authorization switch.

## Readiness gate

| Gate group | Result | Reason |
|---|---|---|
| Foundation preservation, scheduler isolation, V1 scope, legacy quarantine | `PASS` | Exact tag, sibling worktree, content-addressed scope, and default-deny loader controls are present |
| Source contract and admission gateway | `PASS` | Deterministic zero-tolerance mechanics and reason codes pass fixture tests |
| Stable historical identity, ticker/exchange intervals, security types | `BLOCKED` | Only one accepted 2026 identity snapshot exists |
| Raw OHLCV source qualification | `BLOCKED` | Complete local history is current-identity-seeded legacy evidence |
| Active/delisted coverage and ticker reuse reconciliation | `BLOCKED` | Listing reconstruction is ticker-keyed and captured in 2026 |
| Corporate actions and terminal events | `BLOCKED` | Effective-event completeness, historical publication times, revision history, and stable-ID linkage are unproved |
| Session-date semantics | `BLOCKED` | Calendar passes, but bar-provider timestamp/session semantics are not admitted |
| Real canonical panel and real-source invariance | `BLOCKED` | No complete five-family source bundle exists |
| Outcome firewall and no-real-outcome boundary | `PASS` | Default denial is intact and no outcomes were accessed or created |
| Fundamentals, earnings, analysts, sectors, shares, market cap, index membership | `OUT_OF_SCOPE_V1` | V1 is price/volume-only |

The exact 32-gate census and evidence strings are in the machine-readable gate.

## Verification

The complete repository suite passed under the locked Python 3.11.9 and pytest
9.0.3 environment: 799 passed, 2 documented platform skips, and 0 failures in
105.62 seconds. The isolated worktree used only an ignored, hash-verified
eight-file test capsule (209,122 bytes) containing the two accepted calendar
releases and SIP qualification receipt required by existing tests. It did not
copy the historical market corpus or modify the scheduler checkout. The test
capsule is removed after the independent post-commit audit.
