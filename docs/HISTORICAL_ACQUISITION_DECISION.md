# Historical Acquisition Decision V1

## Decision

**NO_QUALIFIED_OPTION_FOUND — do not purchase historical data yet.**

The strongest research-integrity and lowest-complexity candidate is conditionally Morningstar/CRSP **CRSP 1962 US Stock**, Flat File Format 2.0, product code **C6Z**. It is not acquisition-ready. No lower-cost provider or multi-provider stack currently satisfies every mandatory V1 source requirement, and no ticker-only join is acceptable.

This decision is bound to the frozen V1 source contract, admission policy and acquisition requirements. It does not alter the existing `BLOCKED` historical-source readiness gate, legacy quarantine, canonical-panel admission threshold or outcome firewall.

## Conditional acquisition specification

If and only if every prerequisite below is resolved through a later authorized provider-confirmation and purchase phase, acquire:

- Provider: Morningstar / Center for Research in Security Prices.
- Product: CRSP 1962 US Stock, CIZ Flat File Format 2.0, code `C6Z`.
- Coverage: 2016-01-04 through the latest completed session at the future approved acquisition.
- Venues: NYSE, Nasdaq and NYSE American.
- Securities: every historically listed common-stock share class, including active, inactive, delisted, failed, acquired and bankrupt histories; no current-survivor backfill.
- Historical releases: every licensed as-received monthly or quarterly release, manifest, schema and release note from January 2016 onward—not only the latest corrected database.
- Reference tables: `StkSecurityInfoHist`, `StkIssuerInfoHist`, historical security/type/exchange metadata and exchange calendars.
- Bars: `StkDlySecurityData`, including PERMNO, session date, original unadjusted regular-session open/high/low/close, raw share volume, flags, release and file lineage.
- Corporate actions: `StkDistributions` and any additional required C6Z action table covering splits, reverse splits, dividends, rights, spin-offs, symbol/share-class changes, mergers, acquisitions, consideration and successors.
- Terminal events: `StkDelists`, including dates, reason/status/action/payment types, terms, last-trade references and successor identifiers.
- Delivery: portable CIZ flat files through the vendor-approved secure delivery channel, expected to be MOVEit or an equivalent licensed channel; monthly incremental as-received releases.
- Authentication: vendor-issued secure-file-transfer credentials handled only through the project’s later approved secret workflow.
- Storage: quote required. An official legacy matrix has listed approximately 3.3 GB for one 1962 US Stock release; that is not an estimate for the required multi-vintage corpus.

Vendor-provided return fields, including daily or delisting returns, may be retained only if unavoidable in immutable raw bytes and must remain unparsed and inaccessible behind the outcome firewall. They are not required for V1 and must never be used during source admission.

## Mandatory confirmation questions

No inquiry was sent. A later authorized phase must obtain exact written answers before purchase:

1. Can Morningstar/CRSP license every as-received monthly or quarterly C6Z release, schema, manifest and release note from January 2016 onward, rather than only the latest corrected history?
2. Do those releases expose immutable publication/process timestamps and enough record-level revision metadata to reconstruct which identity, distribution and delisting records were usable at every historical cutoff?
3. For all V1 common-stock share classes from 2016-01-04 onward, what are the coverage and missingness for `DlyOpen`, `DlyHigh`, `DlyLow`, `DlyClose` and `DlyVol`; are those original unadjusted regular-session observations; and when is the daily release usable?
4. Does C6Z cover every required action and terminal-event type with stable source-event identity, terms, predecessor/successor PERMNOs and revision chronology?
5. Is an individual or noninstitutional subscriber eligible, and does the agreement permit automated local quantitative research, immutable landing/backups and internal derived point-in-time panels?
6. May source releases, manifests, hashes, backups and internal derived panels be retained after subscription expiration, and are additional exchange agreements or fees required?
7. What is the itemized base, archive, setup, exchange and recurring price; minimum term; exact delivery entitlement; update cadence; and authentication category?
8. Which current Tiingo entitlement enables permaTicker for EOD history, what entity does it identify, and does it expose complete historical ticker and exchange validity intervals for every active, delisted and recycled V1 common-stock security?

The required answer format and affected source-contract requirement IDs are machine-readable in `config/historical_acquisition_decision_v1.json`. Candidate-specific questions for Norgate and Sharadar, plus Tiingo questions beyond its unresolved permanent-ID entitlement, are preserved in each failed evaluation cell and were not sent.

## Cost

| Option | Public price | Exact V1 first-year cost | Decision |
|---|---:|---:|---|
| CRSP C6Z plus archived releases | Quote required | Unresolved | Conditional technical finalist; no purchase |
| Norgate US Stocks Platinum | USD 346.50 / 6 months; USD 630 / 12 months | USD 630 | Invalid despite transparent cost |
| Sharadar direct | Advertised from USD 9/month; bundle USD 29/month | Unresolved exact tier | Invalid |
| Tiingo EOD | Individual USD 300/year; internal commercial USD 499/year | USD 300 or USD 499 for EOD only; action add-on unresolved | Invalid |

Publicly priced alternatives are cheaper but fail noncompensatory mandatory gates. CRSP’s prestige does not establish fitness; only the exact product, archived-release entitlement and contract answers can do that.

## Do not buy for V1

- CRSP10: rolling monthly educational product, not the required daily source.
- CRSP indexes, CRSP/Compustat, fundamentals, earnings, analysts, sectors, industries, shares, market cap or index constituents: bundled or optional future value only.
- Norgate Platinum, Sharadar direct or Tiingo EOD/actions: documented mandatory failures remain.
- Intraday data, news, alternatives, options, borrow or locate data: `OUT_OF_SCOPE_V1`.

## Subsequent authorized phases

The immediate next phase is not ingestion. It is a narrowly authorized provider-confirmation/quote phase that asks only the seven recorded questions, reviews the proposed contract and quote without accepting terms, and returns either a purchase-ready exact entitlement or a continued blocker.

Only after a separate purchase authorization and completed acquisition may another separately authorized ingestion phase execute the 20-step sequence in `config/historical_acquisition_plan_v1.json`: immutable landing; manifest; hashing; schema and license capture; provider-ID preservation; stable identity; ticker and exchange intervals; type mapping; raw OHLCV, action and terminal admission; calendar binding; reconciliation; full-corpus validation; quarantine; canonical panel construction; real-source causal invariance; and readiness-gate rerun.

Even a passing source-readiness gate would not authorize real forward labels, outcome access, model training, feature-performance testing, evaluation, backtesting, holdout access, production deployment, broker connectivity or trading.
