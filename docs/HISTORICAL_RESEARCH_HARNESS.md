# Stocks and ETFs historical discovery harness

Version: `1.1.0-mechanics`
Project: `US_stocks_swing_model_v2`
Status: deterministic synthetic mechanics executor implemented and adversarially
tested; full readiness remains blocked by the accepted real-data chain and exact
legacy-trial census
Harness target: `HISTORICAL_RESEARCH_READY`
Historical evidence scope: `LEGACY_DISCOVERY_ONLY_PIT_UNRESOLVED`

This document specifies the governance harness required before any separately
authorized historical discovery. It grants no execution authority by itself. The
current controlled-rebuild receipt permits approved hash-copy, non-alpha data
validation, synthetic-fixture fitting/WFA, and bounded free Alpaca/Nasdaq
qualification. Synthetic results can never count as alpha evidence. Paid data,
real-history hypothesis/WFA execution, candidate sealing, destructive cutover,
external push, trading, and all writes to the legacy repository remain hard-
paused pending new user authorization.

The harness is local to this repository. A common interpreter and byte-identical
provenance-preserving source copies are permitted, but no mutable data path,
environment, trial ledger, fold decision, model artifact, readiness receipt, or
evaluation result is shared with a futures project. Repository root, Git
directory, environment-lock hash, release ID, ledger path, bundle path, and
readiness receipt must be distinct and verified by no-cross-import/no-cross-write
tests. "Global trial ledger" below means all outcome-informed attempts in this
stock/ETF project; outcome-informed ideas transferred from another project must
be recorded as external exposure rather than treated as pristine.

The current historical source evidence lacks a point-in-time security master,
complete delisting outcomes, and trustworthy historical stock/ETF membership.
Therefore even a mechanically valid historical run is discovery only. It cannot
prove PIT-safe alpha. Harness readiness and evidence scope are separate:
`HISTORICAL_RESEARCH_READY` may certify only that the mechanical harness and
non-alpha release chain are ready, while
`historical_evidence_scope=LEGACY_DISCOVERY_ONLY_PIT_UNRESOLVED`,
`candidate_eligibility=BLOCKED_PENDING_PROSPECTIVE_PIT`, and all alpha/live claims
remain false. Prospective daily as-received identity and membership evidence is
required to remove this evidence ceiling.

## 1. Research basis

Model selection and performance estimation are separate nested operations.
Using the same validation results for both creates selection bias.

- [Varma and Simon, 2006](https://doi.org/10.1186/1471-2105-7-91)
- [Cawley and Talbot, 2010](https://www.jmlr.org/papers/v11/cawley10a.html)

The time ordering and dependence of five-session labels require rolling-origin
evaluation and interval gaps, not random row cross-validation.

- [Burman, Chow, and Nolan, 1994](https://doi.org/10.1093/biomet/81.2.351)
- [Racine, 2000](https://doi.org/10.1016/S0304-4076(00)00030-0)
- [Tashman, 2000](https://doi.org/10.1016/S0169-2070(00)00065-0)

Forecast comparison and economic inference must respect serial and
cross-sectional dependence.

- [Diebold and Mariano, 1995](https://doi.org/10.1080/07350015.1995.10524599)
- [Newey and West, 1987](https://doi.org/10.2307/1913610)
- [Politis and Romano, 1994](https://doi.org/10.1080/01621459.1994.10476870)
- [Petersen, 2009](https://doi.org/10.1093/rfs/hhn053)
- [Cameron, Gelbach, and Miller, 2011](https://doi.org/10.1198/jbes.2010.07136)
- [Romano and Wolf, 2005](https://doi.org/10.1111/j.1468-0262.2005.00615.x)

Selection-bias diagnostics use the Deflated Sharpe Ratio and the Probability of
Backtest Overfitting/CSCV literature.

- [Bailey and Lopez de Prado, Deflated Sharpe Ratio](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf)
- [Bailey, Borwein, Lopez de Prado, and Zhu, PBO](https://doi.org/10.21314/JCF.2016.322)

Alpaca documents explicit feed, adjustment, and `asof` symbol-mapping parameters.
Symbol mapping is not historical membership truth. The actual free feed must be
proved by an entitlement-qualification receipt and recorded in every release; it
is never assumed. `timeframe=1Day` and `adjustment=raw` are pinned, while `asof`
is omitted/null unless a valid ISO mapping date is deliberately requested.

Historical source roles and epochs are binding. Existing HF Data Library bars are
legacy discovery only and retain two distinct epochs: PiTrading-consolidated
through 2022-03-03 and IEX-only from 2022-03-04. They cannot be silently pooled as
one identical feed. Failed legacy Alpaca capsules are qualification evidence only,
never active research bars. A qualified prospective Alpaca feed and as-received
Nasdaq identity/membership snapshots begin separate immutable epochs. Every feed,
methodology, or identity change creates a new release and epoch.

- [Alpaca market-data plans](https://docs.alpaca.markets/us/docs/about-market-data-api)
- [Alpaca historical-bars reference](https://docs.alpaca.markets/us/reference/stockbarsingle-1)
- [Alpaca market-data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq)

## 2. Binding claims boundary

The model evaluates underlying stock and ETF forecasts only. Its primary output
is the absolute distribution of the next tradable five-session underlying price
return: expected return and `p_up`, `p_down`, and `p_neutral`. Rank is secondary.

Historical results cannot claim:

- point-in-time historical membership or survivorship safety;
- option returns, option execution, strike/expiry selection, implied-volatility
  edge, or options profitability;
- deployable short P&L when borrow availability and cost are excluded;
- prospective confirmation or pristine holdout status;
- that stock, ETF, long, and short sleeves share performance.

Every report must record `borrow_cost_mode=EXCLUDED_BY_SCOPE`,
`short_implementation_claim=false`, and `options_execution_claim=false`.

## 3. Trial ledger and preregistration

The repository owns one append-only, SHA-256 hash-chained trial-event ledger.
Each event records sequence, prior hash, UTC time, trial/parent IDs, hypothesis,
multiplicity family, information set, the four sleeve IDs, immutable source/data
hashes, code and environment, charter/feature/label/split/cost hashes, role,
event type, outcome-access flag, trial-count flag, status, and reason.

Appends are lock-protected, atomic, and fail closed on a sequence or hash mismatch.
Before any outcome unlock, the current ledger head is anchored in an immutable
receipt binding a clean code commit, release hash, environment hash, and charter.
Rewriting the ledger and recomputing its chain is invalid. A machine-readable
legacy trial census, including uncertain and manual plot/report exposure, has a
documented lower bound of 62 outcome-informed attempts, not an exact count.
Diagnostics must penalize with at least `max(62, every subsequently discovered
attempt)` and may not estimate a smaller effective count. The trusted gate is
`INVALID_TRIAL_CENSUS_UNRESOLVED` until the exact census is reconciled; the
unknown count can never be treated as zero trials.

Every outcome-informed variation counts, including abandoned smokes and changes
to features, thresholds, neutral band, universe, horizon, adjustment treatment,
cost, sizing, ranking rule, abstention, refit cadence, or selection metric.
Synthetic fixtures and outcome-free source validation are ledgered but do not
count as alpha trials. A release or repository rename cannot reset multiplicity
when historical outcomes overlap. Only genuinely new prospective sessions may
start an independent confirmation information set.

Multiplicity-family assignment and its rule hash must predate outcome access.
Overlapping outcomes, shared strategy ancestry, or transferred outcome-informed
ideas cannot be split into new families merely to reduce the trial count.

The append-only transitions are `DECLARED`, `BUILT`, `OUTER_EVALUATED`, then
either `OUTER_SCREEN_PASS` or `CLOSED`. A passing screen may proceed only through
`HOLDOUT_SPEC_FROZEN`, one optional `HOLDOUT_ACCESSED`, and `HOLDOUT_PASS` or
`CLOSED`. Declaration and contract hashes must predate outcome access. Any
semantic change after an outer result creates a new counted trial.

## 4. Five-session sample contract

For a prediction decision after regular-session close on pinned exchange session
`D0`:

```text
decision_at = D0 close + provider publication latency
entry_at    = D1 regular-session open
exit_at     = D5 regular-session close
target      = (split-normalized exit price / split-normalized entry price) - 1
```

`D1` through `D5` are exactly five pinned exchange sessions, never five calendar
days. The position cannot receive the `D0` close as a fill. This is explicitly a
simple price return, not a log return. The target excludes
dividends and applies only split ratios effective in `(D1, D5]`. Unresolved
mergers, spinoffs, conversions, missing paths, halts, and delistings retain an
explicit unresolved outcome; they are not deleted.

Every sample carries sample, hypothesis, sleeve, asset UUID, as-received symbol,
stock/ETF type, exchange, session IDs, calendar/source/identity/action release
IDs, source epoch, membership-evidence status, asset-type-evidence status,
historical-proxy flag, feature-window start, `feature_available_at`, `decision_at`,
intended entry, label start/end, intended exit, outcome-resolution status, and
final outcome status.

The mandatory ordering is:

```text
feature_window_start <= feature_available_at <= decision_at
< entry_at = label_start_at < label_end_at = intended_exit_at
```

Unknown, ambiguous, nonstandard, or unavailable stock/ETF identity at the
decision cutoff forces abstention. A retrospective asset snapshot cannot be
used as historical membership evidence.

Missing entries, exits, corporate actions, delistings, or required price paths
retain explicit statuses, remain in coverage denominators, and cannot be deleted
after outcome access. Their predeclared handling may yield
`INCONCLUSIVE_OUTCOME_COVERAGE`, but cannot depend on whether the omitted return
would help or hurt the model.

## 5. Information-interval purge and embargo

Label intervals are half-open: `[decision_at, D5 close)`. For every inner or
outer split, remove any training sample whose label interval intersects any
validation/test label interval. When a split permits training observations after
a validation block, also remove training decisions through the block's maximum
label end plus a five-exchange-session embargo.

The embargo is expressed in pinned exchange sessions, not rows or calendar days,
and cannot be shorter than the maximum chartered label span. Split artifacts
record pre/post-purge IDs, interval bounds, removal reasons, and a required zero-
overlap assertion.

## 6. Nested chronological walk-forward analysis

The default outer schedule is expanding-window rolling origin:

| Setting | Required value |
|---|---:|
| Initial training window | 1,008 exchange sessions |
| Outer test block | 126 exchange sessions |
| Step | 126 exchange sessions |
| Minimum outer folds | 8 |
| Minimum outer OOS | 1,008 distinct dates |
| Sealed final holdout | 252 sessions |

Each outer training window contains four chronological inner folds with
126-session validation blocks. Every hyperparameter, feature subset, neutral
band, probability calibration, threshold, abstention rule, sizing rule, refit
cadence, cost choice, and benchmark-based selection rule is chosen inside the
inner folds. The chosen configuration is frozen before the outer block.

Outer dates arrive sequentially. Scheduled parameter refits may use earlier
outer observations only after `D5` outcomes have matured. Hyperparameters cannot
change. If this schedule is infeasible, return `INCONCLUSIVE_DATA_LENGTH`; do
not shorten folds after viewing outcomes.

Outer OOS is a screen, not the final holdout. After an outer screen passes, the
complete model, features, neutral band, thresholds, abstention, costs, book
construction, inference code, and holdout test must be frozen before the one-time
holdout unlock. Outer and holdout results are reported separately and are never
pooled for selection. A holdout failure closes the trial; no retuning, rescue,
retry, or reuse of that holdout is permitted. A successor is a new counted trial
and cannot claim a fresh holdout drawn from dates already exposed. Neither screen
nor holdout passage authorizes candidate sealing.

## 7. Fold-local fit audit

Imputation, winsorization, scaling, volatility normalization, feature selection,
dimensionality reduction, calibration, neutral-band estimation, class weighting,
resampling, market/sector residualization, liquidity thresholds, risk scaling,
and sizing implement `fit(train_ids)` and record exact IDs and input hashes. Any
fit/test-ID intersection is fatal.

Deterministic causal lags may be materialized once only when every input was
available by `decision_at` and no population statistic was fitted. Full-period
ADV, future constituents, future security type, and revised corporate-action
truth are forbidden inputs.

## 8. Date-clustered uncertainty

The primary economic series is one aggregate underlying-book return per date,
not one observation per symbol prediction. The five overlapping cohorts remain
inside each date. HAC lag is
`max(4, floor(4 * (T / 100)^(2/9)))`. Confidence intervals are two-sided 95%;
the economic net-edge test is one-sided against mean net edge less than or equal
to zero.

Forecast-loss panels use two-way clustering by prediction date and security.
Each required dimension needs at least 30 clusters; otherwise the result is
`INCONCLUSIVE_CLUSTER_COUNT`. Stationary bootstrap inference uses 10,000
resamples of complete dates and their full cross-section, with a seed derived
from trial ID. The charter freezes the training-selected block length and
reports 5-, 10-, and 20-session sensitivity. Individual rows are never
resampled independently.

## 9. Metrics, multiple testing, DSR, and PBO

The binding primary forecast loss is multiclass log loss; the binding primary
economic statistic is the stress-cost underlying-book mean return. Brier score,
classwise calibration intercept/slope, expected-return robust absolute error, and
coverage/error at the predeclared abstention policy are diagnostics unless the
charter assigns them to a jointly adjusted primary family before access.
HAC-adjusted Diebold-Mariano loss differences and
Romano-Wolf joint comparisons test improvement over the strongest predeclared
baseline. Absolute up/down/neutral performance is primary; rank cannot rescue a
failed absolute forecast gate.

DSR applies only to the predeclared hypothetical underlying-book return series,
not classification accuracy. It uses concatenated outer-OOS net daily returns
and the raw count of all outcome-informed alternatives in the information
family. Passing requires DSR probability at least 0.95. Romano-Wolf adjusted
one-sided `p <= 0.05` is also required.

The predeclared family includes every tested stock/ETF and long/short sleeve,
horizon, primary metric, candidate/baseline contrast, and negative control. A
baseline is either selected inside the inner folds or all baselines remain in the
joint max-statistic family. No outcome-informed family split, metric substitution,
or choice of a favorable class is permitted.

PBO is computed only with at least ten comparable configurations and an even
number `S >= 8` of equal contiguous chronological blocks. The charter pins `S`,
the 25-basis-point-one-way stress-cost Sharpe selection statistic, and the
configuration set before access. CSCV enumerates every `S choose S/2` in-sample
block combination, uses its complement out of sample, selects only by the pinned
in-sample statistic, assigns deterministic midranks for ties, and records the
selected configuration's OOS rank and logit. PBO is the fraction of logits below
zero, using a common-date, 25-basis-point-one-way stress-cost return matrix.
Missing dates cannot be silently zero-filled. `PBO <= 0.20`
passes the diagnostic, `(0.20, 0.50]` is `INCONCLUSIVE_OVERFIT_RISK`, and
`PBO > 0.50` is `FAIL_BACKTEST_OVERFIT`. A genuinely single predeclared model is
`NOT_APPLICABLE` and receives no positive credit. PBO remains a diagnostic and
cannot substitute for the binding chronological WFA. Tried variants without a
retained matrix are `INVALID_MISSING_TRIAL_EVIDENCE`.

## 10. Five overlapping cohorts and costs

The economic diagnostic creates five concurrent daily cohorts. Each new cohort
receives one-fifth of the declared capital, enters at `D1` open, and exits at
`D5` close. The daily book is the sum of active cohorts. Turnover is calculated
from actual weight changes; each prediction is not treated as a fresh full-
capital portfolio.

Underlying cost sensitivity is `0`, `10`, `25`, and `50` basis points one-way.
Zero is diagnostic, 10 is optimistic sensitivity, 25 is the binding stress gate,
and 50 is extreme sensitivity. Costs apply to actual entries, exits, and weight
changes. Cost monotonicity is mandatory. Capacity and impact remain `UNKNOWN`
unless a notional and ADV participation contract is predeclared.

Before outer access, the charter must freeze within-cohort selection, weight
construction, gross and net exposure, long/short balance, stock/ETF allocation,
per-name and sector caps, cash treatment, abstention treatment, missing-exit and
delisting handling, turnover convention, and capacity/notional assumptions. Any
later change is a new counted trial.

Borrow availability and fees are excluded by user scope. This permits a gross-
of-borrow directional short diagnostic only; it cannot establish implementable
short economics. Option costs and returns are never estimated.

## 11. Baselines, controls, and four independent sleeves

Mandatory baselines are zero expected return/fold-local class base rates,
fold-local market-only forecast, simple trailing-return forecast, fold-local
security historical mean, and an equal-risk market/sector-neutral ranking
baseline.

Negative controls are whole-session circular date shift, within-date symbol-label
permutation, deterministic random feature, and future-adjustment/future-membership
canaries that must be rejected before fitting. No negative control may pass the
complete historical screen gate.

The intended trusted sleeves are exactly:

- `stock_long`
- `stock_short`
- `etf_long`
- `etf_short`

Each sleeve must independently satisfy data coverage, power, forecast, economic,
cost, uncertainty, and multiplicity gates. A failed or underpowered sleeve cannot
be hidden by a pooled long-short, stock/ETF, or aggregate result. Any sleeve
combination or weight selection is a new trial.

While historical PIT identity remains unresolved, proxy-assigned versions of
these sleeves are diagnostic only. They cannot satisfy a trusted stock/ETF sleeve
gate or remove `candidate_eligibility=BLOCKED_PENDING_PROSPECTIVE_PIT`.

## 12. Power and binding outcomes

The charter declares a minimum economically meaningful effect (`MEES`) and a
strictly larger design alternative before outer evaluation, separately for the
binding forecast loss and underlying-book return. Power uses training-only
centered date-level residuals at the design alternative, 5,000 stationary-block
samples at planned OOS length, and the exact final test. Required power is at
least 0.80 for each trusted sleeve; unresolved proxy sleeves cannot earn credit.

Outcomes are mutually exclusive and applied in this order:

- `INVALID`: lineage, PIT representation, leakage, timing, role, ledger, cost,
  sleeve, or evidence failure;
- `INCONCLUSIVE_PIT_IDENTITY`: membership or asset-type evidence cannot support
  the claimed sleeve;
- `FAIL_NO_EDGE`: the 95% confidence upper bound is at most zero;
- `FAIL_NOT_ECONOMIC`: after the prior rule, the 95% confidence upper bound is at
  most MEES;
- `FAIL_MULTIPLICITY_OR_CONTROL`: the effect bound passes but any adjusted test,
  DSR, applicable PBO policy, 25-bps cost, baseline, trusted sleeve, or control
  gate fails;
- `INCONCLUSIVE_DATA_OR_POWER`: any required trusted sleeve has power below 0.80
  or lacks sufficient dates, securities, folds, predictions, or outcome coverage;
- `INCONCLUSIVE_EFFECT`: the interval still intersects MEES;
- `INCONCLUSIVE_ROBUSTNESS`: required robustness evidence is valid but
  underpowered or unstable;
- `PASS_HISTORICAL_DISCOVERY_SCREEN`: the multiplicity-adjusted one-sided 95%
  lower bound exceeds MEES, adjusted p-value is at most 0.05, DSR is at least 0.95
  for the underlying book, applicable PBO policy is satisfied, 25-bps costs pass,
  and all trusted sleeves, baselines, and controls pass.

Definite failures take precedence over data, power, effect, or robustness
inconclusive outcomes.

Because PIT history is unresolved, the last outcome remains unavailable for a
trusted four-sleeve historical run. A diagnostic screen pass does not seal a
candidate; it only records a hypothesis that could later be considered for a
separately authorized prospective program. It is not an alpha claim.

## 13. Builder/evaluator/inference isolation

The splitter owns labels, information intervals, and folds. The builder receives
training features/labels and sequential unlabeled test features, then writes only
a frozen evaluation artifact and predictions. The evaluator reads frozen predictions and
test labels, cannot import training modules, and makes zero `.fit()` calls.

The registered mechanical entrypoint is
`us_stocks_swing_model_v2.research.executor:execute_synthetic_nested_wfa`. It is
synthetic-only and content-addresses the registration, exact inner/outer fit and
audit sample IDs, fold-local scaling plus ridge parameters, inner-only
hyperparameter scores, frozen outer predictions, and fit-free evaluations. Its
`linear_distribution_v1` output contains absolute expected five-session return
and up/down/neutral probabilities; it contains no rank-derived direction. Outer
labels are absent from the builder request type, and every outer prediction
artifact is frozen before the evaluator receives its matching labels.

Prediction manifests must predate label-unlock receipts. Evaluation reports bind
evaluation artifact, predictions, data release, charter, ledger head, code, and
environment hashes. They also bind the preregistered robustness-policy hash and
the exact per-sleeve robustness-evidence hash. Gate receipts, append-only
evaluation records, and later bundle candidates must agree on those hashes;
post-evaluation policy or evidence mutation is invalid. Valid temporal,
seed/parameter, or source-epoch instability propagates as
`INCONCLUSIVE_ROBUSTNESS` through its sleeve and the aggregate book, while a
definite failure retains precedence. The evaluator writes only to its unique run
directory. Final-holdout
access is one-time, append-only, and anchored to the frozen holdout specification
and pre-unlock ledger head. Repeated access is fatal.

Inference is separately fit-free. Its schema may contain underlying expected
five-session return, absolute probabilities, uncertainty, rank, abstention, and
release/bundle IDs only. Option-trade fields are invalid.

Prospective monitoring is a separate, non-authorizing lane. Its records are
append-only and head-anchored, bind the sealed bundle plus monitoring policy and
reference hashes, and contain no automatic retraining, retuning, source
substitution, resume, or promotion action. After a paused or invalid state, a
later healthy observation remains paused unless an exact signed recovery review
binds the previous record and corrected observation. Pending, paused, or invalid
monitoring cannot support manual decision readiness.

This is process isolation, not proof of independent human judgment. Genuine
independence requires a separate account or external evaluator, and prospective
confirmation remains required.

## 14. Readiness boundary

Harness readiness and evidence scope are independent. The registered executor's
synthetic-mechanics subgate is implemented and adversarially tested.
The historical-foundation bridge is synthetic-tested and has also built the
authorized real, non-active discovery release: a verified two-epoch HFDL set
and pinned accepted XNYS calendar produced six physically separate causal-bar,
feature-input, and outcome-input releases, with no epoch pooling. The bridge
preserves explicit denominators for missing sessions and unavailable
membership, identity, action, delisting, and outcome evidence. It emits price
inputs but no matured return, and it is not the active research sample schema
while those evidence fields remain unresolved.

Mechanical `HISTORICAL_RESEARCH_READY` is now bound to a clean committed
code/config/test closure by a verified non-authorizing readiness receipt. It
does not pretend the legacy-trial census is exact: that census remains an
indeterminate conservative floor and continues to block the trusted historical
gate. The mechanical milestone authorizes no real-history execution.

While PIT truth remains unresolved, the historical evidence scope remains
`LEGACY_DISCOVERY_ONLY_PIT_UNRESOLVED`, candidate eligibility remains
`BLOCKED_PENDING_PROSPECTIVE_PIT`, and alpha, options readiness, deployable short
readiness, and live execution remain false. This document alone marks nothing
ready and grants no execution authority.
