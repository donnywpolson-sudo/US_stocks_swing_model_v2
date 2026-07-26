# Codex Handoff

## Repository

- Root: `C:\Users\donny\Desktop\US_stocks_swing_model_v2`
- Legacy root: `C:\Users\donny\Desktop\US_stocks_swing_model` (read-only)
- Branch: `main`
- State: independent local repository with committed tracked closure

## Authorized scope

Build through `REBUILD_COMPLETE` and `HISTORICAL_RESEARCH_READY`, but pause
before provider purchases, real-history hypothesis/WFA execution, candidate
sealing, destructive cutover, or external push.

## Current milestone

Status: `HISTORICAL_RESEARCH_READY_MECHANICAL_DISCOVERY_ONLY`.

- The approved migration hash-copied 4,911 files / 345,845,816 bytes into the
  content-addressed v2 vault. It did not move, link, or modify legacy files.
- The authoritative migration plan is
  `479e3943b0eeae69d08aa078eece05ff73b20def0500b13f746646bb1534ef82`.
- The complete non-active aggregate foundation release is
  `stock_historical_foundation_set/22d6c19ce3f7be6779f4763ad99129ea571b31b021cf9347fc4e7e74eaac4c66`.
- A bounded public Nasdaq snapshot is preserved as acquisition evidence only.
  Its self-hashed capability is not independently authenticated and is no
  longer trust-eligible. Alpaca remains unqualified because credentials were
  unavailable to this process; no bulk Alpaca backfill was attempted.
- Detached network-acquisition attestation verification is implemented.
  Network capture now emits an exact signing request but makes no qualification
  claim; only an externally signed, registry-bound receipt can be verified
  offline and promote the matching immutable snapshot.
- Provider entrypoints now require a separate, single-use externally signed
  request authorization before network access. The receipt binds the exact
  initial URL, registry, limits, pagination family, expiry, and nonce; an
  interrupted use is spent rather than replayed.
- The historical-foundation CLI is plan-only. Mutating foundation mechanics
  are synthetic-fixture-only and root-bound; there is no production
  publication authority.
- Asymmetric external-authorization verification is implemented for pinned
  RSA public JWKs. The authority registry remains `NOT_CONFIGURED`, so no
  external authority is active; shared-secret receipts grant no authority,
  production private keys remain outside the repository, and the completed
  controlled-rebuild receipt cannot be reused.
- No real-history hypothesis, model fit, WFA, candidate, or alpha evaluation
  ran.
- Non-authorizing `REBUILD_COMPLETE` and mechanical
  `HISTORICAL_RESEARCH_READY` receipts are published in the accepted-data
  vault and bind a clean local commit. They do not clear any trust blocker.
- Robustness policy and evidence hashes now bind sleeve, book, evaluation, and
  bundle artifacts. Definite failures take precedence; underpowered robustness
  evidence remains explicitly inconclusive.
- Prospective monitoring mechanics are append-only and bundle/policy/reference
  bound. Paused or invalid monitoring abstains and cannot resume without an
  exact signed review; monitoring cannot retrain, retune, substitute sources,
  or promote.
- The complete non-alpha suite passes: 181 tests. Routine v2 validation now
  authenticates the immutable completed migration capsule instead of requiring
  the independently mutable legacy checkout to remain frozen forever.

## Source truth

- HFDL: legacy-discovery only; fixed/current universe and the documented
  PiTrading-to-IEX transition make it non-PIT confirmation evidence.
- Existing Alpaca: a 780-symbol, 1,878,977-row SIP capsule plus a separate
  30-symbol probe; qualification failed (coverage, gaps, unmapped identity,
  survivorship). Only exact native/checkpoint/snapshot/audit evidence may
  migrate; Parquet is regenerated.
- Prospective Alpaca: guarded and empty pending an authenticated, bounded feed
  qualification. SIP and IEX remain candidates; neither is assumed active.
- Nasdaq: one public as-received qualification snapshot is preserved, but it
  is not trust-eligible. Requalification requires an independently
  authenticated acquisition receipt before any asset join or identity release.
- Completed migration capsule: the immutable 4,911-file release is the
  post-migration source of truth. The allowlist, approval, and legacy baseline
  remain historical review evidence and are not replanned from current legacy
  files.

## Next gate

Stop at the controlled-rebuild boundary. Real-history hypothesis/WFA execution,
candidate sealing, destructive cutover, external push, and trading require new
authorization. `HISTORICAL_RESEARCH_READY` means only that the discovery
harness is mechanically runnable; PIT truth, exact legacy-trial census, alpha,
candidates, live use, options, and deployable shorts remain blocked. The next
source/authentication work must separately review and configure an external
public authority, capture one bounded fresh Nasdaq response, obtain its detached
signature outside the repository, and verify it offline before the source can
be requalified.
