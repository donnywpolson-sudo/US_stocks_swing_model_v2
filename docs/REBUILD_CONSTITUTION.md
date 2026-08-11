# Rebuild Constitution

Version: `1.3.0`

This project evaluates daily US stock and ETF forecasts only after a completed
session. The strict reference target is entry at the D1 regular-session open
and exit at the D5 regular-session close, using split-normalized price return.
Options and live-trading claims are outside scope.

The opt-in `ALPACA_FREE_BOUNDED_V1` profile extends the same session timing to
explicit long and short gross economic outcomes. It fixes stock-borrow and
locate costs at zero, labels all historical shortability unverified, permits
only prospectively observed easy-to-borrow shorts, models distribution and
corporate-action obligations, and retains unresolved observations and stress
scenarios. It does not change the frozen strict reference contract.

## Evidence boundary

Synthetic tests prove mechanics only. The retained Alpaca historical archive
and its proxy releases are immutable caveated discovery evidence with
`legacy_universe_selection_unresolved`. They are not point-in-time membership
evidence, a complete-market universe, training evidence, evaluation evidence,
or a claim of alpha. They preserve their existing manifests unchanged.

Trusted evidence requires separately published prospective identity, calendar,
raw SIP bars, corporate-action, delisting, eligible-universe, feature, and
mature-outcome releases. Missing membership, asset type, actions, delisting,
or D5 maturity causes abstention rather than imputation or row deletion.

## Active source contract

Alpaca SIP is the sole qualified bar lane. It remains non-active pending the
separately authorized prospective smoke capture and later activation gate. Every candidate request
uses `feed=sip`, `timeframe=1Day`, `adjustment=raw`, ascending order, no
`asof`, and an end time at least 20 minutes before the request time. Raw bytes,
safe headers, receipt time, request parameters, pagination lineage, hashes,
and identity binding are retained. A source or methodology change creates a new
epoch.

Nasdaq `nasdaqtraded.txt` remains the strict contract's membership input;
Alpaca assets supplement identity. Under `ALPACA_FREE_BOUNDED_V1` only,
prospectively captured `nasdaqlisted.txt` and `otherlisted.txt` supplement the
complete Alpaca asset master, while dated Alpha Vantage `LISTING_STATUS` is a
historical-universe candidate pending live semantics validation. Complete
accepted snapshots emit absence tombstones; symbols never inherit eligibility
after disappearance or reuse.

## Research gates

Historical and prospective outcomes may not be accessed for a real trial until
the hypothesis, releases, sleeves, costs, WFA schedule, and robustness policy
are recorded in a canonical trial file committed to this repository and backed
up to the configured GitHub branch. This is an owner-controlled audit trail,
not an independently immutable service. Training, evaluation, candidate
sealing, monitoring, and trading each require separate authorization.
