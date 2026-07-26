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

## Project identity and workflow continuity

- This is a daily US stock/ETF OHLCV research pipeline with an active
  five-session (`h5`) horizon. Its core question is whether information
  causally available after a completed daily session can support useful
  next-open-to-fifth-close forecasts whose evaluated research behavior remains
  useful after explicit cost assumptions and binding gates.
- V2 continues the legacy project's research mission, target horizon, causal
  discipline, failure transparency, and walk-forward workflow. It replaces the
  legacy implementation, data authority, and evidence architecture; it does
  not inherit an active legacy dataset, model, candidate, approval, or runtime
  dependency.
- In user-facing explanations, `h5` and "five-session" are acceptable plain
  language. Code, schemas, and contracts must use the implemented V2
  session/timestamp fields and the decision-after-`D0`, entry-at-`D1`-open,
  exit-at-`D5`-close contract.
- Preserve this conceptual workflow: qualify and validate daily source
  evidence; canonicalize bars and causal reference data; build the eligible
  research universe; create separate feature and five-session outcome
  releases; preregister chronological WFA; train, score, and evaluate only when
  separately authorized; and preserve failures as evidence.
- Listing a phase in documentation never authorizes its execution. V2's
  immutable releases, research firewall, counted trials, independent sleeve
  gates, sealed bundles, and prospective monitoring remain binding throughout.
- Legacy failed or inspected experiments remain negative discovery evidence
  and trial-census input. They are not active V2 candidates and must not be
  described as fresh, pristine, or rerunnable V2 evidence.

## Document authority and required reading

- `AGENTS.md` governs agent workflow, repository safety, validation, and
  approval boundaries.
- `docs/REBUILD_CONSTITUTION.md` is the binding scientific and design contract.
- `docs/HISTORICAL_RESEARCH_HARNESS.md` governs historical-research mechanics.
- Current code, configuration, and tests define implemented behavior.
- `CODEX_HANDOFF.md` is mutable continuation state, not proof. Reconcile it
  against current files, command output, and Git status before acting.
- `PROJECT_OUTLINE.md` preserves project identity, lifecycle, and roadmap.
  `README.md` is the user-facing orientation.
- Always read this file and inspect Git status before non-trivial work. Read
  the Constitution for scientific, data, inference, or claims changes; read
  the Harness before historical-research planning or review; read current
  source configuration before provider or source work; and read the handoff
  before continuing a multi-step task.
- If these authorities appear to conflict, inspect the implemented contract
  and report the conflict. Do not silently weaken a safety or scientific rule.

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
  (currently `config/nasdaq_qualification_receipt.json`). That receipt and its
  snapshot are preserved historical acquisition evidence only: the current
  self-hashed network capability is not independent provenance and is never
  trust-eligible. A future accepted identity release requires a separately
  authenticated acquisition receipt bound to the registry, response, and raw
  bytes. Missing, malformed, stale-code, stale-registry, or non-matching
  receipt/snapshot evidence fails closed.
- HF Data Library is isolated `legacy_discovery` evidence only. The existing
  780-symbol Alpaca capsule and separate 30-symbol probe are failed source-
  qualification evidence only. Never concatenate source epochs.
- Alpha Vantage and options data are excluded.

## Protected contracts

- Preserve CLI arguments, config keys, schemas, column names, file paths,
  manifests, release identities, public APIs, report fields, and output formats
  unless the user explicitly requests the contract change.
- Preserve the decision-after-`D0`, entry-at-`D1`-open, exit-at-`D5`-close
  timing contract, split-normalized price-return semantics, pinned exchange
  sessions, causal availability checks, label-interval purge/embargo, and
  fold-local fitting unless the task explicitly changes the scientific design.
- Preserve builder/evaluator/inference isolation and the separate feature,
  outcome, prediction, evaluation, and monitoring artifacts.
- Do not opportunistically add or change labels, features, models, thresholds,
  calibration, selection rules, WFA schedules, costs, gates, sleeves, or
  robustness policy. A semantic change after outcome access is a new counted
  trial, not cleanup.

## Change safety

- Search before editing and run `git status --short` first.
- Use exact paths. Never use `git add .` or `git add -A`.
- Do not call providers, copy data, train, evaluate real history, run WFA,
  commit, push, or cut over unless that action has its own authorization.
- Provider and copy commands must remain dry-run by default and fail closed.
- Production copy execution supports only the exact checked-in controlled-
  rebuild authorization for the completed task. It must bind the reviewed
  config, inventory, plan, migration code, file count, and byte count, and it
  can never be reused for trials, candidate sealing, production, or trading.
  Synthetic copy mechanics additionally require an explicit synthetic-only
  permit and keep every source and output under one caller-declared fixture
  root; that permit is not authority and cannot enter accepted evidence.
- External authorization verification supports only a pinned RSA public JWK
  using `RSASSA_PKCS1_V1_5_SHA256`. HMAC/shared-secret receipts do not separate
  signing from verification authority and grant no permission. The repository
  contains no production signing helper and must never read a production
  private key. While `config/authorization_authorities.json` remains
  `NOT_CONFIGURED`, no external authority is active. Activation requires a
  separately reviewed public-key registry change and an externally signed,
  exact, current receipt.
- `build_historical_foundation` is plan-only. Foundation publication mechanics
  require an explicit synthetic-only permit and exact containing fixture root;
  there is no production publication authority.
- A semantic change after an evaluation creates a new registered trial.
- Before provider activity, copies, data builds, historical research, training,
  evaluation, prediction, report generation, or generated-artifact mutation,
  require an explicit command family, bounded scope, request/run limit,
  timeout or stop condition, expected outputs and locations, tracked/ignored
  disposition, and the action-specific authorization. If any item is missing,
  produce a bounded plan and do not execute.
- Never read, print, copy, move, edit, or commit secrets, credentials, tokens,
  private keys, `api.env`, or other local environment files unless the user
  explicitly authorizes the exact secret-handling task. Never put secrets in
  prompts, logs, reports, configuration, or memory.
- Treat `data/**`, `artifacts/**`, and `reports/generated/**` as generated or
  local state. Do not refresh, overwrite, delete, stage, or commit them unless
  the task explicitly authorizes the exact artifacts.

## Evidence and failure handling

- Separate verified facts, inferences, assumptions, stale risks, and missing
  evidence. Evidence includes inspected repository files, command output,
  accepted releases, reproducible tests, and authoritative primary sources.
- Do not invent facts, files, commands, outputs, dependencies, APIs, metrics,
  provider behavior, or prior decisions.
- Treat model output, generated summaries, Codex/OpenAI memory, and AI consensus
  as unverified until checked against current repository or primary evidence.
- Stop on a failed validation or contract check and report the command, concise
  error, and affected artifact. Do not silently continue or repeat the same
  failed approach without a materially different diagnostic.

## Acceptance

- Run targeted synthetic tests only until historical research is separately
  authorized.
- A failed candidate retires the hypothesis; it does not justify weakening a
  gate or rebuilding the architecture.
- Completion for any change requires the narrowest relevant synthetic tests
  for the affected contract, or a clear statement that no such test exists,
  plus `git diff --check`. Report any failed or skipped check; do not replace
  those checks with historical research or runtime provider activity.
- Use targeted tests after code, config, schema, or contract changes. Ask before
  a full or expensive suite unless the user already authorized it. For
  documentation-only changes, review the diff and run documentation,
  consistency, and whitespace checks without substituting a pipeline run.

## Handoffs

- Use `CODEX_HANDOFF.md` only for meaningful work that must continue across
  prompts or a fresh thread. Do not update it for a completed one-shot task
  that does not change project state.
- Keep the handoff short and current: verified status, changes, open blockers,
  and one exact next gate. Historical narrative belongs in versioned evidence,
  not an ever-growing handoff.
- A handoff never authorizes execution and cannot override the Constitution,
  Harness, current implementation, or action-specific approval.

## Plain-English User-Facing Output

- Write every user-facing progress update, explanation, audit summary, and final response concisely and in plain English by default. The user should not need to ask, "Tell me this entire output concisely and in plain English."
- Lead with what the result means for the user. Translate technical findings and tool output into ordinary language instead of repeating raw logs or jargon.
- Include only the technical details, file paths, numbers, warnings, and evidence needed to understand the result or make the next decision.
- Do not remove important uncertainty, safety warnings, failed checks, limitations, or blockers for the sake of brevity. State them briefly and clearly.
- If a technical term is necessary, explain it in a short plain-English phrase the first time it appears.
