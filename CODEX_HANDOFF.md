# Codex Handoff

## Repository

- Root: `C:\Users\donny\Desktop\US_stocks_swing_model_v2`
- Legacy root: `C:\Users\donny\Desktop\US_stocks_swing_model` (read-only)
- Branch: `main`
- State: new independent repository; initial reviewed commit pending

## Authorized scope

Build through `REBUILD_COMPLETE` and `HISTORICAL_RESEARCH_READY`, but pause
before provider purchases, real-history hypothesis/WFA execution, candidate
sealing, destructive cutover, or external push.

## Current milestone

Status: `REAL_LEGACY_DISCOVERY_FOUNDATION_BUILT_READINESS_RECEIPT_PENDING`.

- The approved migration hash-copied 4,911 files / 345,845,816 bytes into the
  content-addressed v2 vault. It did not move, link, or modify legacy files.
- The authoritative migration plan is
  `479e3943b0eeae69d08aa078eece05ff73b20def0500b13f746646bb1534ef82`.
- The complete non-active aggregate foundation release is
  `stock_historical_foundation_set/22d6c19ce3f7be6779f4763ad99129ea571b31b021cf9347fc4e7e74eaac4c66`.
- A bounded public Nasdaq snapshot was qualified as identity evidence only.
  Alpaca remains unqualified because credentials were unavailable to this
  process; no bulk Alpaca backfill was attempted.
- No real-history hypothesis, model fit, WFA, candidate, or alpha evaluation
  ran.

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
  is not an active identity release until the asset join is implemented.

## Next gate

Run the full non-alpha suite, create the first exact local commit, and publish
the two non-authorizing mechanical milestone receipts against that clean HEAD.
`HISTORICAL_RESEARCH_READY` means only that the discovery harness is
mechanically runnable; PIT truth, exact legacy-trial census, alpha, candidates,
live use, options, and deployable shorts remain blocked.
