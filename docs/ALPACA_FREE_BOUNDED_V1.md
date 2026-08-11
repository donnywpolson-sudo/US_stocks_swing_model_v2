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

Daily-bar plans encode exact RFC 3339 bar-timestamp bounds: New York midnight
for the first session through one second after New York midnight for the final
session. Date-only bounds are prohibited because live free-account evidence
showed that they can be interpreted as recent SIP data after a session is
complete. Both official historical endpoint forms returned the same completed
AAPL bars with the corrected interval; deterministic acquisition continues to
use the multi-symbol form.

Plan the calendar-bound T-1 operating order and bounded liquidity warm-up:

```powershell
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded plan-operational-capture --session 2026-08-11
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded plan-liquidity-warmup --source-snapshot data/w/alpaca_free_bounded_v1/prospective_universe/SNAPSHOT.json --pilot-symbol-count 5
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded execute-liquidity-warmup --source-snapshot data/w/alpaca_free_bounded_v1/prospective_universe/SNAPSHOT.json --pilot-symbol-count 5 --checkpoint data/w/alpaca_free_bounded_v1/liquidity_warmup_pilot_checkpoint.json --approved-plan-id PLAN_SHA256 --execute-network
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded plan-liquidity-warmup --source-snapshot data/w/alpaca_free_bounded_v1/prospective_universe/SNAPSHOT.json
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded validate-warmup-checkpoint --source-snapshot data/w/alpaca_free_bounded_v1/prospective_universe/SNAPSHOT.json --checkpoint data/w/alpaca_free_bounded_v1/liquidity_warmup_checkpoint.json
python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded build-liquidity-universe --source-snapshot data/w/alpaca_free_bounded_v1/prospective_universe/SNAPSHOT.json --checkpoint data/w/alpaca_free_bounded_v1/liquidity_warmup_checkpoint.json
```

The warm-up is limited to 90 completed qualified XNYS sessions and the
prospectively observed inventory. Bars remain `HISTORICAL_RECONSTRUCTED`;
membership and identity remain `PROSPECTIVE_AS_OBSERVED`. It computes only
previous close, valid-session count, and trailing 60-session median dollar
volume. It cannot produce strategy features, outcomes, predictions,
performance, training, or evaluation.

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

No command automatically starts a full backfill. The owner-operated installer
described below creates only the prospective evidence-capture scheduled task;
it contains no prediction, training, evaluation, paper-order, or live-order
operation.

## Prospective automation acceptance and background monitoring

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

`TWO_SESSION_AUTOMATION_ACCEPTANCE_V1` is the mandatory operational gate. It
requires two consecutive fully successful scheduled XNYS sessions with zero
inherited credit. One session proves only one unattended cycle and is
insufficient. The second must also prove next-session rollover, checkpoint
continuation, append-only predecessor chaining, rolling-liquidity advancement,
and deterministic universe reconstruction. The states are:

- `TWO_SESSION_AUTOMATION_ACCEPTANCE_NOT_STARTED`
- `TWO_SESSION_AUTOMATION_ACCEPTANCE_IN_PROGRESS`
- `TWO_SESSION_AUTOMATION_ACCEPTANCE_FAILED`
- `TWO_SESSION_AUTOMATION_ACCEPTANCE_COMPLETE`
- `AUTOMATION_PAUSED_STRUCTURAL_FAILURE`

A transient failure preserves the failed generation and its prior credit, then
creates a zero-credit successor for the next eligible XNYS session. A
structural calendar, schema, credential-safety, receipt-chain, ledger,
universe-determinism, code-binding, or Git-exposure failure pauses later network
activity until remediation. No failed occurrence is overwritten.

After acceptance, `NONBLOCKING_BACKGROUND_RELIABILITY_MONITOR` continues as a
rolling 20-session operational report. It records expected, complete, partial,
and late sessions, provider and pagination failures, retries, missing symbols,
universe determinism, top-500 count, and task timing. It is telemetry, not a
blocking prerequisite for a later separately authorized historical exploratory
development phase. An isolated later provider failure does not revoke completed
two-session acceptance. Structural defects or repeated unresolved failures may
pause operations. Two sessions do not prove long-term reliability, strategy
validity, statistical performance, production readiness, trading readiness, or
that future captures cannot fail.

Completion may report `NEXT_PHASE_HISTORICAL_EXPLORATORY_DEVELOPMENT_ELIGIBLE`
under `HISTORICAL_RECONSTRUCTED_WITH_LIMITATIONS`, but it never creates
`HISTORICAL_RESEARCH_READY`, `PROSPECTIVE_RESEARCH_READY`, training, evaluation,
predictions, backtests, or orders.

## Windows daily capture operation

The tracked master wrapper is
`scripts/run_alpaca_free_daily_capture.ps1`. The scheduled task is
`USStocksSwingV2-Alpaca-Free-Daily-Capture`, triggered on weekdays at 4:15 AM
America/Los_Angeles. The qualified XNYS calendar remains the runtime authority:
a weekend, holiday, or other non-session weekday records
`SKIP_NON_XNYS_SESSION`, performs zero provider requests, changes no acceptance
credit, and exits successfully.

For session T, the wrapper derives all deadlines from the qualified calendar's
actual open: Phase B targets T open minus two hours and must validate by minus
90 minutes; Phase A targets minus 60 minutes; the final ledger and universe
cutoff is minus 15 minutes. T-1 is the immediately previous XNYS session, not
the previous calendar day. This preserves daylight-saving, holiday, and
session-order behavior without hard-coded UTC opens.

Phase B acquires SIP/raw/1Day bars for the complete current liquidity-ranking
candidate population plus the corporate-action REST snapshot. It advances the
90-session rolling window without forward filling. Phase A acquires the complete
Alpaca U.S.-equity asset master and both contracted Nasdaq directory inputs.
The final snapshot retains every inclusion and exclusion reason, requires at
least 60 valid sessions, and deterministically selects the top 500. A new
candidate remains visible as `INSUFFICIENT_HISTORY` and enters a bounded
historical warm-up queue; its prior bars remain `HISTORICAL_RECONSTRUCTED`,
while prospectively received membership and identity evidence remain
`PROSPECTIVE_AS_OBSERVED`. IEX fallback is prohibited.

From Windows PowerShell in the repository root:

```powershell
.\scripts\install_alpaca_free_daily_capture_task.ps1 -DryRun
.\scripts\install_alpaca_free_daily_capture_task.ps1
.\scripts\show_alpaca_free_daily_capture_status.ps1
Disable-ScheduledTask -TaskName 'USStocksSwingV2-Alpaca-Free-Daily-Capture'
Enable-ScheduledTask -TaskName 'USStocksSwingV2-Alpaca-Free-Daily-Capture'
.\scripts\remove_alpaca_free_daily_capture_task.ps1
```

The installer validates the exact repository, branch ancestry, clean tree,
qualified calendar, ignored/untracked `api.env`, canonical credential presence,
verified Python path, wrapper, and a no-network dry-run. The least-privileged
current-user interactive-token registration stores no provider credential or
Windows password in the task. The computer must be powered on or sleeping, not
shut down. `WakeToRun` depends on Windows, firmware, and hardware support; a
missed pre-decision cutoff remains a failed prospective session.

Operational status is stored in ignored
`data/w/alpaca_free_bounded/automation/latest_status.json`; rotated text logs
are under `data/w/alpaca_free_bounded/automation/logs`. These contain safe IDs,
states, and classifications, never credentials, Authorization headers, raw
provider bodies, or passwords. Raw receipts and append-only ledgers are never
removed by log rotation or scheduled-task removal.

## Validated state on 2026-08-10

Bounded live validation completed for delayed Alpaca SIP raw daily bars,
Alpaca assets, Alpaca corporate-action REST snapshots, dated Alpha Vantage
`LISTING_STATUS`, and the two prospective Nasdaq directory files. The ignored
append-only evidence store contains 32 integrity-validated receipt occurrences,
including explicit provider failures. Repeated
logical requests retained distinct occurrence metadata, identical content was
content-deduplicated, and changed asset bytes were retained as a successor
revision.

The first daily-orchestration cycle captured the 2026-08-11 pre-decision asset
and directory phase before the XNYS open and deterministically retained 13,117
directory candidates, including all exclusion reasons; 5,016 were eligible for
later T-1 liquidity inputs. The 2026-08-10 completed-session corporate-action
snapshot passed. The original date-only AAPL request returned HTTP 403 and
remains visible as `PARTIAL_FAIL_CLOSED`. A six-request diagnostic matrix then
used exact RFC 3339 intervals for the 2026-08-10, 2026-08-07, and 2026-08-03
daily bars on both official historical endpoint forms. All six returned HTTP
200 with one accepted SIP/raw/1Day bar and terminal pagination. The result is
`END_INTERVAL_CONSTRUCTION_DEFECT`, not a permanent account entitlement
failure. The earliest live receipt established here was approximately 6 hours
49 minutes after the 2026-08-10 close and before the next XNYS open; earlier
delay cells had already passed and were not fabricated.

The complete 90-session warm-up covered all 5,016 prospectively eligible
candidates in 51 deterministic resumable units. It produced 3,754
liquidity-ready securities and a deterministic 500-security selection with
snapshot ID
`e96b4f5aaa0d5ee88587d5bebc63cffa4cab76c07a4788672a13ff3a29bdcff3`.
The original failed soak and every later generation remain immutable. The valid
zero-credit 20-session generation is superseded only as a blocking policy by
the owner decision above; its evidence remains preserved. The new two-session
generation starts at zero after the implementation commit exists and cannot
inherit any prior credit.

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
