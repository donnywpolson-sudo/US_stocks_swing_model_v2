# Historical Provider Evaluation V1

## Status and boundary

**NO_QUALIFIED_OPTION_FOUND** as of August 13, 2026. This is a primary-source evaluation and acquisition-planning record. It does not authorize an account, inquiry, quote request, trial, purchase, subscription, API use, download, ingestion, canonical-panel build, outcome access, research, training, evaluation, backtest, deployment, broker connection, or trading.

The frozen V1 scope remains end-of-day raw price and volume for a dynamic point-in-time universe of historically listed US exchange-traded common-stock share classes. It requires stable security identity; historical ticker, exchange, listing and security type; active, inactive and delisted coverage; original unadjusted daily OHLCV; corporate actions; terminal events; qualified session and availability semantics; historical revisions; full lineage; and a compatible local-research license. Fundamentals, earnings, analysts, sectors, industries, shares, market cap, index membership, news, options, borrow and intraday data remain `OUT_OF_SCOPE_V1`.

## Method

The evaluation re-read the frozen source contract and admission policy before comparing products. Each mandatory requirement was adjudicated independently from price or convenience. A pass requires current product-specific official evidence. A marketing claim such as “survivorship-bias-free” is not sufficient without the supporting identity, coverage, event and revision fields. Failures and unresolved mandatory questions are noncompensatory: a cheaper API cannot offset a point-in-time identity, survivorship, raw-data, vintage or license failure.

The evidence registry contains 32 current official pages or documents retrieved on August 13, 2026 (Pacific time). No provider was contacted, no account was created, no commercial data was requested, and no paid or material API call was made.

## Mandatory comparison

| Requirement | CRSP C6Z | Norgate Platinum | Sharadar direct | Tiingo EOD |
|---|---|---|---|---|
| Stable security ID | PASS_PRIMARY_EVIDENCE — PERMNO | PASS_PRIMARY_EVIDENCE — AssetID | PASS_WITH_LIMITATION — issuer-scoped permaticker | UNRESOLVED — account-gated permaTicker conflicts with incomplete public mapping docs |
| Historical ticker | PASS_PRIMARY_EVIDENCE — interval tables | FAIL — final-ticker rewiring | PASS_WITH_LIMITATION — action reconstruction | FAIL — recycled ticker gap |
| Exchange/listing history | PASS_PRIMARY_EVIDENCE | PASS_WITH_LIMITATION — major-exchange flag | FAIL — latest exchange only | FAIL — current metadata |
| Security type | PASS_PRIMARY_EVIDENCE | PASS_PRIMARY_EVIDENCE | PASS_WITH_LIMITATION | PASS_WITH_LIMITATION |
| Listing/inactive history | PASS_PRIMARY_EVIDENCE | PASS_WITH_LIMITATION | PASS_WITH_LIMITATION | FAIL |
| Raw daily OHLCV | PASS_WITH_LIMITATION — exact V1 population unproved | PASS_WITH_LIMITATION — export semantics incomplete | FAIL — full OHLCV is split-adjusted | PASS_PRIMARY_EVIDENCE |
| Active securities | PASS_PRIMARY_EVIDENCE | PASS_PRIMARY_EVIDENCE | PASS_PRIMARY_EVIDENCE | PASS_PRIMARY_EVIDENCE |
| Inactive/delisted | PASS_PRIMARY_EVIDENCE | PASS_WITH_LIMITATION — completeness disclaimed | PASS_WITH_LIMITATION | FAIL — recycled tickers excluded |
| Corporate actions | PASS_WITH_LIMITATION — knowledge times/vintages unproved | FAIL | PASS_WITH_LIMITATION | FAIL — partial early release |
| Terminal events | PASS_WITH_LIMITATION — knowledge times/vintages unproved | FAIL | PASS_WITH_LIMITATION | FAIL |
| Session/timestamp semantics | PASS_WITH_LIMITATION | PASS_WITH_LIMITATION | PASS_WITH_LIMITATION | PASS_PRIMARY_EVIDENCE |
| Historical revisions | UNRESOLVED — archived releases not publicly promised | FAIL | FAIL | FAIL |
| Causal availability | UNRESOLVED — publication/received/usable chronology incomplete | FAIL | FAIL | FAIL |
| Full lineage | PASS_PRIMARY_EVIDENCE conditional on release entitlement | PASS_WITH_LIMITATION | PASS_WITH_LIMITATION | PASS_WITH_LIMITATION |
| Local research license | UNRESOLVED — subscriber agreement and quote required | FAIL — deletion after lapse | FAIL — deletion after lapse | FAIL — deletion/derived-use restrictions |

The detailed assessments, primary-evidence IDs and exact vendor questions are in `config/historical_provider_evaluation_v1.json`; the machine-readable table is in `config/historical_provider_comparison_v1.json`.

## CRSP / Morningstar

CRSP is the strongest single-source technical fit. The [current product page](https://indexes.morningstar.com/research-data-products/crsp-us-stock-databases) documents more than 36,000 active and inactive securities, daily and monthly data, corporate actions and permanent security identity. The [PERMNO page](https://indexes.morningstar.com/research-data-products/permno) distinguishes permanent security-level PERMNO from company-level PERMCO. The [CIZ database guide](https://www.crsp.org/wp-content/uploads/guides/CRSP_US_Stock_%26_Indexes_Database_Guide_Flat_File_Format_2.0.pdf) defines regular-session daily open, high, low and close, raw share volume, historical security information, distributions and detailed delistings. The [SIZ-to-CIZ guide](https://www.crsp.org/crsp_pdf/crsp-us-stock-indexes-databases-siz-to-ciz-cross-reference-guide/) maps the smallest relevant current product, CRSP 1962 US Stock, to code `C6Z`.

The product is not purchase-ready. The [metadata guide](https://www.crsp.org/crsp_pdf/crsp-us-stock-indexes-databases-metadata-guide-flat-file-format-2-0/) reports incomplete population of some price fields over the full history, so exact 2016-plus common-stock coverage must be confirmed. Monthly and annual [release notes](https://www.crsp.org/crsp_pdf/crsp-us-stock-database-release-notes-2025-12-annual/) document later corrections to historical names, identifiers, distributions and delistings. That proves latest corrected history is not an as-of vintage and makes the archived release corpus mandatory. Public materials do not promise access to all as-received releases from 2016 onward or supply complete publication/received/usable-time semantics for every source family.

CRSP supports Windows-compatible flat files and programming workflows, but [access](https://indexes.morningstar.com/research-data-products) is subscription-based and public [policies](https://www.crsp.org/policies-statements/) defer actual rights to a subscriber agreement. Individual eligibility, automated local quantitative research, immutable backups, derived panels, post-cancellation retention, exchange obligations and price are unresolved. `CRSP10` is not a substitute: its [official guide](https://www.crsp.org/wp-content/uploads/guides/CRSP10_Year_US_Stock_Database_Guide.pdf) describes a rolling monthly classroom/workshop database, not the required daily source.

## Norgate Data

Norgate is the easiest Windows-native low-cost candidate but does not meet V1. Its [content tables](https://norgatedata.com/data-content-tables.php) document current and delisted US stocks and historical major-exchange indicators, but explicitly make no completeness claim for the delisted database. The [FAQ](https://norgatedata.com/data-package-faq.php) says delisted histories use the final ticker and name without prior ticker/name history. Although [AssetID](https://norgatedata.com/amibroker-usage.php) is stable, the missing historical ticker intervals prevent complete ticker-reuse reconstruction. Published data expose only partial corporate-action and terminal-event semantics and no historical information vintages.

The minimum relevant Platinum package is publicly priced at [USD 346.50 for six months or USD 630 for twelve months](https://norgatedata.com/stockmarketpackages.php). Price does not repair the contract failures. The [EULA](https://norgatedata.com/subscribe/eula.php) requires source and derived data to be deleted after subscription lapse and makes the proprietary database inaccessible, which conflicts with durable source landing and reproducibility.

## Sharadar direct

Sharadar’s current direct product is operationally attractive, with REST and table downloads and [more than 25,000 active and delisted instruments](https://sharadar.com/prices). It cannot satisfy the frozen raw layer: the [stock schema](https://sharadar.com/docs/stocks) defines open, high, low, close and volume as split-adjusted and provides only unadjusted close. Reconstructing full raw OHLCV would violate the admission contract.

The [ticker schema](https://sharadar.com/docs/tickers) defines permaticker at issuer scope, not as a permanent tradable-security/share-class ID. The [actions table](https://sharadar.com/docs/actions) covers several event types but lacks historical knowledge times and revision chronology. Most decisively, the [FAQ](https://sharadar.com/docs/faqs) states historical primary listing venue is unavailable and exchange is latest-only. Public direct pricing starts at USD 9 monthly and advertises a USD 29 bundle, but the exact complete tier is unresolved. The [terms](https://sharadar.com/terms) require deletion of source and derived data within thirty days after termination and restrict the service to personal/nonprofessional use.

## Tiingo

Tiingo has the clearest inexpensive raw-bar API. The [EOD documentation](https://www.tiingo.com/documentation/end-of-day) supplies raw and adjusted OHLCV and documents typical US publication around 5:30 p.m. Eastern with corrections later that evening. Published EOD pricing is [USD 30 monthly or USD 300 annually for individuals and USD 50 monthly or USD 499 annually for internal commercial use](https://www.tiingo.com/about/pricing).

It fails the full source stack. Official [symbology documentation](https://www.tiingo.com/documentation/appendix/symbology) says permanent ticker and delisted support are still expanding and limits delisted support to tickers not yet recycled; the [search API](https://www.tiingo.com/documentation/utilities/search) calls permaTicker an upcoming placeholder. A current [changelog](https://www.tiingo.com/documentation/general/changelog) also says EOD history can be queried by permaTicker for accounts that have it enabled. Because the entitlement, identifier scope, mapping history, price and completeness are not public, stable identity is recorded as `UNRESOLVED`, not as a pass. Early-release [split](https://www.tiingo.com/documentation/corporate-actions/splits) and [dividend](https://www.tiingo.com/documentation/corporate-actions/dividends) endpoints are not a complete action or terminal-event source. Historical exchange intervals, full terminal metadata and information vintages are not documented. The [terms](https://api.tiingo.com/tos/) limit source retention to an active subscription and require express written approval for derived-data use.

## Provider combinations

No valid combination was found. CRSP reference data plus Tiingo bars has no documented shared stable security identifier; a ticker-and-date join is prohibited and would not repair CRSP’s unresolved historical vintages or licensing. Norgate AssetID has no documented bridge to Tiingo. Sharadar permaticker is issuer-level, not a shared tradable-security identifier. The cheaper products also retain deletion or derived-use restrictions, so combining them increases joins and license complexity without clearing mandatory gates.

No additional provider was promoted to the serious-candidate matrix. Publicly discoverable institutional reference or corporate-action specialists would add a sales-only product and an unproved cross-provider bridge; none provided current public evidence that it could beat the conditional single-source CRSP design on the frozen V1 requirements.

## Conclusion

CRSP C6Z is the conditional technical finalist and lowest-complexity design. It is not a recommendation to buy. Until archived releases, knowledge-time semantics, raw-field coverage, event completeness, license, eligibility, retention and price are confirmed in writing, Option D — no safe acquisition yet — remains selected.
