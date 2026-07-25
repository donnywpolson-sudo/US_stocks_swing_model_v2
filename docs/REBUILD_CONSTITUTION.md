# Rebuild Constitution

Version: `1.0.0`

Project: `US_stocks_swing_model_v2`

Scope: US-listed stocks and ETFs; underlying forecasts only

This document is the binding design contract. Tests and machine-readable
configuration enforce it. A later change requires a versioned amendment and,
after any real-history evaluation, a new registered trial.

## 1. Claims and non-claims

The project may eventually estimate the distribution of the split-normalized
underlying price return from the next regular-session open through the fifth
subsequent pinned exchange-session close. It may emit expected return,
up/down/neutral probabilities, uncertainty, rank, and abstention.

It does not claim:

- option profitability, strike/expiry selection, implied-volatility edge, or
  execution quality;
- net short profitability when borrow availability and borrow cost are absent;
- point-in-time historical universe truth from retrospective downloads;
- alpha from a smoke test, a single WFA, or historical discovery alone;
- that stocks and ETFs, or long and short sleeves, share performance.

Every eligible underlying prediction is scored regardless of whether the owner
places a discretionary trade.

## 2. Evidence classes

1. `SYNTHETIC_MECHANICAL`: fixtures proving code, causal isolation, accounting,
   recovery, and abstention. It is not an alpha trial.
2. `LEGACY_DISCOVERY`: existing HFDL and all previously inspected historical
   evidence. It may falsify or develop; it cannot confirm.
3. `REGISTERED_HISTORICAL_DISCOVERY`: a predeclared, counted trial executed by
   the evaluation role after separate authorization.
4. `PROSPECTIVE_AS_RECEIVED`: immutable source vintages collected after a
   candidate and scoring protocol are sealed.
5. `PROSPECTIVE_FINAL`: later corrected truth used only to mature outcomes. It
   never rewrites the as-received state that produced a prediction.

No repository move, new name, source substitution, or unused date range resets
prior knowledge. The global trial registry conservatively imports legacy trial
families before historical research begins.

## 3. Source constitution

### 3.1 Bars

Alpaca Basic is the only candidate active OHLCV lane. A bounded entitlement
qualification must request SIP and IEX separately and record the response for
each; neither feed is assumed. Only the feed named by an accepted qualification
receipt may later become active. Every request must explicitly use:

```text
feed=<qualified sip or iex>
timeframe=1Day
adjustment=raw
asof omitted entirely (or a separately reviewed ISO mapping date)
sort=asc
end <= request_time - 20 minutes
```

Raw provider bytes, request parameters excluding secrets, request time,
response headers, pagination lineage, response hash, requested symbol, returned
symbol, and asset identity are preserved. A change to feed, adjustment,
mapping, endpoint behavior, or methodology starts a new source epoch. Feeds are
never pooled.

### 3.2 Identity and membership

Daily as-received comprehensive Nasdaq Trader `nasdaqtraded.txt` snapshots
provide prospective ETF flags, listing state, test-issue state, and financial-
status evidence. Raw bytes, HTTP headers, retrieval time, and receipt are
atomically landed before parsing; the embedded Eastern file-creation time is
recorded and cannot be later than retrieval. Narrower directory files are
disabled. Alpaca asset UUID/state is supplemental. A record is eligible only
when the security type is explicitly `STOCK` or `ETF`; nonstandard and
ambiguous records are `UNKNOWN` and must abstain. Each accepted input is
explicitly a complete membership snapshot. Disappearance creates an absence
tombstone, symbol reuse does not inherit an old asset's eligibility, and same-
effective-time revisions are visible only after their later knowledge time.
Retrospective files do not establish historical membership.

### 3.3 Corporate actions

Actions are bitemporal: effective session and provider-received time are both
mandatory. Revisions append; they never overwrite. The as-received view drives
features/eligibility. A separately versioned final view may score matured
outcomes. Split-normalized price return includes split ratios; unresolved
mergers, spinoffs, conversions, or delistings yield an unresolved outcome
status rather than row deletion.

### 3.4 Legacy sources

- HFDL is `legacy_discovery_only`, split into the documented pre-2022-03-04
  PiTrading-consolidated and post-2022-03-04 IEX-only epochs.
- The existing 780-symbol Alpaca SIP capsule is failed qualification evidence
  only (coverage/gap/identity/survivorship blockers). Native pages, checkpoints,
  snapshots, and audits may migrate; derived Parquet must be regenerated.
- Alpha Vantage and options data are excluded.

## 4. Immutable releases

Sources land in staging. Validation must complete before atomic promotion to a
content-addressed release. Accepted releases cannot be overwritten. Corrections
create successors.

Every release records:

- logical dataset and source epoch;
- event/session bounds and available/retrieved timestamps;
- file paths, sizes, SHA-256 hashes, row count, key/schema fingerprint;
- upstream release IDs;
- code, configuration, dependency, and environment hashes;
- collision, lock, recovery, and validation state.

No accepted input is found through recursive search or a legacy fallback. Only
an exact manifest path is permitted. Symlinks, junctions, hardlinks, duplicate
keys, partial publication, and mutation fail closed.

## 5. Causal data contracts

Every row distinguishes the market session/event time from `available_at` and
`retrieved_at`. A row can affect a decision only when it was available by the
decision cutoff. Source corrections never rewrite an earlier as-of view.

The exchange calendar is a pinned immutable release. Labels are not made with
ticker-row shifts. For a decision on session `D0`:

- entry is the regular-session open on pinned session `D1`;
- exit is the close on pinned session `D5`;
- raw prices are normalized only for splits effective in `(D1, D5]`;
- dividends are excluded from the price-return target;
- missing, halted, delisted, and action-ambiguous paths retain an explicit
  status.

Feature releases contain no outcome, label, future, or evaluation field.
Outcome releases contain no model prediction. Prediction releases contain no
realized outcome.

## 6. Research firewall

The builder can access source, canonical, feature, and synthetic fixture data.
It cannot read sealed evaluation results while changing a registered candidate.

The evaluator accepts only a preregistered immutable trial specification. It
records the full data/config/code/bundle identity and an append-only result.
Any semantic change after evaluation is a new trial; no result-driven repair is
free.

The operator loads only a sealed bundle and exact allowed feature, identity,
calendar, source-epoch, and security-type evidence. It derives the actual
current recording time internally from a repository-issued production UTC
clock. A caller may supply fixed time only under an explicit synthetic permit,
which is mechanically testable but never trust eligible. Bounded-latency predictions are externally anchored
before entry and the information barrier. It cannot train, call `fit`, access
labels/outcomes/WFA reports, substitute sources, or change thresholds.

Historical research, when separately authorized, requires nested chronological
tuning, label-interval purging, fold-local transformations, date/session-level
uncertainty, registered multiple-testing adjustment, costs, simple baselines,
negative controls, minimum power, and binding inconclusive states. The
robustness policy hash is frozen in the trial and permit. Exact per-sleeve
robustness evidence is hashed into the gate receipt, append-only evaluation
record, and any later bundle candidate, so changing policy or evidence after
evaluation fails closed.

## 7. Independent gates

The primary gate is frozen before evaluation. Secondary metrics cannot rescue
it. At minimum these sleeves are separately gated:

- `stock_long`
- `stock_short`
- `etf_long`
- `etf_short`

An absent or underpowered sleeve is `INCONCLUSIVE`, not a pass. Aggregate
performance cannot hide a failed sleeve. Short results are gross of excluded
borrow costs and cannot claim deployable net-short economics.

Temporal concentration, registered seed/parameter stability, or source-epoch
instability in otherwise valid evidence is
`INCONCLUSIVE_ROBUSTNESS` at both sleeve and book level. A definite statistical,
economic, control, or robustness failure remains `FAIL`; it cannot be softened
to inconclusive merely because another gate is underpowered or unstable.

## 8. Sealed bundle and inference

A sealed bundle pins feature names/types/order, transformations, estimator,
calibration, thresholds, training cutoff, source/data/calendar releases,
environment, code, and all artifact hashes. Serialization reload parity is an
acceptance test.

Inference is fit-free. It emits only:

- underlying asset/symbol and decision time;
- expected five-session price return;
- `p_up`, `p_down`, `p_neutral`;
- uncertainty, rank, and abstention/reason;
- bundle and input release identities.

Option, strike, expiry, premium, Greeks, and proposed-trade fields are schema
violations. Stale, missing, wrong-epoch, unknown-type, schema-mismatched, or
untrusted input must abstain.

## 9. Prospective confirmation

Before outcomes accrue, seal the candidate, eligible universe rules, collection
cutoff, prediction cutoff, scoring rule, effect threshold, effective-sample or
end-date rule, and early-stop policy. Predictions are hash-chained, externally
head-anchored, and written before entry or outcomes mature. Aggregate
prospective performance remains blinded until
the fixed end rule. Missed as-received vintages cannot be backfilled and called
prospective.

Monitoring may pause or abstain for stale data, coverage, drift, provider
failure, clock error, or bundle age. Each decision is appended to a hash chain,
bound to the exact bundle, policy, reference, observation, and predecessor, and
retained under a separate local head anchor. A paused or invalid predecessor
cannot resume without an exact signed recovery review binding the corrected
observation. Monitoring cannot silently retrain, retune, change sources, extend
confirmation, resume, or promote a challenger; its automatic-action list must
remain empty.

## 10. Completion gates

`REBUILD_COMPLETE` requires all architecture, isolation, source, temporal,
publication, recovery, clean-room reproducibility, and adversarial tests to
pass with no unresolved P0/P1 defect.

`HISTORICAL_RESEARCH_READY` additionally requires a registered, deterministic
research harness and conservative legacy trial census, but asserts no alpha.

Afterward, hypothesis failures retire candidates. Broad architecture audits
resume only for a named gate failure, provider-contract change, or concrete new
defect.
