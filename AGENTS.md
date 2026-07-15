# US Stocks Swing Model v2 Instructions

## Scope

- This repository is independent from every futures project and from
  `C:\Users\donny\Desktop\US_stocks_swing_model`.
- Treat the legacy repository as read-only forensic evidence. Never import it,
  discover files from it at runtime, or modify it.
- Active data must be addressed by an accepted immutable release ID. Recursive
  fallback discovery, alternate roots, hardlinks, junctions, and symlinks are
  prohibited.

## Scientific boundaries

- Existing historical data is discovery evidence, not pristine confirmation.
- Any real-history evaluation must be registered before outcomes are read.
- Synthetic mechanical tests do not establish alpha.
- Feature construction cannot read labels, outcomes, evaluation reports, or
  prospective scorecards.
- Inference is fit-free and may load only a sealed bundle and explicitly
  authorized as-of feature/identity/calendar/security-type evidence. The
  production API derives the actual observation time internally from its
  repository-issued system-UTC clock. Caller-supplied fixed time is accepted
  only through an explicit synthetic permit and is never trust eligible. Backdated, post-entry,
  historical-proxy, PIT-unresolved, stale, or unsealed inputs must fail or
  abstain. It cannot call `fit`, read outcomes, or create option-trade fields.
- Outputs concern underlying stocks/ETFs only. Manual options decisions are an
  external discretionary activity and cannot validate this model.

## Source boundaries

- Alpaca Basic is the only candidate active OHLCV source. SIP and IEX must be
  probed separately in one bounded qualification; no feed is assumed active.
  Every request pins the explicitly tested feed, `timeframe=1Day`,
  `adjustment=raw`, ascending order, and minimum lag. Active requests omit
  `asof` unless a deliberate ISO mapping date is separately reviewed.
- The comprehensive `nasdaqtraded.txt` daily as-received snapshot is the sole
  contracted Nasdaq identity input. Its raw bytes, HTTP headers, receipt time,
  and receipt must land atomically before parse. Its Eastern file-creation
  time cannot follow retrieval. Accepted complete membership snapshots emit
  absence tombstones; symbols cannot carry forward after disappearance or
  reuse. Unknown/nonstandard types abstain; narrower fallback files are
  disabled.
- HF Data Library is isolated `legacy_discovery` evidence only. The existing
  780-symbol Alpaca capsule and separate 30-symbol probe are failed source-
  qualification evidence only. Never concatenate source epochs.
- Alpha Vantage and options data are excluded.

## Change safety

- Search before editing and run `git status --short` first.
- Use exact paths. Never use `git add .` or `git add -A`.
- Do not call providers, copy data, train, evaluate real history, run WFA,
  commit, push, or cut over unless that action has its own authorization.
- Provider and copy commands must remain dry-run by default and fail closed.
- Copy execution additionally requires either an externally signed authorization
  or the exact checked-in controlled-rebuild authorization for this task. The
  authorization must bind the reviewed config, inventory, plan, migration code,
  file count, and byte count; a controlled-rebuild authorization can never be
  reused for trials, candidate sealing, production, or trading.
- A semantic change after an evaluation creates a new registered trial.

## Acceptance

- Run targeted synthetic tests only until historical research is separately
  authorized.
- A failed candidate retires the hypothesis; it does not justify weakening a
  gate or rebuilding the architecture.
