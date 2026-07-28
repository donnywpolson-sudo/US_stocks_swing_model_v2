# Codex Handoff

## Repository

- Exact root: `C:\Users\donny\Desktop\US_stocks_swing_model_v2`
- Merged owner-operated baseline: `e109647` (PR #6)
- Reconcile this handoff with the current Git commit, branch, and worktree before acting.

## Current Milestone

`HISTORICAL_RESEARCH_READY_MECHANICAL_DISCOVERY_ONLY`

- The reviewed migration capsule contains 4,911 files and 345,845,816 bytes.
- The project is operated locally by one owner; external signing authorities and
  public-key registries are not part of the active design.
- The bounded SIP-versus-IEX qualification passed for both exact test feeds,
  but no Alpaca feed is selected or active.
- Fresh Nasdaq snapshot A
  `b47b40cf912eccc49260d091c92d2435eaf23c630aac78d23a1ac44182df2e7b`
  is locally integrity-verified. The preserved historical receipt remains
  comparison-only and unchanged.
- The offline two-fresh-capture Nasdaq bootstrap mechanics and frozen policy
  are implemented in the working tree. They do not call the network, publish a
  receipt, activate identity data, or bypass the normal trusted-prior parser.
- No real-history WFA, alpha, candidate, provider, or live operation is authorized.
- Owner actions are bound to schema-v2 content-addressed local integrity records.
- Network execution remains plan-only unless both `--execute-network` and
  `FREE_SOURCE_QUALIFICATION_APPROVED=YES` are supplied for the exact bounded plan.
- Mechanical-readiness receipt schema v2 embeds and revalidates the exact local
  publication record; unbound legacy receipts fail closed.
- Local integrity validation binds production versus synthetic clock authority,
  and synthetic records require the exact creating clock permit.
- Network preflight attempts are intentionally spent before transport; failures
  require a new explicit invocation and session.
- Outcome maturation requires bar evidence for every expected exchange session
  from entry through exit.
- HFDL bridge source-index bounds must exactly match the verified epoch manifest.
- The immutable migrated HFDL sidecars retain their original exact `+00:00`
  creation-time encoding. The rebuild compatibility path normalizes that UTC
  instant in memory only; public inputs remain `Z`-strict and source bytes are
  never rewritten.
- The exact checked-in one-shot foundation-refresh authorization permits only
  one non-active successor build on the clean authorized descendant. It
  preserves prior releases, allows idempotent resume of that same build, and
  grants no provider, model, WFA, candidate, or production-use authority.
- The clean `4fb3048` foundation gate passed and its new commit-bound ignored
  receipts were published without activating research or providers.

## Accepted Assurance Boundaries

- Static and named runtime I/O isolation are bounded regression evidence, not an operating-system sandbox.
- Filesystem publication assumes trusted single-principal cooperative writers, not hostile same-principal mutation.
- Correctly synchronized system UTC is a prerequisite for trustworthy production
  timestamps and local integrity records; privileged clock spoofing is outside
  the supported threat model.
- Local and GitHub tests are review evidence. They are not production authorization, provider approval, or alpha evidence.

## Portable CI

- The portable lane uses Windows 2025, Python 3.11.9, and the hash-locked dependency set.
- A fresh checkout excludes only tests marked `local_evidence`, which require the ignored completed-migration capsule.
- Each run uploads commit- and run-bound test evidence with 90-day retention.
- The private repository plan does not enforce branch protection, so a green portable run is a manual merge gate.

## Next Gate

Stop before snapshot B capture. The next gate is a separate exact authorization
for one bounded Nasdaq snapshot B network capture after its embedded file time
has advanced beyond snapshot A. Offline pair assessment, receipt publication,
and activation remain separate later gates.
