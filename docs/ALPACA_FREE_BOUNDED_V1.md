# ALPACA_FREE_BOUNDED_V1

`ALPACA_FREE_BOUNDED_V1` is an opt-in, completely free research-data profile.
It does not relabel or modify the strict reference contract frozen at
`c29e244174940f76babf75bcf91bbd11ca470c46`.

The primary estimand is
`ALPACA_SIP_5_SESSION_LONG_SHORT_GROSS_RETURN_EX_BORROW_COSTS`: Alpaca SIP
five-session gross long/short trading return before stock-borrow and locate
costs, using a bounded provider-reconstructed universe of affirmatively
verified U.S. primary-listed common stocks, prospective easy-to-borrow short
gating, and explicit unresolved-event reporting.

This is not complete total return, net trading return, a complete terminal
economic return, a borrow-cost-adjusted result, a reconstruction of everything
providers knew in 2016, or proof that a historical short could be borrowed.

## Source and evidence contract

- Alpaca historical bars start with a requested raw-data date of `2016-01-01`.
  Every accepted request explicitly pins `feed=sip`, `timeframe=1Day`,
  `adjustment=raw`, ascending order, no `asof`, and a 20-minute end lag. The
  20-minute project setting is stricter than the documented free-plan minimum.
- Alpha Vantage `LISTING_STATUS` active and delisted CSV responses at explicit
  dates are historical-universe candidates. They remain candidates until live
  known-case probes establish schema, date semantics, classification quality,
  ticker behavior, later-delisted coverage, duplicates, and completeness.
- `nasdaqlisted.txt` and `otherlisted.txt` are captured prospectively with their
  complete bytes and embedded file-creation times. Files captured now are not
  historical membership evidence.
- The complete available Alpaca U.S.-equity asset list is captured
  prospectively. `borrow_status` is canonical; deprecated `easy_to_borrow` is
  retained only for compatibility and contradiction detection.
- Alpaca corporate-action REST pages preserve all documented groups plus
  unknown groups. The mutation stream is optional and must pass a free-account
  capability probe before use; REST changed-response capture is the fallback.
- Every record is labeled `HISTORICAL_RECONSTRUCTED` or
  `PROSPECTIVE_AS_OBSERVED`. Reconstructed evidence is never relabeled as
  prospectively observed.

Raw responses live only under the ignored `data/` tree. The append-only store
content-deduplicates raw bytes by SHA-256 while retaining every receipt
occurrence, request/receipt time, safe header, status, page/retry lineage,
provider request ID, parsing/validation state, evidence class, and changed
response. Provider error payloads are preserved. Credential values never enter
plans, URLs in receipts, logs, fixtures, or configuration.

## Universe and identity

For signal session `T`, universe inputs stop at `T-1`. The primary profile
selects the top 500 affirmatively verified U.S. primary-listed common stocks on
Nasdaq, NYSE, or NYSE American with previous close at least $5, 60 valid prior
sessions, and highest trailing 60-session median dollar volume. The separately
named top-1,000 sensitivity profile cannot replace the primary profile after
outcome inspection.

Every candidate is retained with selected/rejected status and every reason.
ETFs/ETPs, ADRs, preferreds, warrants, rights, units, funds, structured,
leveraged/inverse, OTC, test, unresolved-identity, unknown-type, and
insufficient-history candidates fail closed. Stable provider IDs and separate
effective/knowledge times preserve ticker changes and reject overlapping ticker
reuse or contradictory identity mappings.

## Long, short, and terminal events

The fixed horizon is D1 regular-session open through D5 regular-session close.

```text
long  = (terminal value + distributions received - entry price) / entry price
short = (entry price - terminal value - distributions owed) / entry price
```

Short returns are not clipped at -100%. Splits change quantity without creating
profit or loss. Verified cash, stock, stock-and-cash mergers, redemptions, and
worthless removals use explicit terms. A zero terminal value requires explicit
zero-consideration evidence. Missing terms, inaccessible OTC continuation,
halts, bankruptcy, liquidation, unavailable successor value, and other
unresolved terminal cases remain in the denominator; the last listed price is
never substituted.

Every historical reconstructed short carries
`SHORTABILITY_UNVERIFIED_HISTORICAL`, `SHORT_EXECUTION_APPROXIMATE`, and
`BORROW_COST_ASSUMPTION_ZERO`.

Every prospective short requires a pre-order, unexpired snapshot with active,
tradable, marginable, shortable, canonical `borrow_status=easy_to_borrow`, no
contradiction, and at least one whole share without upward rounding. A later
degradation generates a next-opportunity buy-to-cover instruction but never a
fabricated fill. Fixed-horizon and fill-based executed returns remain separate.

Stock-borrow fee and locate fee are exactly zero. Every result is labeled
`GROSS OF STOCK-BORROW AND LOCATE COSTS`. Spread, slippage, other transaction
charges, distributions owed, corporate-action liabilities, failed execution,
buy-in risk, and configured market impact are not implicitly zero.

Unresolved long rows receive the adverse `-100%` stress label. Every unresolved
short row receives all three preregistered buy-in scenarios: 2x entry (`-100%`),
3x (`-200%`), and 5x (`-400%`). They are scenarios, not estimates, predictions,
or lower bounds; short loss has no finite lower bound.

## Windows commands

This checkout has no required installed package environment. With the pinned
Python 3.11.9 runtime, select the repository source path first:

```powershell
$env:PYTHONPATH='src'
```

Validate configuration, plan capability probes, and show known-case gates:

```powershell
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded validate-config
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded validate-credentials
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded probe-capabilities --as-of 2026-08-10
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded known-case-diagnostics
```

Plan or resume a bounded historical backfill without network access:

```powershell
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded plan-backfill --symbol AAPL --symbol META --start 2016-01-01 --end 2026-08-08 --requested-at 2026-08-10T20:00:00Z --full
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded resume-backfill --symbol AAPL --symbol META --start 2016-01-01 --end 2026-08-08 --requested-at 2026-08-10T20:00:00Z --completed-unit-id UNIT_SHA256 --full
```

Plan one prospective premarket snapshot or completed-session capture:

```powershell
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded capture-premarket --as-of 2026-08-10
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded capture-completed-session --session 2026-08-07 --symbol AAPL --symbol META
```

The governed calendar qualification and the deterministic two-phase daily
interface are:

```powershell
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded plan-calendar-qualification
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded qualify-calendar-successor --approved-plan-id PLAN_SHA256 --owner-confirmation YES
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded plan-daily-capture --session 2026-08-11 --phase pre-decision
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded plan-daily-capture --session 2026-08-10 --phase completed-session --symbol AAPL
```

Calendar qualification validates both accepted releases, the locked
`exchange-calendars` runtime, the complete session tables, the original and
successor content hashes, and exact byte equality before it can bind the
successor to this opt-in profile. It does not alter the strict/default calendar
binding, activate a source, or authorize research. A changed record ID, changed
session hash, nonzero session difference, missing predecessor release, or
unqualified successor fails closed.

Network execution is owner-operated and one page/attempt per invocation. It
requires the exact plan ID printed by the matching plan, the explicit flag,
`FREE_SOURCE_QUALIFICATION_APPROVED=YES`, and applicable environment-only
credentials. A retryable 429/5xx response is preserved and produces a bounded
jittered next-attempt instruction; retry uses a fresh invocation. The variables
are `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`, and `ALPHA_VANTAGE_API_KEY`; values
must not be stored in the project.

```powershell
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded execute-source --source assets --as-of 2026-08-10 --approved-plan-id PLAN_SHA256 --execute-network
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded execute-source --source alpha-active --as-of 2020-08-28 --approved-plan-id PLAN_SHA256 --execute-network
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded execute-source --source bars --as-of 2020-08-24 --end-exclusive 2020-09-05 --symbol AAPL --approved-plan-id PLAN_SHA256 --execute-network
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded execute-source --source bars --as-of 2026-08-10 --end-exclusive 2026-08-11 --symbol AAPL --prospective --approved-plan-id PLAN_SHA256 --execute-network
```

Validate receipts, rebuild the universe, report, and check readiness:

```powershell
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded validate-receipts
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded rebuild-prospective-universe --session 2026-08-11 --receipt-id ASSET_RECEIPT --receipt-id NASDAQ_LISTED_RECEIPT --receipt-id NASDAQ_OTHER_RECEIPT
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded record-daily-capture --session 2026-08-11 --phase pre-decision --receipt-id ASSET_RECEIPT --receipt-id NASDAQ_LISTED_RECEIPT --receipt-id NASDAQ_OTHER_RECEIPT --universe-snapshot-id SNAPSHOT_SHA256
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded validate-capture-ledger
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded rebuild-universe --input data/w/alpaca_free_bounded/universe_input.json --profile ALPACA_FREE_BOUNDED_V1
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded coverage-report --input data/w/alpaca_free_bounded/coverage_input.json
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded event-status-report --input data/w/alpaca_free_bounded/outcomes.json
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded check-readiness --synthetic-tests-passed
```

No command automatically starts a full backfill or creates an operating-system
scheduled task. Paper trading remains absent/disabled by default; no fill can
exist without preserved order/fill evidence.

## Prospective capture ledger and soak policy

The ignored append-only JSONL ledger records each session and phase, the T-1
information cutoff, pre-decision cutoff, qualified calendar, capture and source
plan IDs, request and receipt times, receipt IDs, content hashes, parser and
validation state, terminal pagination, evidence class, missing-source reason,
retry state, changed-response predecessor, universe snapshot ID, and the final
session status. A later record links to its predecessor; it never overwrites a
failed or partial occurrence.

Phase A accepts Alpaca assets/borrow fields and both Nasdaq directory files only
when their local receipt times precede the session open. Its deterministic
candidate-universe snapshot retains every directory symbol and every inclusion
or exclusion reason; top-500 selection remains pending until T-1 liquidity
inputs exist. Phase B accepts only completed-session Alpaca `sip`/`1Day`/`raw`
bars and the corporate-action REST snapshot after the configured delay.

The preregistered infrastructure soak is 20 consecutive completed XNYS
sessions. Every expected source must be present or explicitly failed, with no
IEX, feed mixing, incomplete pagination, post-cutoff borrow evidence, silent
receipt gap, raw file in Git, or nondeterministic derived snapshot. States are
`PROSPECTIVE_CAPTURE_SOAK_NOT_STARTED`,
`PROSPECTIVE_CAPTURE_SOAK_IN_PROGRESS`,
`PROSPECTIVE_CAPTURE_SOAK_FAILED`, and
`PROSPECTIVE_CAPTURE_SOAK_COMPLETE`. Soak completion can support a later
readiness review; it never creates `PROSPECTIVE_RESEARCH_READY`, training, or
evaluation authority.

## Validated state on 2026-08-10

Bounded live validation completed for delayed Alpaca SIP raw daily bars,
Alpaca assets, Alpaca corporate-action REST snapshots, dated Alpha Vantage
`LISTING_STATUS`, and the two prospective Nasdaq directory files. The ignored
append-only evidence store contains 27 validated receipt occurrences. Repeated
logical requests retained distinct occurrence metadata, identical content was
content-deduplicated, and changed asset bytes were retained as a successor
revision.

Alpha Vantage returned canonical CSV responses for seven bounded date/state
requests, but the data used current identity names retroactively for known
historical cases (including pre-change META and pre-terminal BBBY naming) and
did not establish exact daily point-in-time membership. The historical-universe
classification is therefore `HISTORICAL_RECONSTRUCTED_WITH_LIMITATIONS`, not
established. AAPL, FB/META, and ATVI known cases are partial; LK/LKNCY and
BBBY/BBBYQ remain unresolved because the available evidence does not prove a
terminal economic exit.

The strict/default SIP qualification remains bound to its original calendar
release. The opt-in profile completed a governed cutover to accepted successor
`834ee91a92b21e0c0d053b80f6e0404c14a7d0520417fc83f530b78d475ba3f7`.
Its 9,049-session payload is byte-identical to strict release
`71a5620bc4a02b13a915a76f5ce5028eac0f9ac64eab8dd4c4bd021c15a31c7d`;
both session payloads have SHA-256
`9268af4703b5709409e3e119b34737542aaeaaee1311ede80d337d44227a2e69`,
and the exact session difference count is zero. The old release remains
recoverable. No accepted release, strict source binding, or environment hash
was edited.

Current profile states are:

- `DATA_INFRASTRUCTURE_READY`
- `LIVE_SOURCE_VALIDATION_PENDING` for exact Alpha Vantage historical-universe semantics
- `HISTORICAL_RECONSTRUCTED_WITH_LIMITATIONS`
- `PROSPECTIVE_CAPTURE_READY`
- `TRAINING_BLOCKED`
- `EVALUATION_BLOCKED`

`HISTORICAL_RESEARCH_READY` and `PROSPECTIVE_RESEARCH_READY` are false. The
offline readiness command intentionally cannot promote either state from
diagnostic flags.

## Readiness boundary

Offline implementation and synthetic tests can establish
`DATA_INFRASTRUCTURE_READY`. Until bounded live probes validate Alpaca coverage,
Alpha Vantage historical membership semantics, and repeatable daily capture,
the profile remains `LIVE_SOURCE_VALIDATION_PENDING` for Alpha Vantage exact
historical-universe semantics. Exact membership gaps keep historical evidence
at `HISTORICAL_RECONSTRUCTED_WITH_LIMITATIONS`, never silently research-ready.

`TRAINING_BLOCKED` and `EVALUATION_BLOCKED` remain present. Provider responses,
data-infrastructure readiness, or a limited historical reconstruction do not
authorize training, model comparison, strategy-performance testing, historical
trial registration, source activation, publication, or trading.
