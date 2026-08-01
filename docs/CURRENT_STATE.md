# Current Project State

This is a concise, commit-snapshot summary for people and Codex. It is not
execution authority and does not replace current code, configuration, accepted
releases, Git state, or an action-specific approval. Recheck those sources
before acting.

## Current milestone

`ALPACA_HISTORICAL_BACKFILL_PUBLICATION_PLANNING_IMPLEMENTED`

- The controlled rebuild architecture and its synthetic acceptance contracts
  are complete.
- HF Data Library is retired from new derivative and research work; retained
  files are historical audit and trial-census evidence only.
- Alpaca SIP is the selected, qualified bar feed. The canonical source record
  is [`config/sources.json`](../config/sources.json):
  `sources.alpaca_basic_delayed_sip.request_contract.qualified_feed` is `sip`.
- The current-identity-seeded Alpaca SIP backfill has been captured and has a
  no-network publication planner. It remains PIT-unresolved legacy-discovery
  evidence, not research or readiness evidence.
- The first bounded active-SIP canonical-bars build is a separate pending path.

## Next meaningful gate

The next selected gate is a bounded authorization to publish the captured
current-identity-seeded backfill as a caveated `legacy_discovery_only` release.
The publication implementation exists, but publication, activation, research,
and downstream use are not authorized by this page or by planning.

## What can proceed

Routine local documentation, code, and focused synthetic-test work may proceed
within a user-requested local phase. See the approval matrix in
[`AGENT_WORKFLOW.md`](AGENT_WORKFLOW.md).

Commit, provider/network activity, generated releases or receipts, historical
research, training, evaluation, prediction, activation, push, trading, and
destructive work need their applicable bounded approval. This summary does not
combine or waive those gates.

## Non-claims

The project has no historical alpha result, active candidate, prospective
confirmation, trading readiness, or authority to use options data. SIP
qualification does not establish point-in-time membership, complete corporate
actions, canonical bars, or research eligibility.

## Where to verify details

- [`AGENTS.md`](../AGENTS.md): binding repository-operation rules.
- [`config/sources.json`](../config/sources.json): current source policy.
- [`NETWORK_ACQUISITION.md`](NETWORK_ACQUISITION.md): provider and publication
  mechanics.
- [`HISTORICAL_RESEARCH_HARNESS.md`](HISTORICAL_RESEARCH_HARNESS.md): research
  mechanics and claims boundary.
