# Network Acquisition Attestation

Network capture and trust promotion are separate operations.

An HTTPS response can be landed as immutable acquisition evidence, but its
self-hashed capability is not independent provenance. It remains
non-trust-eligible until an external signer attests the exact immutable
snapshot.

## Bound evidence

The detached receipt binds:

- immutable snapshot ID;
- network-acquisition registry ID and capability ID;
- source and exact URL;
- accepted HTTP status;
- raw-byte SHA-256;
- normalized-header SHA-256;
- production UTC retrieval time; and
- time-authority mode.

The receipt also binds its scope, signature time, key ID, authority-registry
ID, and authority class. Its required scope is
`ATTEST_NETWORK_ACQUISITION`.

## Two-stage workflow

### 1. Bounded capture

When separately authorized, one exact Nasdaq capture can be performed with:

```powershell
$env:FREE_SOURCE_QUALIFICATION_APPROVED='YES'
python -m us_stocks_swing_model_v2.cli.qualify_free_sources `
  --execute-network `
  --nasdaq-only
```

This operation lands the immutable snapshot and prints an
`attestation_request`. It does not parse the response as trusted identity
evidence, write a qualification receipt, or make the source active.

Provider execution additionally requires the repository-mandated exact
request limit, timeout, output disposition, and explicit authorization. The
environment variable alone is not authorization.

### 2. External signing and offline verification

The external signer independently reviews the captured bytes and request
metadata, adds the exact signature fields, signs the canonical receipt outside
the repository, and returns a detached JSON receipt. Production private-key
material must never enter this checkout.

Offline verification uses:

```powershell
python -m us_stocks_swing_model_v2.cli.qualify_free_sources `
  --verify-nasdaq-snapshot <absolute-snapshot-directory> `
  --acquisition-attestation <absolute-signed-receipt> `
  --attestation-authority-registry <absolute-active-registry> `
  --attestation-key-id <key-id> `
  --attestation-public-key-file <absolute-public-jwk>
```

Verification performs no network call. It reloads the immutable snapshot,
revalidates the network registry capability, revalidates the active external
public authority, verifies the detached RSA signature and signing chronology,
and only then permits the Nasdaq parser to consume the bytes.

## Fail-closed properties

- `LandedSnapshot` cannot be directly constructed by a caller.
- Unattested captures report `trust_eligible=false`.
- Synthetic snapshots cannot be promoted by an external receipt.
- The receipt cannot be reused for another snapshot, source, URL, response,
  registry, capability, or retrieval time.
- Registry mutation or authority revocation invalidates verification.
- Verification enriches an in-memory snapshot; it does not mutate the
  content-addressed snapshot directory.
- No code in this repository signs a production acquisition receipt.
