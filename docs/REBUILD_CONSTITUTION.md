# Rebuild Constitution

Version: `1.2.0`

This project evaluates daily US stock and ETF forecasts only after a completed
session. The fixed target is entry at the D1 regular-session open and exit at
the D5 regular-session close, using split-normalized price return. Options,
borrow assumptions, and live-trading claims are outside scope.

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

Nasdaq `nasdaqtraded.txt` is the contracted membership input; Alpaca assets
supplement identity. Complete accepted snapshots emit absence tombstones;
symbols never inherit eligibility after disappearance or reuse.

## Research gates

Historical and prospective outcomes may not be accessed for a real trial until
the hypothesis, releases, sleeves, costs, WFA schedule, and robustness policy
are preregistered in the external immutable registry. Training, evaluation,
candidate sealing, monitoring, and trading each require separate authorization.
