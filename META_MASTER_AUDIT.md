# Meta Audit of the US Stocks and ETFs Swing Research Master Audit

Version: `1.1.0`

Classification: `NON_AUTHORIZING_EVIDENCE_SPECIFICATION`

Mode: `MASTER_SPECIFICATION_REVIEW`

## Mission and authority

Determine whether the exact hash-bound root `MASTER_AUDIT.md` is a complete,
internally consistent, executable, and false-pass-resistant specification for a
later audit of `US_stocks_swing_model_v2`. The Meta Audit grades defects in the
Master specification. It does not grade the project's present readiness or
whether runtime evidence currently exists.

Repository authorities, code, configuration, and tests are a read-only
reference corpus used to derive requirements and challenge the Master. They are
not additional audit targets. A reference-corpus conflict is a Meta finding
only when it exposes missing, ambiguous, inconsistent, or unenforceable Master
text.

It never grants a lifecycle state, substitutes for a Master Audit result, or
authorizes provider access, source activation, publication, historical
research, holdout access, candidate sealing, prediction, manual decision
support, or trading. This document defines no executable auditor, CLI,
machine-result schema, or publication mechanism. Any review, test campaign, or
retained report requires separate bounded authorization.

## Target and reference boundary

One invocation has exactly one review target: the frozen bytes and SHA-256 of
root `MASTER_AUDIT.md`. The Meta specification, reference corpus, prior reports,
generated artifacts, and project runtime state are not co-targets and receive
no lifecycle or readiness classification.

Absent accepted releases, provider receipts, research results, trial or
holdout records, predictions, monitoring ledgers, or other runtime evidence are
not Meta findings when the Master correctly requires, identifies, and
fail-closed classifies their absence. The Meta review instead asks whether the
Master would demand the right evidence, bind it precisely, reject invalid or
missing evidence, and report the resulting project decision without ambiguity.

The specification-review verdict cannot be reused as a Master Audit verdict.
`SUPPORTABLE`, `BLOCKED`, and `INSUFFICIENT_EVIDENCE` are reserved for a later
project-targeted Master Audit and are never Meta specification-review outcomes.

## Mandatory blind-first method

1. Freeze the exact repository, Git identity, declared coordination delta,
   target Master bytes and SHA-256, reviewer identity, source-reading order,
   safe reference paths, requested review outputs, and a complete ordered
   command manifest. Each command record fixes its executable, arguments,
   working directory, environment additions, invocation limit, timeout,
   expected exit behavior, and failure disposition.
2. Before close-reading `MASTER_AUDIT.md`, build an independent coverage
   standard from `AGENTS.md`, the Constitution, Historical Research Harness,
   current public interfaces, schemas, configuration, accepted-release
   mechanics, authorization boundaries, data flow, research firewall,
   inference, monitoring, recovery, and tests.
3. Enumerate plausible false-pass paths. Assign each `Critical`, `High`,
   `Medium`, or `Low` severity and `P0` through `P3` remediation priority.
4. Only then map every independently derived requirement and threat to exact
   Master text and the read-only authority, code, configuration, and test
   references that establish why the requirement belongs in the Master.
5. Challenge the Master with specification-level adversarial cases. For each
   case, determine whether the Master mandates the earliest correct failing
   boundary, required evidence, applicability rule, status, reporting, and
   remediation ownership. Existing executable tests may be inspected as
   reference material. The Meta review does not execute Master gates or require
   current runtime evidence to exist.
6. Reconcile contradictions without averaging them away. A producer
   assertion, document statement, or passing happy-path test is not independent
   proof.
7. Emit the required traceability report and exact proposed Master amendments
   only to an explicitly authorized destination. The result remains evidence
   only and never edits the target.

The Master Audit must not be used to derive the initial coverage standard.
New blind findings expand the specification review even when the Master is
silent. Reference-corpus defects may be reported as conflicts or limitations,
but they are outside the target and cannot be disguised as Master amendments.

## Command and failure discipline

Every reviewer process must be present in the frozen ordered command manifest.
Read-only intent does not permit an undeclared diagnostic, direct import,
validator, alternate test selector, fallback, or retry. Before any process
runs, its exit behavior must be classified as either:

- `REQUIRED_SUCCESS`, where any nonzero exit stops the complete review before
  later commands or report publication; or
- `REPORTABLE_TEST_RESULT`, where a nonzero exit is preserved as test evidence,
  no later project command may run, and only the authorized specification
  report may be produced with `SPECIFICATION_REVIEW_INCOMPLETE`.

This repository uses a `src/` package layout. Pytest receives `src` from
`pyproject.toml`; standalone Python does not. A direct import or environment
validator is forbidden unless the exact command and required module path are
explicitly authorized. If the command manifest contains only pytest, the
reviewer must use its selected environment tests plus static lockfile
inspection and must not add a supplementary import.

An undeclared command, unexpected nonzero exit, timeout, or interruption is an
invocation-level failure. Preserve partial evidence, publish nothing, and stop
without retry or cleanup unless an exact recovery authorization says otherwise.

A specification-review invocation does not require the full project test suite.
Tests may be read as reference material, and separately authorized targeted
tests may validate changes to the audit documents, but their results do not
classify project readiness. The absence of current runtime test execution is
not a Meta defect when the Master itself requires the appropriate executable
evidence before a project claim can pass.

## Reference corpus and separation

Inspect the repository reference corpus before the Master Audit, including:

- accepted-release construction, verification, locking, atomic publication,
  immutable successors, and recovery;
- source qualification and selection, Nasdaq and Alpaca identity joining,
  security-type eligibility, corporate actions, and XNYS calendar handling;
- feature, outcome, prediction, evaluation, and monitoring schemas and
  capability boundaries;
- trial registration, ledger anchoring, WFA construction, robustness policy,
  sleeve gates, holdout guards, bundle sealing, and fit-free inference;
- prospective prediction and outcome ledgers, clock enforcement, monitoring,
  recovery, abstention, and change control; and
- project isolation, dependency closure, secret handling, audit-support
  controls, the independent five-cohort economics oracle, and tests.

Use these references to determine what the Master must require, not whether the
project presently satisfies those requirements. Record when the Master would
allow the same implementation to produce both a claim and its supposed proof.
A Critical or High specification claim must require independent calculation,
an alternate reader, adversarial evidence, or clean-room reproduction where
the project contract demands it. Reviewer identity, frozen hashes, reference
limitations, source order, and conflicts of interest belong in the final
report.

## Minimum independent coverage standard

The blind standard must cover:

- exact provenance, accepted immutable releases, canonical schemas and types,
  hashes, deterministic reproduction, clean committed state, dependency
  closure, and fail-closed recovery;
- Alpaca feed separation, request-policy enforcement, source epochs, raw bytes,
  qualification evidence, and configuration-controlled activation;
- complete as-received Nasdaq membership, Alpaca asset identity, symbol reuse,
  absence tombstones, explicit stock/ETF/unknown classification, and
  retrospective-membership limitations;
- bitemporal corporate actions, append-only action revisions, completeness,
  split-normalized outcomes, explicit unresolved events, and later final truth
  separated from as-received decisions;
- pinned XNYS sessions, availability and knowledge time, decision after `D0`,
  entry at `D1` open, exit at `D5` close, and no calendar-day or ticker-row
  substitution;
- feature/outcome/prediction/evaluation isolation, chronological nested WFA,
  label-interval purge and embargo, fold-local fitting, and serving parity;
- complete trial genealogy, conservative legacy-trial census, researcher
  degrees of freedom, multiplicity, optional stopping, baselines, negative
  controls, finite stop rules, and one-time holdout control;
- dependence-aware uncertainty, five overlapping cohorts, costs, liquidity,
  capacity, and separate `stock_long`, `stock_short`, `etf_long`, and
  `etf_short` gates, including borrow limitations;
- sealed bundles, fit-free inference, production time, append-only predictions,
  prospective blinding, outcome maturity, abstention, monitoring, recovery,
  and external anchors;
- underlying-only outputs with no option, strike, expiry, premium, Greek, or
  proposed-trade fields; and
- standalone operation with secrets excluded and every mutable legacy or
  sibling repository unavailable.

The requirement and threat censuses must be frozen before mapping. The
specification review must determine whether the Master correctly requires
`audit_controls.assess_traceability_matrix`,
`audit_controls.ProviderLineageEvidence`,
`audit_controls.ProspectiveControlProtocol`, the redacted six-surface secret
scanner, and `research.economics.reconstruct_five_cohort_economics` when those
controls are applicable. Their presence in code is not itself a Master pass;
the Master must require the exact adverse cases, evidence identities,
limitations, applicability, and fail-closed disposition needed to close each
project-audit row.

## Required stock/ETF false-pass campaign

At minimum, assess mutations that:

- alter one accepted file, manifest hash, release ID, dependency, receipt,
  approval, Git identity, or required recovery artifact;
- remove a release payload, add an unexpected file, publish the manifest first,
  introduce a link, reuse an approval, or substitute a producer success flag
  for independent verification;
- activate an unqualified Alpaca feed, pool SIP and IEX, pool source epochs,
  change `adjustment=raw`, violate request lag, or silently change mapping;
- reuse an old symbol's eligibility, omit an absence tombstone, accept a
  nonstandard Nasdaq instrument as stock/ETF, backdate Alpaca status, or claim
  retrospective membership as point-in-time truth;
- overwrite a corporate-action revision, omit governed coverage, expose a
  later-known action to an earlier decision, mishandle splits, or delete
  unresolved merger/delisting outcomes;
- use the `D0` close as entry, count calendar days or ticker rows as sessions,
  read future availability, omit purge/embargo, leak outcomes into features, or
  include realized outcomes in predictions;
- run an unregistered outcome-informed attempt, undercount inherited trials,
  change a multiplicity family after results, exhaust or extend an optional-stop
  budget, retry or repeatedly unlock/close a failed holdout, or query it
  indirectly;
- let aggregate results rescue a failed or underpowered sleeve, ignore
  overlapping-label dependence, omit registered robustness evidence, or claim
  deployable shorts without borrow evidence;
- misallocate the five cohorts, omit a required sleeve, calculate turnover from
  independent full-size trades, violate cost monotonicity, hide
  `PASS`/`FAIL`/`UNKNOWN` capacity, omit the short-borrow limitation, or treat
  unavailable returns as attainable;
- mutate a sealed bundle, train during inference, accept caller-controlled
  production time, backdate/rewrite/truncate predictions, or add option fields;
- change or prematurely read the fixed prospective end, backfill a missed
  prospective vintage, mature outcomes with untrusted final evidence,
  auto-resume monitoring, recover from the wrong predecessor, substitute a
  source, or promote a challenger automatically;
- omit or alter raw provider bytes, response headers, request lineage,
  pagination lineage, page census, or raw-before-parse ordering;
- expose credentials through any required `git`, `logs`, `reports`, `caches`,
  `artifacts`, or `admitted_evidence` surface, or leak discovered secret bytes
  into the report; or
- import active data, code, configuration, or authority from another
  repository.

For each mutation, identify the earliest precheck, gate, or public boundary the
Master must require, the evidence the later project audit must bind, and the
status the Master must produce. Detection only by an unrelated later control
is a Master traceability weakness. Missing current executable evidence is not a
Meta finding when the Master explicitly requires it and classifies its absence
as `MISSING_EVIDENCE`; omitting that requirement or permitting an inferred pass
is a specification finding.

## Traceability requirements

The traceability matrix has one row per independently derived requirement and
records:

- requirement and false-pass path;
- severity and remediation priority;
- authoritative project reference and owner;
- exact Master controlling text and gate, or an explicit missing-text gap;
- whether the Master requires the correct code/config enforcement, executable
  evidence, evidence identity, applicability, failure status, and reporting;
- the specification defect and concrete failure path;
- exact proposed addition, replacement, or deletion;
- residual specification risk, disposition, and remediation owner.

The matrix binds exact reference-document and target identities. It does not
require runtime artifact identities merely to judge whether the Master
specification correctly demands them. Missing rows, vague controlling text,
unresolved Critical/High claims, ambiguous applicability, non-executable
requirements, or undocumented residual risk require amendments.

The reviewer must also assess whether the Master's later project-audit
traceability contract correctly requires
`audit_controls.assess_traceability_matrix` to reject census mismatch,
duplicate or dangling rows, severity drift, unsupported closure, and
demonstrated false passes. The synthetic checker does not determine the Meta
verdict and need not be executed during specification review.

## Severity and final status

- `Critical/P0`: the Master Audit can bless unauthorized data or source use,
  holdout access, trading, secret disclosure, materially mutable evidence, or a
  false lifecycle state.
- `High/P1`: a required readiness dimension can be omitted, spoofed, or
  accepted without independent evidence.
- `Medium/P2`: ambiguity or weak traceability could conceal a meaningful
  defect, but another mandatory control is likely to detect it.
- `Low/P3`: clarity, maintainability, or defense-in-depth improvement.

The final Meta specification-review status is:

- `SPECIFICATION_SATISFACTORY` only when every independent requirement and
  threat is mapped, no unresolved Critical/High or P0/P1 finding remains, every
  Medium/Low finding has an explicit accepted disposition, and the Master is
  executable and internally consistent;
- `SPECIFICATION_AMENDMENTS_REQUIRED` when any unresolved Critical/High or
  P0/P1 defect remains, or a Medium/Low item lacks an explicit disposition; or
- `SPECIFICATION_REVIEW_INCOMPLETE` when target identity, blind coverage,
  reviewer independence, reference access, mapping, or the authorized review
  process is insufficient to reach a reliable specification verdict.

These statuses say nothing about the project's lifecycle state. A satisfactory
Master can correctly produce `BLOCKED` or `INSUFFICIENT_EVIDENCE` when it is
later executed against incomplete or failing project evidence.

## Disallowed review methods

Do not:

- derive the blind coverage standard by paraphrasing the Master Audit;
- treat line count, keyword presence, schema parsing, or a green happy-path
  suite as complete coverage;
- accept a component's own output as independent validation;
- mark a required check not applicable without positive evidence;
- downgrade a missing Master requirement to prose assurance;
- classify the project, a lifecycle state, or runtime evidence completeness;
- penalize the Master merely because separately gated runtime evidence does not
  yet exist;
- execute the Master Audit, its gates, or a project-readiness test campaign;
- conceal limitations outside the traceability matrix;
- infer project authority from `SPECIFICATION_SATISFACTORY`; or
- use another repository as active evidence or runtime dependency.

## Required report

When separately authorized, produce one hash-bound report containing the exact
target Master identity, blind coverage standard, threat catalog, complete
specification traceability matrix, unresolved findings, final specification
status, exact reference identities, reviewer independence statement, and
explicit non-authority statement.

Every finding must include severity and priority, exact Master location,
reference basis, concrete failure path, exact proposed addition, replacement,
or deletion, residual risk, disposition, and remediation owner. Proposed
amendments must be a unified diff against the frozen Master bytes or
unambiguous replacement blocks that require no editorial decisions. The review
must not apply those amendments.

When an invocation separately authorizes a retained report, its only default
destination is
`reports/generated/meta_master_spec_review/<report-sha256>.md`, where the
filename is the SHA-256 of the exact report bytes. Earlier project-evidence
Meta reports remain historical, non-authorizing records and do not establish
specification closure.

The report must state that it grants no provider, publication, activation,
research, holdout, candidate, prediction, manual-decision, trading, or options
authority. It cannot be used as a readiness transition or project-audit result.
Accepted amendments require separate authorization, and the amended Master
must undergo a new independently hash-bound specification review.
