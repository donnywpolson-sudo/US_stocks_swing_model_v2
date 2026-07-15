# Project Outline

## Goal

Build a reproducible, bias-resistant research and decision-support system for
five-session directional and magnitude forecasts on US stocks and ETFs. The
system outputs underlying forecasts only; the owner may separately use those
forecasts when making discretionary option trades.

## Status

`HISTORICAL_RESEARCH_READY_MECHANICAL_DISCOVERY_ONLY`

Historical modeling, WFA, candidate sealing, bulk provider acquisition, and
cutover remain unauthorized. The exact approved local data copy and bounded
public Nasdaq qualification are complete.

## Milestones

1. **Core contracts**: constitution, audit traceability, source and migration
   configuration, immutable release publication, causal identity/actions,
   session-based outcomes, evaluation firewall, sealed bundles, fit-free
   inference, append-only ledger, synthetic tests.
2. **Data foundation**:
   - **2A complete**: resumable atomic-copy contract, deterministic HFDL/Alpaca
     offline canonicalizers, as-received snapshot store, bitemporal identity,
     explicit release selector, complete-snapshot absence/reuse handling,
     actual-time anchored inference, and clean-room/adversarial synthetic tests.
   - **2B complete for the authorized local foundation**: the reviewed copy,
     two physical HFDL epochs, pinned XNYS calendar, separate feature/outcome
     bridges, and aggregate non-active release were validated. Nasdaq identity
     evidence is qualified; Alpaca remains a guarded prospective lane.
3. **Historical research ready (mechanical only)**: registered nested-WFA
   executor, dependence-aware statistics, costs, benchmarks, negative controls,
   and finite gates are mechanically implemented and bound by non-authorizing
   receipts to a clean commit. This does not authorize real-history execution or
   establish trusted PIT evidence.
4. **Candidate research**: separately authorized hypothesis and counted
   historical evaluation.
5. **Prospective confirmation**: sealed prediction protocol, blinded outcome
   maturation, fixed end rule.
6. **Manual decision support**: monitored fit-free inference, abstention,
   recovery, shadow operation, and separately approved cutover.

## Non-negotiable separations

- This repository owns no futures code or data.
- The legacy stock repository is never a runtime dependency.
- Source releases are immutable; corrections create successor releases.
- Historical discovery and prospective confirmation are distinct evidence.
- Feature and outcome artifacts are separate.
- Prediction records never contain realized outcomes.
- Stock/ETF and long/short gates are individually binding.
- Monitoring may pause or abstain but may not retrain or tune.
