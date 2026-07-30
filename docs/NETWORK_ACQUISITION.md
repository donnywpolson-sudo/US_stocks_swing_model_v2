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
