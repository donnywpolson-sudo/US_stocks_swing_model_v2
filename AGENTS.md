# US Stocks Swing Model v2 Instructions

## Scope

- This repository is independent from every futures project and from
  `C:\Users\donny\Desktop\US_stocks_swing_model`.
- Before any edit or execution, confirm `git rev-parse --show-toplevel`
  resolves exactly to `C:\Users\donny\Desktop\US_stocks_swing_model_v2`.
  Stop if it does not; never rely on a similarly named repository or a stale
  working directory.
- Treat the legacy repository as read-only forensic evidence. Never import it,
  discover files from it at runtime, or modify it.
- Active data must be addressed by an accepted immutable release ID. Recursive
  fallback discovery, alternate roots, hardlinks, junctions, and symlinks are
  prohibited.
- An accepted release is an exact `release_manifest.json` validated by
  `verify_accepted_release` in
  `src/us_stocks_swing_model_v2/releases.py`, published only at
  `accepted_root/dataset/release_id`. Its release ID must equal the SHA-256 of
  its canonical manifest, including the declared file hashes, code, config,
  and environment bindings. Missing, mismatched, or unverified manifests are
  not accepted evidence.

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
- Prospective monitoring records are append-only and bind the exact bundle,
  policy, reference, observation, and predecessor. Pending, paused, or invalid
  monitoring cannot support manual decisions. A paused or invalid predecessor
  requires an exact signed recovery review; monitoring cannot retrain, retune,
  substitute sources, auto-resume, or promote.

## Source boundaries

- Alpaca Basic is the only candidate active OHLCV source. SIP and IEX must be
  probed separately in one bounded qualification; no feed is assumed active.
  Every request pins the explicitly tested feed, `timeframe=1Day`,
  `adjustment=raw`, ascending order, and minimum lag. Active requests omit
  `asof` unless a deliberate ISO mapping date is separately reviewed.
- `config/sources.json` is the canonical request-policy record: its
  `alpaca_basic_delayed_sip.request_contract` fixes the 20-minute minimum end
  lag and `asof: null`. A feed becomes eligible only when that record names it
  and an accepted qualification receipt binds the feed and request evidence.
  An ISO mapping date must be valid, explicit, and recorded in that reviewed
  qualification evidence; otherwise omit `asof`. While `qualified_feed` is
  null, no Alpaca feed is active or authorized.
- The comprehensive `nasdaqtraded.txt` daily as-received snapshot is the sole
  contracted Nasdaq identity input. Its raw bytes, HTTP headers, receipt time,
  and receipt must land atomically before parse. Its Eastern file-creation
  time cannot follow retrieval. Accepted complete membership snapshots emit
  absence tombstones; symbols cannot carry forward after disappearance or
  reuse. Unknown/nonstandard types abstain; narrower fallback files are
  disabled.
- The canonical Nasdaq receipt is the path named by
  `config/sources.json` at `sources.nasdaq_symbol_directory.qualification_receipt`
  (currently `config/nasdaq_qualification_receipt.json`), and its snapshot ID
  must resolve under the configured immutable snapshot store. A missing,
  malformed, or non-matching receipt/snapshot is not accepted identity
  evidence.
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
- External authorization is valid only as a `SignedAuthorizationReceipt`
  validated by `src/us_stocks_swing_model_v2/governance.py` against an ACTIVE
  authority in `config/authorization_authorities.json`. The receipt must bind
  the authorized action, subject/project, target, reviewed artifact hashes,
  issue/expiry times, and signer/key. The controlled-rebuild receipt is
  `config/controlled_rebuild_authorization.json`; its canonical JSON SHA-256
  must equal `authorization_id` and it must contain the exact scope and
  immutable bindings above. Missing, expired, unsigned, inactive, altered, or
  incompletely bound authorization is invalid and grants no permission.
- A semantic change after an evaluation creates a new registered trial.

## Acceptance

- Run targeted synthetic tests only until historical research is separately
  authorized.
- A failed candidate retires the hypothesis; it does not justify weakening a
  gate or rebuilding the architecture.
- Completion for any change requires the narrowest relevant synthetic tests
  for the affected contract, or a clear statement that no such test exists,
  plus `git diff --check`. Report any failed or skipped check; do not replace
  those checks with historical research or runtime provider activity.

## Plain-English User-Facing Output

- Write every user-facing progress update, explanation, audit summary, and final response concisely and in plain English by default. The user should not need to ask, "Tell me this entire output concisely and in plain English."
- Lead with what the result means for the user. Translate technical findings and tool output into ordinary language instead of repeating raw logs or jargon.
- Include only the technical details, file paths, numbers, warnings, and evidence needed to understand the result or make the next decision.
- Do not remove important uncertainty, safety warnings, failed checks, limitations, or blockers for the sake of brevity. State them briefly and clearly.
- If a technical term is necessary, explain it in a short plain-English phrase the first time it appears.
