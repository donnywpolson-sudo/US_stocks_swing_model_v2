# Audit Traceability

Each release-blocking finding must terminate in code, a synthetic test, and a
gate. `Milestone 1` means the core contract exists; later data/research tests
remain fail-closed until their milestone is authorized.

Current-state entries below are reconciled as of 2026-07-26. “Mechanics
implemented” means synthetic, non-authorizing enforcement exists; it does not
claim real-history evidence, PIT completeness, candidate eligibility,
prospective confirmation, or production readiness.

| Finding | Required correction | Enforcement | Acceptance evidence | State |
|---|---|---|---|---|
| A new repo does not restore a pristine holdout | All existing history remains discovery; prospective confirmation begins only after sealing | Constitution; registered trial ledger | Registry rejects unregistered or mutated trials | Milestone 1 |
| Real-data smoke is an uncounted trial | Only synthetic smoke is free; real-data evaluation requires registration | `trials.py` | Unregistered evaluation test | Milestone 1 |
| Hashes alone do not prove causality | Event/effective, available, retrieved, source epoch and release identity are mandatory | release/action/outcome schemas | Missing-time validation tests | Milestone 1 |
| HFDL source break and hindsight limits | Isolate two HFDL epochs as legacy discovery; never pool with Alpaca | source config and migration roles | Configuration invariant tests | Milestone 1 |
| Existing Alpaca capsule failed quality | Preserve native/checkpoint/snapshot/audit evidence only; regenerate Parquet and retain failure state | source and migration configs | Scope/role/exclusion invariant tests | Milestone 1 |
| Free source can silently change entitlement/feed/adjustment/mapping | Bounded SIP-versus-IEX qualification; activate only the evidenced feed; pin 1Day/raw/ascending/minimum lag and omit `asof` unless an ISO date is deliberate | `providers/alpaca.py`; qualification CLI | Request-policy/feed/byte-bound tests | Milestone 2A |
| Alpaca alone cannot safely classify all security types | Add as-received Nasdaq identity; unknown/nonstandard types abstain | `providers/nasdaq.py`; inference | ETF/stock/unknown fixtures | Milestone 1 |
| Corporate actions are revisable | Append bitemporal revisions; separate as-received and final views | `corporate_actions.py` | Visibility/revision tests | Milestone 1 |
| Row shifts cross missing sessions and delistings | Use a pinned exchange-session calendar and explicit terminal statuses | `calendar.py`; `outcomes.py` | holiday/missing/halt/delist/action tests | Milestone 1 |
| Adjusted prices can embed hindsight | Use raw bars; compute split-normalized price outcome from versioned actions | outcome engine | split fixture and unresolved action fixture | Milestone 1 |
| Feature/label/prediction coupling leaks future truth | Separate schemas and forbidden-field checks | `schemas.py` | poison-field tests | Milestone 1 |
| Evaluator-guided repair overfits | Registration hash freezes semantics; changes create new trial | `trials.py` | mutation/re-evaluation tests | Milestone 1 |
| Aggregate results hide subgroup failure | Bind stock/ETF and long/short sleeve gates independently | `gates.py` | failed/underpowered sleeve tests | Milestone 1 |
| Fold model is not a deployable model | Seal full bundle identity and verify every artifact hash | `bundle.py` | mutation/reload-parity tests | Milestone 1 |
| Daily scoring retrains | Fit-free artifact loader and inference engine expose no training API | `inference.py` | `.fit()` spy/absence tests | Milestone 1 |
| Option proxy fields overstate the claim | Prediction schema permits underlying-only fields | `schemas.py`; inference | forbidden-option-field tests | Milestone 1 |
| Prediction rows can be backdated, rewritten, truncated, or include outcomes | Actual-time bounded inference; canonical IDs over full input/output; hash chain plus separate content-addressed head anchor; separate outcome ledger | `inference.py`; `ledger.py` | backdate/post-entry/ID/rewrite/truncation/outcome-field tests | Milestone 2A |
| Partial publish or concurrent writers corrupt releases | One-writer exclusive locks, staging verification, atomic rename, orphan quarantine | `releases.py`; `locking.py` | crash/idempotence/lock tests | Milestone 1 |
| Legacy file discovery or stale approval imports clutter | Deny-unlisted migration with approved roots; no links/overwrites; execution approval binds exact config/inventory/plan/code/count/bytes | `migration.py`; CLI | alternate-root/hardlink/approval-drift/exact-tree tests | Milestone 2A |
| Provider or copy command runs accidentally | Plan/dry-run default plus explicit flag and environment token | both CLIs | guard tests | Milestone 1 |
| Interrupted copy exposes partial evidence | Resume from verified checkpoints and atomically promote only the complete content-addressed plan | `migration.py` | synthetic crash/resume and family-receipt tests | Milestone 2A |
| Source mutates during a long copy | Recheck each source before/after copy and the full plan before promotion | `migration.py` | mutation and hash tests | Milestone 2A |
| Legacy Parquet/source transitions become active truth | Validate HFDL sidecars/bytes and tag both epochs as non-PIT legacy discovery | `canonical/hfdl.py` | feed-break and clean-room tests | Milestone 2A |
| Derived Alpaca Parquet hides raw/provenance defects | Reparse explicit raw gzip page manifests; retain failed quality/non-active state | `canonical/alpaca.py` | pagination/duplicate/schema/determinism tests | Milestone 2A |
| Identity files are parsed before evidence is preserved | Atomically land bytes, headers and receipt before strict comprehensive-file parse | `providers/snapshots.py`; `providers/nasdaq.py` | tamper/header/trailer/identity tests | Milestone 2A |
| Missing/reused symbols remain eligible by carry-forward | Complete bitemporal membership snapshots, file-time causality, explicit absence tombstones and no reuse inheritance | `identity.py`; `providers/nasdaq.py` | disappearance/reuse/change/late-revision/future-file-time tests | Milestone 2A |
| Alternate-root or pending release silently wins | Select exactly one accepted manifest path with dataset/epoch/role allowlists | `source_selection.py` | pending/wrong-role tests | Milestone 2A |
| Large correlated row count overstates evidence | Date/session effective sample and block/HAC methods | `research/hac.py`; `research/bootstrap.py`; registered synthetic executor | Synthetic dependence and effective-sample tests | Milestone 3 mechanics implemented; real-history execution blocked |
| Tuning leaks across WFA folds | Nested chronological folds, interval purge, fold-local transforms, and capability-restricted outer-label access | `research/splits.py`; `research/builder.py`; `research/executor.py` | Synthetic leakage, poison, and structural isolation tests | Milestone 3 mechanics implemented; real-history execution blocked |
| Prospective evidence is inspected or rewritten early | Separate append-only prediction/outcome truth ledgers, fixed end rule, fit-free evaluation, and local head anchors | `ledger.py`; `outcomes.py`; `research/evaluator.py` | clock, recovery, revision, census, and fit-free tests | Milestone 5 mechanics implemented; prospective execution blocked |
| Monitoring silently changes the model | Monitoring may only alert, pause, or abstain and remains bundle/policy/reference bound | `monitoring.py`; `config/prospective_monitoring_policy.json` | mutation-attempt, bounds, recovery, and bundle-binding tests | Milestone 6 mechanics implemented; production monitoring blocked |
| Manual options selection contaminates scoring | Score every eligible underlying prediction; keep discretionary journal external | Constitution/output schema | coverage reconciliation | Pending milestone 5 |

## Milestone-1 exit rule

All `Milestone 1` rows must have executable, passing synthetic evidence. Pending
rows remain explicit blockers for their later milestone and cannot be promoted
by prose or waiver.
