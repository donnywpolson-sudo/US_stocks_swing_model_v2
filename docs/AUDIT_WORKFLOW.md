# Audit Workflow

This document distinguishes audit preparation, audit execution, and project
readiness. It is coordination guidance; it does not authorize any action.

## Status Vocabulary

- `NOT_PREPARED`: no current canonical envelope exists.
- `PREPARED`: a canonical envelope exists but its host and contract validation
  has not completed.
- `VALIDATED`: preparation-time metadata, host, script, identities, ordering,
  and bounds passed. No reviewer has started.
- `STARTED`: the exact authorized reviewer invocation began.
- `INCOMPLETE`: the invocation stopped without a reliable final result.
- `COMPLETE`: the authorized audit produced its final result.

Audit completion never implies project readiness. Every audit-related result
states the exact audit scope and contract, its status, the separate project
readiness state, and the next gate.

## Retired Root Audit Contracts

The former root Master and Meta audit specifications and their checked-in
execution configurations are intentionally retired. They are absent from the
current repository and are not executable governance. Historical versions
remain available through Git history and must not be treated as current
authority.

Reusable programmatic audit helpers remain available for synthetic mechanical
tests. Their availability does not restore the retired interfaces, authorize a
repository audit, or establish project readiness.

Any future repository audit requires a fresh, explicit contract bound to the
current repository state, scope, evidence census, commands, limits, output
disposition, and action-specific authorization. A future contract must not
reuse or infer authority from the retired specifications, configurations,
envelopes, dispatches, or historical results.
