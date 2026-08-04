# Prospective AAPL/SPY SIP Smoke Runbook

This runbook is an operator checklist, not execution authority. It covers one
future AAPL/SPY smoke session only and never activates SIP, publishes a
release, registers a trial, or starts training.

## Gate sequence

1. Revalidate the repository root, committed closure, clean worktree, accepted
   identity release, accepted XNYS calendar release, and non-active SIP source
   state. Confirm the fixed session is complete and the 20-minute lag has
   elapsed.
2. After separate authorization, generate and inspect one immutable local
   request-plan package with `stock-v2-prospective-sip-smoke
   --write-plan-package`. Confirm AAPL/SPY,
   `feed=sip`, `timeframe=1Day`, `adjustment=raw`, ascending order, no `asof`,
   one page, 30-second HTTP timeout, 120-second host limit, and 1 MiB cap.
3. Obtain separate owner authorization bound to that package's exact plan ID.
   Use `--execute-network --plan-package <package> --approved-plan-id
   <exact-plan-id>` to execute one HTTPS request only; preserve raw bytes, safe
   headers, receipt time, request order, page evidence, and identity/calendar
   binding. A timeout, redirect, oversized response, or pagination spends the
   attempt and stops it.
4. Reload the landed snapshot from disk. Offline verification requires exactly
   one bar per AAPL/SPY/session, raw-SIP evidence, no page token, and the
   pinned calendar census. Emit only a candidate assessment.
5. Generate and inspect a no-write publication plan with
   `--plan-publication --plan-package <package> --snapshot-directory
   <snapshot>`. It must bind the acquisition receipt, candidate, raw hash,
   identity/calendar, source state, and clean code closure.
6. Obtain a separate publication authorization bound to that exact plan ID.
   Only then may `--execute-publication` create one non-active canonical-bars
   release and independently reload/verify it. Do not enable the active
   pipeline.
7. After bars exist, form a separate corporate-action/delisting capture plan
   covering the feature lookback and future D1--D5 outcome window. Uncovered
   intervals are abstentions, not absent events or dropped rows.

## Stop conditions

Stop without retry or cleanup on any closure, plan, source-config, timing,
credential, transport, receipt, pagination, identity, calendar, row-census,
or payload-integrity failure. Preserve any partial local evidence and seek a
new bounded authorization before another attempt.
