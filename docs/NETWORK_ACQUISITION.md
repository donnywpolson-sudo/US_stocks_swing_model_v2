# Owner-Operated Network Acquisition

This personal project uses an owner-operated local acquisition mode. It does
not use an external signer, public-key registry, or detached authorization
receipt for provider downloads.

## Safety boundary

Free-source qualification is plan-only by default. A real request requires both
of these deliberate inputs in the same invocation:

```powershell
$env:FREE_SOURCE_QUALIFICATION_APPROVED='YES'
python -m us_stocks_swing_model_v2.cli.qualify_free_sources `
  --execute-network `
  --nasdaq-only
```

The flag without the environment confirmation fails closed. The environment
confirmation without the flag remains plan-only.

Before transport, the CLI builds and validates an exact request plan against
`config/network_acquisition_registry.json`. A process-local session binds:

- source and exact initial HTTPS URL;
- checked network-registry identity;
- GET method;
- timeout and response-byte limit;
- page limit and pagination policy; and
- ordered, single-use request attempts.

The session exists only in memory. It is not transferable between processes and
cannot be reconstructed from a file. Preflight spends the ordered page attempt
before transport begins. A transport, response-binding, landing, or interruption
failure therefore spends that attempt; same-session retry is intentionally
rejected. Retry requires a new explicit invocation and a new local session.
This favors fail-closed replay and ordering safety over in-session availability.

## Bounded Alpaca SIP-versus-IEX qualification

The Alpaca bars planner emits separate content-addressed SIP and IEX request
plans. Execution must supply both exact reviewed plan IDs; a missing or
different ID fails before snapshot-store or network-session creation. The
page bound is explicit and belongs to both plans:

```powershell
python -m us_stocks_swing_model_v2.cli.qualify_free_sources `
  --plan-only `
  --alpaca-only `
  --symbols AAPL,SPY `
  --start 2026-07-23T04:00:00Z `
  --end 2026-07-30T03:59:59Z `
  --max-pages 1
```

The matching separately authorized execution uses the same arguments plus
`--execute-network`, `--approved-sip-plan-id`, and
`--approved-iex-plan-id`. A one-page plan stops if the first landed response
contains a continuation token; it never issues an undeclared second request.
Credentials remain process-environment inputs and never enter a plan, URL,
snapshot, or output.

After both snapshots land, the pair can be reassessed offline without writes:

```powershell
python -m us_stocks_swing_model_v2.cli.qualify_free_sources `
  --verify-alpaca-pair <sip-snapshot-directory> <iex-snapshot-directory> `
  --symbols AAPL,SPY `
  --start 2026-07-23T04:00:00Z `
  --end 2026-07-30T03:59:59Z
```

The assessor reloads the immutable network snapshots, applies the pinned
calendar and strict per-feed bar checks, and emits a content-addressed no-write
assessment. If both feeds pass, SIP is the candidate; otherwise the sole
passing feed is the candidate. No pass, early stop, or evidence mismatch
selects no feed. A candidate is not an accepted qualification receipt and does
not authorize activation, configuration changes, canonical bars, or research.

The completed five-session assessment selected SIP under the frozen both-pass
tie rule. `config/alpaca_feed_qualification_policy.json` binds that assessment,
both immutable snapshots, the registry, calendar, request contract, prospective
non-active accepted receipt, and later source-cutover contract.

The local design CLI is no-write and has no execute mode:

```powershell
python -m us_stocks_swing_model_v2.cli.prepare_alpaca_source_cutover
```

It revalidates the complete evidence and emits a content-addressed design. The
design grants no receipt-publication, activation, canonical-bars, provider,
credential, research, or audit authority. Receipt publication must create one
non-active accepted release first. Only a later separately authorized cutover
may set `qualified_feed` to `sip`, name that verified receipt, and enable the
source. Canonical bars and research remain later gates.

The dedicated receipt publisher is also no-write by default:

```powershell
python -m us_stocks_swing_model_v2.cli.publish_alpaca_qualification
```

Planning revalidates the clean committed repository, exact policy and pair
assessment, both immutable captures, registry, calendar, inactive source
configuration, environment, code/config closures, and accepted/work roots. It
emits one content-addressed publication plan. Execution remains a separate
gate and requires `--execute`, that exact approved plan ID,
`ALPACA_QUALIFICATION_PUBLICATION_APPROVED=YES`, and production system UTC.
The only durable result is one atomic accepted release under
`data/vault/accepted/alpaca_feed_qualification/<release_id>/` containing the
qualification receipt and release manifest. The publisher performs no network
or credential access, does not modify `config/sources.json`, and cannot
activate a feed or authorize canonical bars or research.

## Verified Alpaca SIP source cutover

The source cutover CLI is plan-only by default:

```powershell
python -m us_stocks_swing_model_v2.cli.activate_alpaca_source
```

It is bound to the exact accepted qualification release, receipt, publication
plan, inactive `config/sources.json` baseline, one implementation successor,
environment, and code/config closures. Planning writes nothing and emits the
exact before/after configuration hashes, receipt path, four declared source
field changes, and a content-addressed activation plan ID.

Execution is a separate activation gate. It requires `--execute`, the exact
approved activation plan ID, and
`ALPACA_SOURCE_ACTIVATION_APPROVED=YES`. It atomically changes only
`config/sources.json`: enable `alpaca_basic_delayed_sip`, set
`qualified_feed=sip`, record the accepted qualification receipt path, and set
the status to `active_sip_qualified_pending_canonical_bars`. Any repository,
release, plan, or config drift stops before the write. The cutover performs no
provider or credential access and does not build canonical bars or authorize
research.

## First bounded active-SIP canonical bars

The first active canonical-bars contract is intentionally limited to AAPL and
SPY for the completed `2026-07-30` XNYS session. It uses a dedicated network
registry so adding the active lane cannot relabel or invalidate the historical
SIP/IEX qualification snapshots.

The checked-in CLI is plan-only by default:

```powershell
python -m us_stocks_swing_model_v2.cli.acquire_canonical_bars
```

Planning is credential-free, network-free, and no-write. It revalidates the
clean main commit, active SIP source configuration, accepted qualification,
accepted identity release, pinned calendar, isolated acquisition registry,
code/config/environment closures, and this exact request:

```text
GET https://data.alpaca.markets/v2/stocks/bars
symbols=AAPL,SPY
start=2026-07-30T04:00:00Z
end=2026-07-30T23:30:00Z
feed=sip
timeframe=1Day
adjustment=raw
asof omitted
sort=asc
limit=10000
```

Execution additionally requires `--execute-network`, the exact approved
acquisition plan ID, `FREE_SOURCE_QUALIFICATION_APPROVED=YES`, and credentials
supplied only through `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` in the process
environment. The contract permits one GET, one page, one attempt, a 30-second
HTTP timeout, a 120-second host timeout, and 1,048,576 response bytes. A
continuation token stops after the first immutable snapshot; it never causes a
second request.

The active lane uses schema-v2 snapshot receipts to preserve both request and
retrieval time while remaining backward-compatible with the preserved
schema-v1 qualification snapshots. Offline verification requires exactly one
valid daily row for each symbol on `2026-07-30`, binds both asset UUIDs and the
pinned XNYS session, and derives a canonical candidate without writing a
release.

Accepted publication is a later, separately approved no-network invocation
bound to the exact publication plan and
`ALPACA_CANONICAL_BARS_PUBLICATION_APPROVED=YES`. It may create one immutable
`alpaca_daily_bars` release containing `bars.parquet`,
`canonical_bar_receipt.json`, and `release_manifest.json`. The release is
`active_historical` source evidence only. It does not authorize an eligible
universe, features, outcomes, research, prediction, or trading.

## Bounded active-SIP canonical-bars successor

Canonical-bar accumulation uses a separate successor contract so the original
two-row release remains immutable and independently verifiable. The successor
planner is also network-free, credential-free, and no-write by default:

```powershell
python -m us_stocks_swing_model_v2.cli.accumulate_canonical_bars
```

It binds the accepted first release as its exact predecessor and plans one SIP
GET for AAPL and SPY from `2026-07-31T04:00:00Z` through
`2026-08-01T03:59:59Z`. The required delta session is `2026-07-31`. Execution
cannot begin before `2026-08-01T04:19:59Z`, preserving both the observed
provider end-boundary behavior and the active source's 20-minute minimum end
lag.

The request remains exactly one GET, one page, one attempt, a 30-second HTTP
timeout, a 120-second host timeout, and at most 1,048,576 response bytes.
Credentials remain process-environment-only. Any continuation token, missing,
extra, malformed, or duplicate session, source or repository drift,
predecessor mismatch, timeout, or response-bound failure stops without a
second request.

Offline verification requires two exact delta rows and combines them
process-locally with the predecessor's two rows. A prospective successor
publication contains a complete four-row canonical table covering
`2026-07-30` and `2026-07-31`; it is not a delta-only release. The receipt and
manifest bind the predecessor release and bars hash, the new snapshot, both
asset UUIDs, the accepted qualification, identity and calendar releases, and
the code/config/environment closures.

Successor publication is a separate no-network gate requiring the exact plan
ID and `ALPACA_CANONICAL_BARS_SUCCESSOR_PUBLICATION_APPROVED=YES`. It creates
one new immutable `alpaca_daily_bars` release and never replaces the
predecessor. Publication does not activate another source, select a current
bars release for downstream consumers, build an eligible universe, or
authorize features, outcomes, research, prediction, or trading.

## Plan-only Alpaca SIP historical backfill

The accepted rehabilitated archive is immutable legacy-discovery evidence for
780 symbols through `2026-07-10`. The historical-backfill planner compares its
verified provider-symbol census with the accepted current identity snapshot.
It selects only currently eligible, active, present `STOCK` and `ETF` rows and
then excludes symbols already represented in the rehabilitated release.

```powershell
python -m us_stocks_swing_model_v2.cli.plan_alpaca_historical_backfill
```

The command is credential-free, network-free, and no-write. It verifies the
clean main repository, active SIP request policy, accepted identity release,
accepted rehabilitated release, pinned XNYS calendar, isolated historical-
backfill network registry, and code/config/environment closures. The checked-in
cohort is 10,072 eligible stocks/ETFs: 657 overlap the rehabilitated archive,
leaving 9,415 current-identity-seeded symbols for the prospective backfill.

The plan sorts symbols, forms at most 100-symbol batches, and crosses them with
11 calendar-year windows covering the 2,644 XNYS sessions from `2016-01-04`
through `2026-07-10`. Each request unit pins:

```text
GET https://data.alpaca.markets/v2/stocks/bars
feed=sip
timeframe=1Day
adjustment=raw
asof omitted
sort=asc
limit=10000
timeout=30 seconds
maximum pages=3
maximum response bytes per page=16,777,216
```

Annual end timestamps use the verified provider-safe boundary one second before
the following Eastern midnight. The full request-unit packet is
content-addressed process-locally; the CLI reports only its identity, census,
group bounds, and non-authorizing metadata rather than printing every URL.
Suggested execution groups contain at most five symbol batches, but they grant
no provider authority. A later execution gate must bind the exact overall plan
and exact group and must separately authorize process-local credential
handling, bounded calls, atomic ignored snapshot landing, and immediate offline
verification.

The same CLI contains the fail-closed group runner, but it cannot execute from
the plan alone. One invocation requires `--execute-group`, the exact approved
overall plan ID, the selected group's exact request-plan-ID hash,
`FREE_SOURCE_QUALIFICATION_APPROVED=YES`, and credentials supplied only through
`APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` in the process environment. It never
reads `api.env`. Approval mismatches stop before environment credential access.

Each invocation executes exactly one group in unit order. Every unit receives a
fresh single-use local network session and may make at most three ordered GETs.
Each response is atomically landed before its pagination token is parsed. The
unit is then verified before the next unit begins; a non-terminal third page,
request drift, redirect, non-200 response, malformed or repeated token, timeout,
oversized response, landing ambiguity, or verification failure stops the group
without retry or cleanup. The 1,800-second group host timeout is enforced by
the owner-operated host invocation.

Before a later attempt, `--plan-group-continuation` performs a no-network,
no-write inventory of the exact immutable snapshot store. For each unit it
revalidates complete retained lineages, explicitly selects the newest valid
lineage by request time, binds every selected snapshot and superseded lineage
into a content-addressed continuation plan, and reduces request and byte bounds
to only units still requiring capture. Equal-time ambiguity, tampering,
unexpected URLs, invalid pagination, or failed row verification stops planning.
Execution requires the separately approved continuation-plan ID and rederives
the complete plan before credential access or network-session creation. It
revalidates selected pages again in group order and calls Alpaca only for the
remaining units; retained evidence is never rewritten or cleaned up.

Offline verification must require terminal pagination, requested-symbol and
pinned-session containment, unique symbol/session rows, valid raw daily OHLCV
and timestamps, and explicit exclusions for requested symbols with no returned
history. Alpaca's exact numeric `vw=0` sentinel is normalized process-locally
to unavailable VWAP before validation and counted in the assessment. It does so
without changing the retained raw response. This applies with or without
reported activity because VWAP is ancillary provider metadata, not a selected
model feature; nonzero VWAP must still be finite and positive, while OHLC,
volume, trade-count, timestamp, and session invariants remain strict.
Verification cannot require every current symbol to have existed in every
historical session.

The process-local group assessment content-addresses the exact plan, group,
unit assessments, landed snapshot identities, row counts, and zero-history
exclusions. It is conversation output only and is not an accepted release or a
publication plan. Snapshot directories are immutable ignored acquisition
evidence; group execution does not canonicalize, merge, publish, activate, or
authorize research.

This is deliberately not a historical membership reconstruction. The cohort is
seeded from one current identity snapshot, so any eventual bars remain
`LEGACY_DISCOVERY` with
`CURRENT_IDENTITY_SEEDED_PIT_UNRESOLVED` quality. They cannot establish
survivorship-safe coverage, confirmation, source activation, eligibility,
features, outcomes, WFA readiness, or research authority. HFDL is excluded.
Execution, verification, publication, and any merge with the rehabilitated
release remain separate gates.

### Offline completeness and publication planning

`plan_alpaca_historical_backfill_publication` is no-network and no-write. It
rebuilds the current backfill plan, revalidates every retained lineage in all
19 groups, requires all 1,045 request units to be complete, and content-addresses
the selected snapshot, raw-byte, header, receipt, continuation, and unit-
assessment censuses. Missing, ambiguous, duplicated, incomplete, non-terminal,
tampered, or locally unverified evidence fails closed.

The resulting publication plan binds a future
`alpaca_historical_daily_bars` release with
`legacy_discovery_only` role and
`CURRENT_IDENTITY_SEEDED_PIT_UNRESOLVED` quality. It requires exact snapshot-
evidence preservation and deterministic calendar-year Parquet regeneration.
The current contract deliberately defers the release ID because the shard
builder and publication execution are not implemented. The plan grants no
publication, activation, eligible-universe, feature, outcome, training,
evaluation, research, provider, credential, or HFDL authority.

```text
python -m us_stocks_swing_model_v2.cli.plan_alpaca_historical_backfill_publication \
  --created-at <exact-production-UTC-Z>
```

Implementing the deterministic shard builder is a separate local phase.
Publishing one exact accepted release is a later, separately bounded generated-
evidence gate after its prospective release identity is known.

## Landed evidence

The guarded transport rejects redirects and binds the exact requested URL,
response URL, HTTP status, normalized headers, and raw-byte hash before the
snapshot is committed. Empty or oversized responses fail closed. Snapshot
publication is atomic and content-addressed.

A loaded network snapshot is `LOCAL_INTEGRITY_VERIFIED` only when all of these
remain valid:

- the receipt and files are internally hash-consistent;
- the acquisition capability matches the checked active network registry;
- the evidence is `NETWORK_AS_RECEIVED`;
- the retrieval timestamp came from production system UTC; and
- no synthetic-only permit is present.

This state means the local bytes and their recorded acquisition metadata are
tamper-evident and reproducible. It is not a claim of independent provenance,
third-party witnessing, provider correctness, or research fitness.

## Offline Nasdaq verification

Verification performs no network call:

```powershell
python -m us_stocks_swing_model_v2.cli.qualify_free_sources `
  --verify-nasdaq-snapshot <absolute-snapshot-directory> `
  --prior-nasdaq-accepted-record-count <trusted-prior-count>
```

The command reloads and rehashes the immutable snapshot, revalidates the pinned
network capability, and only then permits parsing. Production parsing still
requires the record count from the immediately preceding accepted Nasdaq
qualification receipt; there is no implicit first-run or routine-update bypass.

The checked-in `config/nasdaq_qualification_receipt.json` predates this mode and
remains preserved historical negative evidence. Its `NOT_ACTIVE` state and
stale identities must not be rewritten or relabeled. A future qualification is
a new acquisition and a new receipt.

## First trusted Nasdaq count bootstrap

The normal single-snapshot parser still has no first-run bypass. The separately
implemented bootstrap requires exactly two fresh locally integrity-verified
captures:

- snapshot A is frozen in `config/nasdaq_bootstrap_policy.json`;
- snapshot B must have different raw bytes, a later retrieval time, and a
  strictly later embedded Nasdaq file-creation time;
- both captures must pass the full absolute structure and completeness checks
  under the same pinned network registry; and
- the A-to-B count change must stay within the normal 10% drop and 25% absolute
  change limits.

After snapshot B is separately captured, the pair can be assessed offline:

```powershell
python -m us_stocks_swing_model_v2.cli.qualify_free_sources `
  --verify-nasdaq-bootstrap-pair `
  <absolute-snapshot-A-directory> `
  <absolute-snapshot-B-directory>
```

This command performs no network call and writes no receipt. A pass identifies
snapshot B's count only as a non-active baseline candidate. Publication and
activation each remain separately unauthorized. The preserved historical count
is emitted only as comparison metadata and cannot affect pass/fail. Owner-run
local integrity is reproducibility evidence, not independent provenance.

## Non-active bootstrap receipt publisher

The dedicated publisher is plan-only by default:

```powershell
python -m us_stocks_swing_model_v2.cli.publish_nasdaq_bootstrap
```

Plan generation performs no writes and revalidates the clean committed Git
closure, frozen implementation plan, current registry and environment, both
snapshot receipts and raw bytes, the A/B assessment, preserved historical
receipt, exact accepted/work roots, and inactive source configuration. It emits
a content-addressed publication plan ID.

Execution is a separate gate. It additionally requires `--execute`, the exact
approved plan ID, `NASDAQ_BOOTSTRAP_PUBLICATION_APPROVED=YES`, production system
UTC, and the clean one-commit successor to the reviewed base. The only permitted
output is one atomic, idempotent release at:

```text
data/vault/accepted/nasdaq_bootstrap_baseline/<release_id>/
```

The release contains `nasdaq_bootstrap_receipt.json` and
`release_manifest.json`, is labeled `qualification_evidence_only`, and may
establish snapshot B's count as a routine continuity baseline. It does not make
the Nasdaq source active, modify `config/sources.json`, relabel the historical
receipt, establish historical membership, or authorize models or research.

## Prospective identity-release readiness

The published bootstrap release supplies only the trusted prior count for the
next normal Nasdaq parse. Snapshot B is not reused as the first active identity
snapshot. A prospective merged identity input requires:

- one separately approved, as-received Alpaca `/v2/assets` capture;
- one separately approved Nasdaq capture with a later retrieval and embedded
  file-creation time than snapshot B;
- a normal Nasdaq continuity parse against the accepted count of 13,064; and
- an offline merge that remains `NETWORK_AS_RECEIVED`.

The Alpaca asset command is plan-only by default and performs no write:

```powershell
python -m us_stocks_swing_model_v2.cli.qualify_identity_sources
```

Its future execution is limited to one request, one page, 32 MiB, and 30
seconds. Execution requires `--execute-network`, the exact separately approved
request plan ID, `FREE_SOURCE_QUALIFICATION_APPROVED=YES`, credentials supplied
only through the process environment, and production system UTC. Credentials
are never included in the request plan, snapshot, output, or logs.

The captured `/v2/assets` response remains immutable and unfiltered. Identity
readiness applies the frozen
`config/alpaca_asset_projection_policy.json` contract offline: validate every
raw row, select only `class=us_equity` and `status=active`, audit every excluded
class/status, and fail rather than deduplicate a selected asset ID or symbol.
The legacy strict parser API remains available for its existing callers. The
approved source epoch for any later release is
`nasdaq_alpaca_active_us_equity_v1`.

One landed snapshot can be reverified without network access or writes:

```powershell
python -m us_stocks_swing_model_v2.cli.qualify_identity_sources `
  --verify-alpaca-assets <alpaca-assets-snapshot>
```

The output binds the raw snapshot hash, projection contract and assessment IDs,
raw and selected counts, selected-row hash, and exclusion counts. It neither
publishes an identity release nor activates a source.

After both captures exist, the join assessment is offline and no-write:

```powershell
python -m us_stocks_swing_model_v2.cli.qualify_identity_sources `
  --assess-pair <alpaca-assets-snapshot> <fresh-nasdaq-snapshot>
```

The original identity-readiness authorization remains immutable historical
evidence. Publication planning additionally binds the separately
content-addressed eligibility-remediation record in
`config/nasdaq_identity_readiness_policy.json`. That record fixes the reviewed
base commit and tree plus the exact assessment and both input snapshots. The
production publisher recomputes the assessment and requires those three input
identities to match before it accepts a completely clean tree descended by
exactly one reviewed successor commit; dirty, mismatched-input, zero-distance,
unrelated, base-tree-mismatched, and multi-commit states fail closed.

The identity publisher also defaults to a no-write plan:

```powershell
python -m us_stocks_swing_model_v2.cli.publish_identity_release `
  --alpaca-assets-snapshot <alpaca-assets-snapshot> `
  --nasdaq-snapshot <fresh-nasdaq-snapshot>
```

Publication requires its own exact plan approval, `--execute`, and
`NASDAQ_IDENTITY_RELEASE_PUBLICATION_APPROVED=YES`. It may create only one
atomic accepted `identity` release containing `identity_snapshots.json` and
`identity_publication_receipt.json`. It performs no network call and does not
modify `config/sources.json` or activate any source. Activation remains a later
independent review and execution gate.
