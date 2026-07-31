# Project Outline

## Document authority

This repository separates durable rules, scientific design, implementation,
continuation state, planning, and user orientation:

- `AGENTS.md` governs agent workflow, repository safety, validation, and
  approval boundaries.
- `docs/REBUILD_CONSTITUTION.md` is the binding scientific and design contract.
- `docs/HISTORICAL_RESEARCH_HARNESS.md` governs historical-research mechanics.
- Current code, configuration, and tests define implemented behavior.
- `CODEX_HANDOFF.md` is mutable continuation state, not proof. Verify its claims
  against current files, command output, accepted releases, and Git status.
- `PROJECT_OUTLINE.md` preserves project identity, lifecycle, and roadmap.
- `README.md` is the user-facing orientation.

If these sources appear to conflict, do not silently choose the less restrictive
interpretation. Inspect the implemented contract, preserve the scientific and
safety boundaries, and report the inconsistency before changing behavior.

## Project objective

Build a reproducible, bias-resistant daily OHLCV research pipeline for
five-session directional and magnitude forecasts on US stocks and ETFs.

The core research question is continuous with the prebuild: can price, volume,
identity, corporate-action, and exchange-session information that was
causally available after a completed daily session support useful
next-open-to-fifth-close forecasts whose evaluated research behavior remains
useful after explicit cost assumptions and walk-forward checks? V2 changes the
data and evidence architecture used to answer that question; it does not
replace the question with a different project.

The model may eventually emit an underlying asset's expected five-session price
return, up/down/neutral probabilities, uncertainty, rank, and an abstention
reason. It does not select option contracts or claim option profitability. The
owner may separately use an underlying forecast when making a discretionary
option decision, but that external choice cannot validate this model.

The project is research-only until every later gate is separately satisfied and
authorized. It is not investment advice, proof of alpha or profitability, an
options strategy, a deployable short strategy, or a live trading system.

## Continuity with the prebuild

V2 preserves these concepts from the prebuild:

- daily stock/ETF OHLCV research;
- a five-session (`h5`) horizon;
- a decision after the completed daily bar, entry at the next session's open,
  and exit at the fifth session's close;
- causal eligibility and feature construction;
- chronological walk-forward evaluation with train-only fitting;
- explicit costs, validation gates, limitations, and negative results;
- research-only claims unless later evidence and approvals justify more.

V2 deliberately replaces these implementation details:

- mutable or searched data roots with accepted immutable releases;
- legacy stage paths with explicit source, canonical, feature, outcome,
  evaluation, prediction, and monitoring artifacts;
- informal experiment continuation with preregistered counted trials;
- mixed research access with builder/evaluator/inference isolation;
- one aggregate result with separate stock/ETF and long/short gates;
- historical-only confidence with sealed prospective confirmation.

Continuity is conceptual, not operational. No legacy source, generated file,
model, candidate, trial result, approval, or path becomes active in V2 merely
because it served the same project mission.

## Current status

`HISTORICAL_RESEARCH_READY_MECHANICAL_DISCOVERY_ONLY`

The controlled architecture, approved local migration, non-active historical
foundation, accepted current identity release, active qualified Alpaca SIP
source, first bounded active-SIP canonical-bars release, and deterministic
historical research mechanics are complete. Canonical-bar history must grow
through immutable, predecessor-bound successor releases before eligible-
universe construction. The completed foundation remains legacy discovery
evidence with unresolved point-in-time limitations.

Historical hypothesis evaluation, real-history WFA, candidate sealing, bulk
provider acquisition, prospective confirmation, destructive cutover, external
push, trading, and live use remain unauthorized. No historical alpha evaluation
or active model candidate exists in v2.

`REBUILD_COMPLETE` means the controlled rebuild and architecture passed their
acceptance boundary. `HISTORICAL_RESEARCH_READY` means the registered discovery
harness is mechanically runnable. Neither status establishes alpha, trusted
point-in-time evidence, candidate readiness, or permission to execute the next
stage.

## Five-session research contract

For a decision after the regular-session close on pinned exchange session `D0`:

```text
decision_at = D0 close + provider publication latency
entry_at    = D1 regular-session open
exit_at     = D5 regular-session close
target      = (split-normalized exit price / split-normalized entry price) - 1
```

`D1` through `D5` are exactly five pinned exchange sessions, not calendar days
or ticker-row shifts. The position cannot use the `D0` close as its entry.
Dividends are excluded from the price-return target. Only split ratios effective
in `(D1, D5]` are applied.

Missing entries or exits, halts, delistings, mergers, spinoffs, conversions, and
action-ambiguous paths retain an explicit unresolved status and remain in
coverage accounting. They are not deleted because their result is inconvenient.

Features and eligibility may use only evidence available by `decision_at`.
Feature releases contain no labels, outcomes, future fields, or evaluation
results. Outcome releases contain no model prediction, and prediction releases
contain no realized outcome.

## Research workflow

The high-level workflow remains recognizable from the prebuild, but every V2
phase uses the new release and evidence contracts:

1. Qualify and validate daily OHLCV, identity, action, and calendar sources.
2. Preserve as-received evidence and publish accepted immutable releases.
3. Canonicalize daily bars and causal reference data without crossing source
   epochs.
4. Apply as-of eligibility rules and build the stock/ETF research universe.
5. Build separate feature-only and five-session outcome-only releases.
6. Register the hypothesis, trial count, data identities, features, costs,
   metrics, gates, and nested chronological WFA plan before evaluation.
7. Fit transformations and models only on the permitted training IDs inside
   each fold, then score and evaluate through the isolated evaluator.
8. Report stock-long, stock-short, ETF-long, and ETF-short results separately,
   including failed and inconclusive outcomes.
9. If every binding gate passes, seek separate authorization to seal a
   candidate; a historical pass alone does not authorize sealing.
10. Collect append-only, genuinely prospective predictions before outcomes
    mature, then evaluate at the fixed end rule.
11. Consider monitored manual decision support only after prospective,
    operational, and cutover gates pass.

Documentation of this workflow is not authorization to execute any phase.

## Claims, failure, and negative evidence

Failure is an acceptable and necessary research result. Failed and inconclusive
outcomes must remain visible, with their exact evidence and limitations.
Secondary metrics, aggregate performance, or a favorable subgroup cannot rescue
a failed binding gate.

A failed candidate retires that hypothesis. Changing its features, thresholds,
neutral band, universe, target, costs, selection metric, or evaluation semantics
after outcomes are known creates a separately registered and counted trial.

Do not describe synthetic tests, source checks, a smoke test, one historical WFA,
or a favorable chart as alpha. Do not claim point-in-time universe truth,
survivorship safety, corporate-action completeness, deployable short economics,
option profitability, prospective confirmation, or trading readiness without
the exact evidence required for that claim.

## Evidence model

The project keeps different evidence classes separate:

1. `SYNTHETIC_MECHANICAL` proves code behavior, isolation, accounting,
   recovery, and abstention. It is not an alpha trial.
2. `LEGACY_DISCOVERY` includes HFDL and all previously inspected historical
   evidence. It may develop or falsify a hypothesis but cannot confirm one.
3. `REGISTERED_HISTORICAL_DISCOVERY` is a predeclared, counted real-history
   evaluation performed only after separate authorization.
4. `PROSPECTIVE_AS_RECEIVED` is immutable source evidence collected only after
   a candidate and scoring protocol are sealed.
5. `PROSPECTIVE_FINAL` is later corrected truth used to mature outcomes without
   rewriting the as-received state that produced a prediction.

A repository rename, source substitution, unused date range, or new artifact
does not erase prior outcome exposure or reset the trial count.

The prebuild's inspected and failed hypotheses therefore remain part of the
legacy discovery record and conservative trial census. They are useful negative
evidence, but they are not active V2 models, V2 historical evaluations, or
permission to rerun or rescue a retired hypothesis.

## Source roles and limitations

| Source | Current role |
|---|---|
| Completed migration capsule | Immutable post-migration source of truth for the approved non-active historical foundation |
| HF Data Library | `legacy_discovery` only, physically separated into pre-2022-03-04 PiTrading-consolidated and later IEX-only epochs |
| Existing Alpaca SIP capsule and probe | Failed source-qualification evidence only; never an active feed |
| Alpaca Basic bars | SIP and IEX passed the bounded comparison; SIP is selected and active through its accepted qualification receipt, while canonical bars remain separately gated |
| Alpaca assets | The current accepted identity release binds the immutable asset snapshot and frozen projection; it supplies asset identity but does not authorize bars or research |
| Nasdaq Trader | The current accepted identity release binds the strictly newer comprehensive capture and Alpaca identity projection; retrospective membership remains prohibited |
| Alpha Vantage | Excluded |
| Options data | Excluded from model inputs, outputs, training, evaluation, and validation |

Current historical evidence does not prove a point-in-time security master,
historical stock/ETF membership, survivorship-clean coverage, complete delisting
outcomes, or authoritative corporate-action truth. The HFDL feed transition is
a binding source-epoch break and cannot be silently pooled.

Short diagnostics exclude borrow availability and borrow cost, so they cannot
establish deployable net-short economics. Current optionability or discretionary
option selection cannot enter historical samples, features, labels, folds, or
model evaluation.

Unknown, nonstandard, stale, PIT-unresolved, wrong-epoch, unsealed, or
schema-mismatched inputs must fail or abstain rather than inherit eligibility.

## Lifecycle and milestones

The intended lifecycle is:

```text
bounded source qualification
  -> immutable as-received releases
  -> causal identity, actions, calendar, and eligible universe
  -> separate feature-only and outcome-only releases
  -> preregistered nested chronological WFA
  -> independent stock/ETF and long/short gates
  -> separately authorized sealed candidate
  -> fit-free prospective inference
  -> append-only predictions and separately matured outcomes
  -> fixed-end prospective confirmation
  -> monitored manual decision support after separate approval
```

Milestone state:

1. **Core contracts complete**: Constitution, audit traceability, source and
   migration configuration, immutable publication, causal identity/actions,
   session outcomes, research firewall, sealed bundles, fit-free inference,
   append-only ledgers, and synthetic tests.
2. **Authorized data foundation complete**: resumable approved copy, two
   physical HFDL epochs, deterministic offline canonicalization, pinned XNYS
   calendar, separate feature/outcome bridges, aggregate non-active release,
   and bitemporal identity mechanics. The preserved Nasdaq snapshot is not
   qualified identity evidence. Two fresh captures passed the bootstrap without
   a normal-parser bypass, and their non-active continuity-baseline receipt was
   published without relabeling the preserved historical receipt. Guarded
   plan-only tooling now uses the separately captured Alpaca asset snapshot only
   through a hash-bound active-US-equity projection, and still requires a newer
   Nasdaq snapshot plus an offline merged-identity pass before it can emit a
   publication plan. Identity release publication and activation remain later
   independent gates. Further foundation publication
   is plan-only by default. The exact checked-in one-shot successor-refresh
   authorization is the only non-synthetic exception; it cannot create more
   than one distinct non-active build or grant research or production use.
3. **Historical research ready mechanically**: registered nested-WFA executor,
   dependence-aware statistics, costs, benchmarks, controls, finite gates, and
   sleeve/book robustness propagation are implemented and bound to
   non-authorizing receipts. Real-history execution remains paused.
4. **Candidate research pending**: requires a separately authorized,
   predeclared, counted hypothesis and historical evaluation.
5. **Prospective confirmation pending**: requires a sealed candidate, scoring
   protocol, immutable as-received collection, blinded maturation, and fixed
   end rule.
6. **Manual decision support pending**: requires monitored fit-free inference,
   append-only bundle/policy/reference-bound records, mandatory abstention, and
   separately approved cutover. Monitoring cannot retrain, retune, substitute
   sources, auto-resume, or promote.

## Causal, leakage, and selection rules

No future or outcome-informed information may enter eligibility, features,
labels, imputation, scaling, selection, calibration, thresholds, WFA, costs,
gates, or reporting.

Historical research must use nested chronological walk-forward analysis.
Training samples whose label information intervals overlap validation or test
intervals are purged. Any required embargo is expressed in pinned exchange
sessions. Imputation, scaling, winsorization, feature selection, calibration,
thresholds, and other fitted transformations use training IDs only within each
fold.

Outer dates arrive sequentially. Earlier outer outcomes may be used only after
they mature and only where the preregistered refit schedule permits. A sealed
holdout is accessed at most once and cannot be reused after failure.

Stock-long, stock-short, ETF-long, and ETF-short evidence is gated separately.
An absent or underpowered sleeve is inconclusive; aggregate results cannot hide
it. Predictions and prospective monitoring remain fit-free.

## Validation and reporting

Validation must be proportional to the change. Documentation-only work requires
a reviewed diff, consistency checks, and `git diff --check`. Code, config,
schema, and contract changes require the narrowest relevant synthetic tests.
Broad or expensive suites require explicit approval unless already authorized.

Research reports should identify the objective, immutable inputs and lineage,
method, registered trial, validation, results, limitations, blockers, and next
authorized gate. Report verified facts separately from assumptions and
inferences. Gross-only or cost-sensitivity results must not be presented as
tradable evidence.

A failed validation stops the task. Do not replace a failed or skipped check
with prose, model output, memory, a provider call, or a broader unauthorized
run.

## Handoffs, stop conditions, and approvals

Use `CODEX_HANDOFF.md` only when meaningful work must continue across prompts or
a fresh thread. Keep it short and current. It records continuation state but
does not authorize execution or override current evidence.

Stop and report before proceeding when:

- the repository root is not the exact v2 root;
- validation or an immutable contract check fails;
- accepted lineage, authorization, or required evidence is missing;
- a command would call a provider, mutate generated artifacts, or broaden scope
  without a complete bounded-execution contract;
- a conclusion would require PIT, survivorship, corporate-action, short-borrow,
  options, prospective, or trading evidence that is absent;
- the next step would train, evaluate real history, run WFA, seal a candidate,
  cut over, push externally, or trade without its own authorization.

Every provider, copy, historical-research, training, evaluation, prediction,
report-generation, prospective, cutover, and trading action requires its exact
scope, limits, outputs, stop conditions, and action-specific approval. Planning
or documentation never supplies that authorization.

## Non-negotiable separations

- This repository owns no futures code or data.
- The legacy stock repository is read-only evidence and never a runtime
  dependency.
- Accepted releases are immutable; corrections create successor releases.
- Historical discovery and prospective confirmation are distinct evidence.
- Feature, outcome, prediction, evaluation, and monitoring artifacts remain
  separated.
- Prediction records never contain realized outcomes or option-trade fields.
- Stock/ETF and long/short gates are individually binding.
- Monitoring may alert, pause, or abstain but may not retrain, retune,
  substitute sources, auto-resume, or promote.
