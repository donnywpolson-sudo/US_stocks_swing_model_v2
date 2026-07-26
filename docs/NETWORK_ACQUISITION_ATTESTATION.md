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

## Three-stage workflow

### 1. Pre-request authorization

Plan-only mode emits one exact request packet per selected source:

```powershell
python -m us_stocks_swing_model_v2.cli.qualify_free_sources `
  --plan-only `
  --nasdaq-only `
  --authorization-request-directory C:\absolute\new\request-directory
```

After the public authority is active, prepare the exact canonical signing
bytes:

```powershell
python -m us_stocks_swing_model_v2.cli.assemble_network_authorization `
  --request C:\absolute\request\nasdaqtraded.json `
  --signing-payload-output C:\absolute\new-signing-payload.bin `
  --authority-registry C:\absolute\active-registry.json `
  --authority-key-id <key-id> `
  --public-key-file C:\absolute\public.jwk
```

The external authority signs those bytes outside the repository.
The request binds `AUTHORIZE_NETWORK_ACQUISITION`, the initial URL, source,
network registry, 30-second timeout, response limit, page limit, pagination
policy, ten-minute expiry, and a 256-bit nonce. Assemble and verify the
detached signature offline:

```powershell
python -m us_stocks_swing_model_v2.cli.assemble_network_authorization `
  --request C:\absolute\request\nasdaqtraded.json `
  --detached-signature C:\absolute\signature.txt `
  --authority-registry C:\absolute\active-registry.json `
  --authority-key-id <key-id> `
  --public-key-file C:\absolute\public.jwk `
  --output C:\absolute\new-network-authorization.json
```

The receipt is atomically consumed before the first request. It cannot be
replayed; a failed or interrupted acquisition requires a freshly signed
receipt.

### 2. Bounded capture

When separately authorized, one exact Nasdaq capture can be performed with:

```powershell
$env:FREE_SOURCE_QUALIFICATION_APPROVED='YES'
python -m us_stocks_swing_model_v2.cli.qualify_free_sources `
  --execute-network `
  --nasdaq-only `
  --network-authorization C:\absolute\new-network-authorization.json `
  --network-authority-registry C:\absolute\active-registry.json `
  --network-key-id <key-id> `
  --network-public-key-file C:\absolute\public.jwk
```

This operation lands the immutable snapshot and prints an
`attestation_request`. It does not parse the response as trusted identity
evidence, write a qualification receipt, or make the source active.

Provider execution additionally requires the repository-mandated exact
request limit, timeout, output disposition, and explicit authorization. The
environment variable alone is not authorization.

For Alpaca pagination, one receipt authorizes one bounded page sequence. Only
the `page_token` returned by the immediately preceding verified response may
change; all other signed URL parameters remain exact.

### 3. Acquisition signing and offline verification

The external signer independently reviews the captured bytes and request
metadata, adds the exact signature fields, signs the canonical receipt outside
the repository, and returns a detached JSON receipt. Copy the public receipt
into the configured ignored qualification root before verification; the
verifier rejects receipt paths outside its approved root. Production
private-key material must never enter this checkout.

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
- A pre-request authorization receipt cannot be reused for a second network
  acquisition.
- Registry mutation or authority revocation invalidates verification.
- Verification enriches an in-memory snapshot; it does not mutate the
  content-addressed snapshot directory.
- No code in this repository signs a production acquisition receipt.
