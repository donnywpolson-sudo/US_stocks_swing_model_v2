# Historical Source Contract V1

The machine-readable contract is
`config/historical_source_contract_v1.json`; admission policy is
`config/historical_source_admission_policy_v1.json`. Both are
content-addressed and default deny.

## Mandatory package families

Every source-qualified build requires all five package families:

1. historical security master;
2. raw unadjusted daily OHLCV;
3. corporate actions;
4. delisting and terminal events; and
5. exchange calendar.

One qualified component cannot compensate for a missing family. Material
quarantine cannot be hidden by shrinking the universe, especially when it
affects failed, acquired, inactive, or delisted securities.

Each package identifies its provider and dataset/version, retrieval time,
coverage, security scope, identifiers, adjustment state, timezone and timestamp
meaning, revision policy, license classification, storage location, immutable
file manifest and hashes, schema hash, ingestion version, and limitations.
Unknown adjustment, timestamp, stable-identity, ticker-validity, delisting,
revision, license, or lineage semantics block admission.

## Identity and survivorship

Ticker is a time-varying attribute, never permanent identity. Historical rows
bind a stable tradable-security ID, vendor instrument ID, ticker, MIC, security
type, listing state, effective interval, publication/receipt/usable times,
revision, and source-row hash. Nonoverlapping ticker reuse may map to different
stable securities. Overlapping venue/ticker assignments or overlapping
identity intervals are ambiguous and cannot enter the panel. Predecessor and
successor securities remain distinct histories.

Inactive, delisted, failed, acquired, and bankrupt histories stay in the source
denominator. Terminal metadata may be retained, but this phase never computes a
delisting return, terminal holding-period return, acquisition payoff, or
bankruptcy loss and never assumes the last close is a terminal outcome.

## Raw bars and actions

Canonical bars must be raw/unadjusted and keyed by stable security plus session.
Full-corpus checks cover OHLC, volume, duplicates, session validity, historical
ticker/exchange validity, availability, lineage, current-state joins, synthetic
or interpolated observations, forward fill, silent drops, and unresolved rows.
Rows are classified rather than repaired. A missing observation remains
missing.

Corporate actions remain event-level evidence separate from raw bars. Complete
effective-event scope, historical publication/usable times, revision history,
stable identity, and exact lineage are required. Price or volume gaps are
diagnostics only and cannot create an event. A fully adjusted convenience
series is never canonical raw evidence.

## Admission gateway

`historical_source_admission.py` implements deterministic descriptors,
full-corpus validation receipts, admission results, historical-identity
interval and ticker-reuse checks, raw-bar classification, structural-universe
inputs, and mandatory-family bundle validation. Results and bundles are
content-addressed. Repeating unchanged inputs produces the same status, counts,
reason codes, and IDs.

Only a clean `EXTERNAL_AS_RECEIVED` package with documented local-research
permission, every required semantic, verified files/schema/counts, zero
unresolved rows, and full-corpus validation can be `ADMITTED`. Legacy and
current-state packages are quarantined. Synthetic packages remain
`SYNTHETIC_TEST_ONLY`. Adjusted-only and semantically incomplete packages are
blocked.

The outcome firewall's source-qualified resolver requires the exact admission
record and binds access to its declared storage location. Canonical raw-bar
loading separately requires a clean raw-OHLCV admission with matching source
identity. General interface or fixture success does not authenticate a real
provider package.

## Current acquisition boundary

No configured local package satisfies the five-family contract. The exact
vendor-neutral missing package specification is
`config/historical_source_acquisition_requirements_v1.json`. That specification
does not authorize network access, bulk download, purchase, account creation,
new terms, credential access, publication, or activation.
