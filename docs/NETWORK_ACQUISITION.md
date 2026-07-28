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
