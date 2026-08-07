# US Stocks Swing Model v2 Master Audit

Version: `2.0.1`

Classification: `NON_AUTHORIZING_READ_ONLY_SPECIFICATION`

## Purpose

This specification governs a content-addressed, read-only assessment of the
current Alpaca-only `US_stocks_swing_model_v2` repository. It supports exactly
two cumulative target states:

1. `REBUILD_COMPLETE`
2. `HISTORICAL_RESEARCH_READY`

The second target evaluates mechanical historical-research readiness only. It
does not authorize real-history row access, registration, training, evaluation,
holdout access, candidate sealing, prediction, source activation, publication,
manual decision support, or trading.

`AGENTS.md`, `docs/REBUILD_CONSTITUTION.md`,
`docs/HISTORICAL_RESEARCH_HARNESS.md`, current code/configuration/tests, and
verified accepted-release manifests remain authoritative. This document cannot
weaken them or turn an open external prerequisite into a repository defect.

## Invocation identity

Every invocation is frozen by one canonical envelope ID. The envelope binds:

- the exact repository root, branch, clean commit and tree;
- Python 3.11.9, the selected executable, `pyproject.toml`, and both lockfiles;
- the complete ordinary tracked-file path and byte-hash census;
- the exact target-specific qualitative review corpus;
- exact config-declared accepted release directories, manifest hashes, release
  IDs, roles, quality states, payload counts, and payload byte totals;
- the ordered command contract, per-command run limit, timeout, expected exit,
  and output bound;
- fresh-reviewer independence and conversation-only output; and
- explicit all-false mutation and external authorities.

The envelope is reconstructed in memory for every command. No envelope, audit
report, receipt, release, or generated artifact is written. A dirty tree,
identity drift, corpus drift, release mismatch, command substitution, missing
footer, timeout, unexpected exit, or transport truncation stops the invocation
as `INCOMPLETE` without retry.

## Reviewer independence and transport

Each target uses a fresh reviewer with no inherited turns and no prior target
access. Before creating that reviewer, a target-free synthetic packet at least
as large as the compact dispatch must arrive unchanged and checksum-verified.
Failure keeps the target `VALIDATED_NOT_STARTED`; it does not spend or retry an
audit attempt.

The reviewer receives only the exact compact dispatch. Repository content is
read through the dispatch's envelope-bound read command. Every complete group
ends with `===== MASTER_AUDIT_GROUP_COMPLETE =====`. The reviewer must execute
each declared group exactly once and may use no undeclared repository command.

## Evidence hierarchy

Use, from strongest to weakest:

1. exact verified release payload and manifest evidence;
2. current code, configuration, schemas, and executable tests;
3. binding project contracts and source policies;
4. non-authoritative state summaries and prose.

Generated summaries, prior model output, handoffs, local hash chains standing
alone, and historical negative evidence cannot override stronger evidence.
Secrets and credential files are outside the readable corpus. A secret-like
tracked path is a preflight failure before its bytes are opened.

## Audit gates

### G1 - Repository and environment integrity

Confirm the exact repository identity, clean commit/tree, complete tracked
census, Python version, dependency locks, path containment, ordinary-file
status, and absence of tracked secret-like paths. Reject linked, hardlinked,
untracked, fallback, alternate-root, or mutable authority inputs.

### G2 - Immutable release and source authority

Verify every target-declared accepted release through `verify_accepted_release`
at `accepted_root/dataset/release_id`. Confirm source roles, explicit Alpaca SIP
request policy, non-active status where declared, preserved negative evidence,
and absence of a route that promotes legacy or qualification evidence.

### G3 - Causality, identity, calendar, and action coverage

Verify decision-after-D0, D1-open entry, D5-close exit, pinned exchange
sessions, as-received identity, security type, membership, causal availability,
raw-price handling, and split semantics. Process-date corporate-action capture
must remain raw-only and must not claim effective-event or delisting
completeness.

### G4 - Builder, outcome, evaluator, and inference isolation

Verify that feature construction cannot read outcomes, labels, evaluation
reports, or future reference state; outcomes remain separately materialized;
evaluation cannot fit; and production inference is fit-free, sealed-bundle
bound, clock-derived, abstaining, and unable to access outcomes or option-trade
fields.

### G5 - Research accounting and chronology

For `HISTORICAL_RESEARCH_READY`, verify exact trial accounting, pre-outcome
registration, immutable-anchor requirement, chronological nested WFA,
label-interval purge and exchange-session embargo, fold-local fitting,
multiplicity, costs, sleeve-specific gates, robustness, and one-time holdout
controls. Synthetic mechanics prove mechanics only.

### G6 - Prospective, monitoring, and recovery controls

Verify that pending prospective evidence, incomplete action/delisting coverage,
unconfigured external registry state, paused/invalid monitoring, and missing
recovery authorization fail closed. No local edit may cosmetically clear an
external or real-evidence prerequisite.

### G7 - False-pass resistance and tests

Run the exact full configured test command once. Treat a normal test failure as
a reportable audit result; process failure, timeout, undeclared output, or
environment drift stops the invocation. Review false-pass paths for stale
hashes, source-role drift, recursive discovery, hidden denominator loss,
post-outcome selection, retry, secret exposure, and authority conflation.
The command uses pytest's one-line traceback mode so every failing node and
error remains complete within the reviewer transport bound.

When applicable, the audit must bind the broader remediation controls to exact
adverse cases and evidence rather than treating their presence in code as a
pass:

- `audit_controls.assess_traceability_matrix` must check a frozen requirement
  and threat census, severity consistency, false passes, evidence, and open
  Critical/High findings;
- `audit_controls.ProviderLineageEvidence` must bind raw provider bytes,
  response headers, request and pagination lineage, page order, and
  raw-before-parse state;
- `audit_controls.ProspectiveControlProtocol` must enforce finite attempts,
  direct one-time holdout access, the fixed end, and the complete vintage
  census without backfill;
- `research.economics.reconstruct_five_cohort_economics` must independently
  check the four-sleeve, five-cohort accounting, actual-weight turnover, costs,
  unavailable outcomes, capacity, and short limitations; and
- `audit_controls.scan_declared_audit_surfaces` must use the exact `git`, `logs`, `reports`,
  `caches`, `artifacts`, and `admitted_evidence` censuses,
  reject an omitted surface, and redact rather than disclose secret-like
  matches.

### G8 - Decision and remediation classification

Classify each gate and finding with exact evidence. A repository-local defect
may be proposed for later `LOCAL_CORRECTABLE` remediation. Provider work,
generated evidence, release mutation, research, training, evaluation,
activation, destructive work, commit, push, and the August raw capture remain
separate gates.

## Target decisions

Allowed decisions are:

- `SUPPORTABLE`: every target gate is supported on the exact frozen baseline;
- `BLOCKED`: verified evidence demonstrates a target requirement is not met;
- `INSUFFICIENT_EVIDENCE`: the required conclusion cannot be established from
  the admitted evidence; or
- `INCOMPLETE`: the invocation did not reliably finish.

`REBUILD_COMPLETE` is limited to architecture, reproducibility, isolation,
publication mechanics, recovery mechanics, and fail-closed controls. It makes
no alpha or production-readiness claim.

`HISTORICAL_RESEARCH_READY` is cumulative on a completed rebuild audit and is
limited to mechanical readiness. Correctly enforced open prerequisites such as
the indeterminate legacy-trial census, unconfigured external immutable
registry, prospective PIT evidence, and action/delisting completeness are
limitations, not local defects, unless a bypass or false readiness claim
exists.

## Conversation-only result

The final response binds the target state, envelope ID, dispatch ID, reviewer
independence attestation, group census, command outcomes, overall decision,
gate decisions, findings with severity/evidence/remediation class, limitations,
and all-false authority flags. It is not a retained report or reusable
authorization. Remediation changes invalidate transfer of the original verdict
to the new commit; focused remediation checks do not constitute a new audit.
