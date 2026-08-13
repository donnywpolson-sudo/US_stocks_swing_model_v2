# V1 Price/Volume Research-Data Scope

Status: frozen data-foundation scope. This document authorizes no outcome,
research, training, evaluation, backtest, deployment, broker, or trading work.
The content-addressed authority is
`config/v1_price_volume_research_scope.json`.

## Scope

V1 is end-of-day price-and-volume infrastructure for a point-in-time dynamic
universe of historically listed U.S. exchange-traded common-stock securities.
Information usable through completed regular session D0 may contribute to a
post-close signal. The earliest default execution opportunity remains the next
eligible session. Same-close execution remains denied.

V1 requires source-qualified stable security identity, historical ticker and
exchange/listing intervals, explicit historical security type, active and
inactive/delisted coverage, raw unadjusted daily OHLCV, effective corporate
actions, terminal-event representation, qualified sessions and timestamps,
causal structural-universe state, causal price/volume eligibility inputs, and
complete lineage.

## Security types

V1 includes only explicitly classified common-stock share classes. Share
classes remain distinct. Preferreds, ETFs, ETNs, mutual and closed-end funds,
warrants, rights, units, SPAC units, convertibles, and OTC securities are
excluded. ADR inclusion is unresolved and therefore defaults to excluded.
Unknown or ambiguous instruments are excluded with a reason code; ticker shape
is never classification evidence.

## Deliberately out of scope

Fundamentals and their vintages, earnings events and publication times,
analyst estimates and vintages, sector/industry classifications, shares,
market capitalization, index membership, news, alternative data, options,
borrow/locate data, and intraday signals are `OUT_OF_SCOPE_V1`. Current values
from any of those domains cannot substitute for historical point-in-time data.
Their interfaces may remain, but V1 has no dependency on them.

Final liquidity, price, minimum-history, and ADR policies remain later
preregistration decisions. The source layer exposes causal inputs and structural
state; it does not choose strategy thresholds.

## Authorization boundary

The scope contains no forward return, label, future price path, realized trade
outcome, performance measure, holdout, or real-outcome enablement. A passing
source gate would establish only data eligibility for another separately
authorized phase.
