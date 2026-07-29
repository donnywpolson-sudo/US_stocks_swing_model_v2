# US Stocks and ETFs Swing Research Master Audit

Version: `1.0.0`

Classification: `NON_AUTHORIZING_EVIDENCE_SPECIFICATION`

Mode: `EVIDENCE_ONLY`

## Purpose and authority

This document specifies how to assess whether declared immutable evidence can
support one requested lifecycle state for `US_stocks_swing_model_v2`. It does
not itself perform an audit or authorize provider access, source activation,
data publication, historical research, holdout access, candidate sealing,
prediction, manual decision support, or trading.

`AGENTS.md`, `docs/REBUILD_CONSTITUTION.md`,
`docs/HISTORICAL_RESEARCH_HARNESS.md`, current code/configuration/tests, and
verified accepted manifests remain authoritative. This specification cannot
weaken them. It defines no executable auditor, CLI, machine-result schema, or
stage-matrix file. An audit run, test campaign, retained report, or publication
requires its own bounded authorization.

## Invocation contract and frozen evidence

One audit invocation requests exactly one lifecycle state and freezes:

- the exact repository root, branch, clean committed Git identity, and declared
  coordination-only worktree delta, if one is explicitly allowed;
- this specification and the authoritative project documents applicable to the
  requested state;
- the canonical source, identity, security-type, corporate-action, calendar,
  feature, outcome, trial, cost, gate, bundle, inference, and monitoring
  contracts applicable to the request;
- every admitted release manifest, receipt, plan, authorization, trial record,
  ledger head, bundle, test receipt, dependency inventory, and report by exact
  relative path, byte count, and SHA-256; and
- the auditor identity, source-reading order, runtime class, permitted paths,
  output destination, and a complete ordered command manifest. Each command
  record fixes the executable, arguments, working directory, environment
  additions, maximum invocations, timeout, expected exit behavior, and whether
  a nonzero exit is a reportable test result or an invocation-stopping process
  failure.

The invocation must bind exact accepted release IDs rather than discover data
recursively or hard-code the release that happens to be current when this
document is edited. Evidence and authority are separate hash domains. A file
cannot silently serve as both, and a producer's success statement cannot
replace independent verification. Any mutation after freezing invalidates the
affected claim.

## Auditable lifecycle states

- `REBUILD_COMPLETE`: architecture, deterministic rebuild, isolation,
  publication, recovery, and adversarial mechanics are supportable. This makes
  no alpha or execution claim.
- `HISTORICAL_RESEARCH_READY`: the registered discovery harness and
  non-alpha prerequisites are mechanically supportable. Real-history execution
  remains separately authorized.
- `CANDIDATE_SEALED`: a separately authorized candidate, its complete
  information set, thresholds, code, environment, and scoring protocol are
  immutably frozen.
- `PROSPECTIVE_PASS`: genuinely new as-received predictions and separately
  matured outcomes satisfy the predeclared fixed-end prospective protocol.
- `MANUAL_DECISION_SUPPORT_READY`: fit-free inference, operational inputs,
  monitoring, abstention, recovery, and cutover controls are supportable. This
  does not validate options or authorize trading.

An audit may report a state as supportable only for the exact frozen invocation.
Negative or incomplete prospective states remain `PROSPECTIVE_EVIDENCE_PENDING`,
`FAIL`, or `INCONCLUSIVE` as defined by the project; they cannot be relabeled as
passes.

## Independent blind-first workflow

1. Freeze the requested state, Git identity, evidence scope, safe paths,
   command budget, auditor identity, and output contract.
2. Perform repository-first threat discovery before using this document as a
   checklist. Look for false-pass routes including stale paths, duplicate
   authority, mutable data, secret leakage, source-role drift, membership
   hindsight, corporate-action revisions, outcome leakage, approval reuse, and
   incomplete recovery.
3. Register every safe-to-read evidence item with exact identity, provenance,
   limitations, and consuming claims.
4. Independently reproduce any claim that cannot be established from verified
   primary bytes and deterministic calculations. A producer receipt alone is
   not independent replication.
5. Classify every required subcheck while preserving contradictions,
   uncertainty, missing evidence, failures, and negative results.
6. Reconcile blind findings against this specification and the authoritative
   contracts. Blind findings may expand the work; this checklist never narrows
   a discovered risk.
7. Emit a concise human report and evidence index only when the invocation
   authorizes their exact output locations. The result remains evidence only.

## Safety boundary

The audit reads only frozen, declared, safe paths. Undeclared or escaping paths,
symlinks, junctions, hardlinks, stale hashes, secret-bearing files, and mutable
legacy paths are precheck failures. The audit does not discover sibling
repositories, read `api.env`, call providers, activate a source, publish a
release, run historical research, access a holdout, train, predict, or trade.

Separately authorized tests may supply evidence to an audit, but this document
does not authorize their execution. A test result proves only the behavior
actually exercised, under the exact recorded code, environment, and command.

### Command discipline

Every process started by the reviewer must appear in the frozen ordered command
manifest. Read-only intent does not authorize an unlisted diagnostic, import,
validator, test selector, fallback, or retry. An undeclared command or
unexpected nonzero exit is an invocation-level process failure: stop before any
later command or report publication and preserve the observed state.

This repository uses a `src/` package layout. Pytest receives `src` through
`pyproject.toml`; a standalone Python import does not. A direct import or
environment validator is prohibited unless its exact invocation and required
module path are separately declared in the command manifest. When the manifest
authorizes only pytest, runtime and dependency evidence must come from that
exact test population and static lockfile inspection; the reviewer must not
invent a supplementary import.

A declared test command may classify its documented nonzero exit as test
evidence rather than a process failure. That exception must be frozen before
execution, permits only report generation afterward, and never authorizes
another project command, retry, cleanup, or a readiness claim.

## Evidence and applicability

Use one canonical evidence index per invocation. Each evidence record contains
one identity, safe relative path, byte count, SHA-256, provenance, evidence
class, limitations, and all consuming claim references. Duplicate aliases,
dangling references, undeclared bytes, unsafe paths, or contradictory
identities fail precheck.

Evidence strength descends from independently reproduced primary bytes and
calculations, to verified immutable manifests and receipts, to producer
reports, and then to prose assertions. Weaker evidence cannot overrule
contradictory stronger evidence.

Evidence state and check status are distinct:

- evidence may be `VERIFIED`, `CONTRADICTED`, `UNSAFE`, `STALE`, `MISSING`, or
  `UNREVIEWED`;
- applicability must be positively established from the requested lifecycle
  state and authoritative contracts;
- absence of evidence never makes a required check not applicable; and
- reused evidence is admissible only when its exact identity, semantics,
  limitations, and owner satisfy every consuming check.

Traceability closure is exact, not keyword-based. Build a frozen requirement
and threat census, then require one row per requirement with severity and
`P0`-`P3` priority, owner, controlling Master text and gate, code/config
enforcement, executable test or explicit gap, mutation result, exact evidence
ID, residual risk, disposition, and remediation owner. The synthetic
`audit_controls.assess_traceability_matrix` checker must reject missing or
duplicate requirements, missing threats, dangling evidence, a claimed closure
without fail-closed mutation evidence, and severity/priority drift. A
demonstrated false pass is `BLOCKED`; incomplete coverage is
`INSUFFICIENT_EVIDENCE`; unresolved Critical/High or P0/P1 rows cannot support
a lifecycle claim.

## Decision semantics

Subcheck statuses are `PASS`, `FAIL`, `ERROR`, `MISSING_EVIDENCE`, `UNKNOWN`,
`NOT_RUN`, and `NOT_APPLICABLE`.

- Any required `FAIL` produces `BLOCKED`.
- Every required check being `PASS` or positively `NOT_APPLICABLE` produces
  `SUPPORTABLE`.
- Every other valid classification produces `INSUFFICIENT_EVIDENCE`.

A failed precheck is distinct from a domain failure. Interrupted, timed-out, or
partially completed work cannot be converted into a pass.

## Six cumulative stages

1. Freeze the invocation and perform blind threat discovery.
2. Evaluate G1-G3 for evidence integrity, point-in-time causality, identity,
   corporate actions, and pipeline isolation.
3. Evaluate G4-G5 for complete selection accounting and dependence-aware net
   out-of-sample evidence.
4. Evaluate G6 for holdout escrow and disclosure control.
5. Evaluate G7 for execution economics, capacity, and pathwise robustness.
6. Evaluate G8, all cumulative target requirements, recovery, reporting, and
   the final non-authorizing decision.

A later stage never erases an earlier blocker. Only checks required for the
frozen state may be marked applicable.

## Eight gates

### G1 - Evidence integrity and reproducibility

Verify exact repository identity, clean committed state, declared coordination
delta, dependency closure, canonical serialization, exact schemas and types,
deterministic timestamps and hashes, immutable release closure, one-writer
publication, crash recovery, and fail-closed applicability.

Every accepted release must verify through its exact manifest at
`accepted_root/dataset/release_id`. Reject missing or extra files, byte or hash
mismatch, wrong release ID, path escape, links, mutable fallback discovery,
partial publication, and unsupported environment drift. A green suite or
copied files alone is not readiness evidence.

### G2 - Point-in-time causality, identity, and reference data

Verify event/session, effective, availability, knowledge, retrieval, and
decision-time ordering. Identity must bind exact Alpaca asset identity and the
complete as-received Nasdaq `nasdaqtraded.txt` membership snapshot applicable
at the decision. Verify explicit `STOCK`, `ETF`, and `UNKNOWN` treatment,
absence tombstones, symbol reuse, later-known same-effective-time revisions,
and abstention for ambiguous or nonstandard instruments.

Verify bitemporal corporate actions, append-only revisions, action coverage,
split ratios effective in `(D1, D5]`, unresolved mergers/spinoffs/conversions/
delistings, and separation of as-received features from later final scoring
truth. Verify the exact pinned XNYS calendar release and this timing contract:

```text
decision_at = D0 close + provider publication latency
entry_at    = D1 regular-session open
exit_at     = D5 regular-session close
```

`D1` through `D5` are exchange sessions, not calendar days or ticker-row
shifts. Retrospective identity cannot claim point-in-time historical
membership. Missing or unresolved states remain explicit and never disappear
through filtering.

For every provider-derived input, verify an explicit lineage object binding the
raw response bytes, response headers, request contract, ordered request
lineage, ordered pagination lineage, exact page count and page hashes, and the
fact that raw landing preceded parsing. The synthetic
`audit_controls.ProviderLineageEvidence` contract must reject any hash,
ordering, page-census, or content-ID mismatch. A producer summary or parsed
table cannot substitute for this lineage.

### G3 - Feature, outcome, prediction, and evaluation isolation

Verify physical and capability separation among feature, outcome, prediction,
evaluation, and monitoring artifacts. Feature construction must not read
labels, outcomes, evaluation reports, or future-known reference state. Outcome
artifacts contain no model prediction, and prediction artifacts contain no
realized outcome.

Verify explicit entry lag and five-session horizon, label-interval purge and
embargo, chronological nesting, fold-local imputation/transformation/selection,
train-only fitting, sequential outcome maturity, and standalone reproduction
from verified V2 releases. Inference must be fit-free and unable to access
outcomes, WFA reports, training APIs, or option-trade fields.

### G4 - Trial accounting and selection control

Verify immutable preregistration before every outcome-informed attempt, the
hash-chained global trial ledger, conservative legacy trial census, candidate
genealogy, AI-assisted choices, all abandoned or failed variations,
multiplicity-family assignment, optional-stopping limits, finite stop rules,
baselines, negative controls, and exact information-set ownership.

A repository rename, new directory, changed source, altered universe, feature
change, threshold change, or unused date range does not reset prior knowledge.
Any semantic change after outcome access is a new counted trial.

Exercise the frozen `audit_controls.ProspectiveControlProtocol` together with
the existing holdout receipt state machine. Attempt-budget exhaustion,
undeclared optional stopping, an indirect final-holdout query, access before
the exact unlock, reuse after a failed or closed holdout, and repeated unlock
or close must fail at their first public boundary.

### G5 - Dependence-aware net out-of-sample evidence

Verify nested chronological WFA, date/session-level uncertainty, overlapping
five-session dependence, effective independent breadth, security and date
clustering, temporal and source-epoch stability, registered robustness policy,
multiple-testing adjustment, and portfolio-compatible net evidence.

The `stock_long`, `stock_short`, `etf_long`, and `etf_short` sleeves are
independently binding. Aggregate results cannot rescue a failed, unstable,
missing, or underpowered sleeve. Short results without verified borrow
availability and borrow cost remain explicitly non-deployable. Producer metrics
must reconcile to immutable predictions, outcomes, costs, and gate receipts.

### G6 - Holdout escrow and disclosure control

Verify immutable holdout identity, access ledger, frozen candidate and code,
one-time or explicitly budgeted access, indirect-query controls, disclosure
limits, and failure recovery. Outer WFA is not the final holdout. Unknown,
premature, repeated, or unauthorized holdout access is a failure, not an
implied pass. Holdout failure closes the trial; exposed dates cannot be
repackaged as fresh confirmation.

### G7 - Net economics, capacity, and robustness

Verify five overlapping capital cohorts, actual weight-change turnover,
next-open entry, fifth-close exit, registered cost schedules, slippage and
delay sensitivity, liquidity and participation limits, concentration,
availability-adjusted attainable returns, break-even margins, portfolio
interaction, and pathwise survival.

Verify that dividends are excluded from the price-return target and splits are
handled by the exact causal action contract. Borrow availability and borrow
cost limitations must remain visible. Gross-short or frictionless evidence
cannot support deployable net-short or manual-decision claims.

Independently reconstruct the economics through
`research.economics.reconstruct_five_cohort_economics`. The synthetic oracle
must enforce five-session cohort life, at most one new cohort per session, at
most one-fifth gross capital per cohort, directional consistency in all four
sleeves, aggregate actual-weight
turnover, monotonic 0/10/25/50 basis-point cost results with 25 basis points
binding, explicit unavailable-return propagation, and `PASS`, `FAIL`, or
`UNKNOWN` capacity. Short evidence remains
`GROSS_ONLY_BORROW_EXCLUDED`; this mechanics-only calculation cannot establish
alpha or deployment readiness.

### G8 - Readiness, inference, monitoring, and recovery

Verify cumulative lifecycle requirements, sealed-bundle byte closure,
serialization reload parity, serving parity, exact source/data/calendar/
identity/action bindings, production-clock enforcement, prediction latency,
abstention, append-only ledgers, external head anchors, change detection,
recovery, and audit trail.

For prospective and manual-decision states, also verify:

- the candidate, universe rules, scoring protocol, fixed end rule, and
  early-stop policy were sealed before prospective outcomes accrued;
- predictions were created before entry and outcome maturity, cannot be
  backdated, rewritten, truncated, or enriched with realized outcomes;
- an exact expected-versus-observed vintage census proves that missed
  as-received vintages were not backfilled and relabeled prospective;
- the fixed prospective end and aggregate-read boundary stayed blinded until
  the sealed protocol permitted the read;
- monitoring records bind the exact bundle, policy, reference, observation,
  predecessor, and local head anchor;
- stale, invalid, or paused predecessors abstain until an exact authorized
  recovery review verifies the successor; and
- monitoring cannot retrain, retune, substitute sources, auto-resume, extend
  confirmation, promote a challenger, or perform an automatic action.

`MANUAL_DECISION_SUPPORT_READY` remains underlying-stock/ETF decision support
only. Options selection is external and cannot validate this model.

## Source, universe, and release ownership

`config/sources.json` is the canonical source-policy record. An Alpaca Basic
OHLCV feed is eligible only when its exact accepted qualification evidence is
bound and the configuration names that feed. SIP and IEX are separate source
epochs and are never pooled. `adjustment=raw`, `timeframe=1Day`, ascending
ordering, the minimum end lag, and reviewed `asof` behavior remain binding.

The comprehensive Nasdaq snapshot is the sole contracted prospective Nasdaq
identity input; Alpaca asset state is supplemental. The eligible universe is a
causal result of verified releases and explicit security-type evidence, never a
retrospective symbol list. Corporate-action and XNYS calendar inputs must be
exact accepted releases appropriate to the requested state.

HFDL and preserved legacy artifacts remain `LEGACY_DISCOVERY` or historical
foundation evidence under their declared roles. No external repository,
mutable legacy path, Alpha Vantage data, options data, failed Alpaca capsule, or
unaccepted generated artifact can become active input.

## Required false-pass campaign

Every complete audit must show fail-closed evidence for at least:

- stale Git or dependency identity, stale receipts, reused approvals, and
  evidence-free pass/fail assertions;
- incomplete or mutable releases, wrong release IDs, unexpected files, links,
  duplicate keys, manifest-before-payload publication, and fallback discovery;
- wrong Alpaca feed, feed pooling, source-epoch pooling, mapping drift,
  retrospective membership, silent symbol reuse, missing tombstones, and
  ambiguous security types treated as eligible;
- future-known identity or actions, overwritten action revisions, wrong XNYS
  sessions, D0-close fills, calendar-day or row-shift horizons, and unresolved
  outcomes silently dropped;
- feature access to outcomes, outcome-bearing predictions, non-fold-local
  fitting, missing purge/embargo, unregistered trials, and trial-count reset;
- aggregate or pooled performance rescuing a failed stock/ETF or long/short
  sleeve, inadequate dependence adjustment, and absent robustness evidence;
- unknown or repeated holdout access, indirect holdout queries, and disclosed
  dates relabeled as fresh;
- incorrect five-cohort accounting, missing costs, unsupported short economics,
  liquidity/capacity omission, and unavailable returns treated as attainable;
- mutated or unsealed bundles, inference training, caller-controlled production
  time, stale inputs, backdated or rewritten predictions, and option fields;
- monitoring without an exact predecessor, unauthorized recovery or
  auto-resume, source substitution, automatic promotion, or automatic action;
- secret or credential bytes across the exact `git`, `logs`, `reports`,
  `caches`, `artifacts`, and `admitted_evidence` surface census; and
- operation that depends on a mutable legacy or sibling repository.

For each case, identify the first expected failing boundary and the exact
evidence that demonstrates it. A failure caught only by an unrelated later
control remains a traceability weakness. Missing executable coverage is
`MISSING_EVIDENCE`, never an inferred pass.

Secret testing uses `audit_controls.scan_declared_audit_surfaces` for the exact
six-surface census. Findings expose only category, surface, safe relative path,
and line number; secret bytes must never enter output. A forbidden secret
filename is reported without opening it. This synthetic campaign proves the
scanner's fail-closed mechanics only. Reading real secret-bearing files or
retaining a real-project scan report requires its own bounded authorization.

## Stop rules and reporting

Stop the entire invocation on a stale hash, schema mismatch, duplicate
authority, unsafe path, secret boundary, missing approval, contradictory
primary evidence, undeclared command or capability, unexpected process exit,
unauthorized outcome access, or exhausted command budget. Preserve partial
evidence and errors without running later commands, publishing a report,
retrying, or cleaning unless an exact recovery contract authorizes that action.
Only a predeclared reportable test-result exit follows its frozen exception.

The authorized report must contain the requested state, overall decision, gate
and subcheck statuses, exact evidence index, contradictions, limitations,
unresolved risks, remediation owners, and an explicit all-false authority
statement. Human summaries must reconcile to the evidence table and may not
state a more favorable conclusion.

A `SUPPORTABLE` result is evidence for a later separately reviewed state
transition only. It grants no provider, publication, activation, research,
holdout, candidate, prediction, manual-decision, trading, or options authority.
Final closure additionally requires the Meta Audit to find no unresolved
Critical/High or P0/P1 weakness in this specification.
