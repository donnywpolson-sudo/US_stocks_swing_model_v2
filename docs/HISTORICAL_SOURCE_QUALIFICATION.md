# Historical Source Qualification V1

Status: `BLOCKED` by mandatory external source deficiencies. Source-independent
contract, admission, quarantine, identity-interval, raw-bar, causal-universe,
lineage, and firewall controls are implemented. No source-qualified V1 panel
has been built or relabeled from legacy data.

The detailed content-addressed report is
`config/historical_source_qualification_report_v1.json`; the machine-readable
gate is `config/historical_source_readiness_gate_v2.json`.

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
