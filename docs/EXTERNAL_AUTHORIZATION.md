# External Authorization

External authorization is verification-only in this repository. Production
private keys and signing operations stay outside the checkout.

## Supported contract

- Authority class: `EXTERNAL_USER_AUTHORITY`
- Signature algorithm: `RSASSA_PKCS1_V1_5_SHA256`
- Public-key format: an exact UTF-8 JSON Web Key containing only `alg`, `e`,
  `kty`, `n`, and `use`
- Required JWK values: `alg=RS256`, `e=AQAB`, `kty=RSA`, and `use=sig`
- RSA modulus policy: 2048 through 8192 bits
- Registry: `config/authorization_authorities.json`

The registry entry binds the key ID, SHA-256 of the exact public-JWK bytes,
authority class, and signature algorithm. Registry status must be `ACTIVE` and
the key ID must resolve to exactly one entry. Any registry mutation or
revocation invalidates an already loaded authority.

The checked-in registry is deliberately `NOT_CONFIGURED`. Therefore no current
receipt can authorize production activity.

Production verification accepts only that exact checked-in registry path. A
caller-created registry is rejected even when it is schema-valid, marked
`ACTIVE`, and matches the caller's public key. Activation therefore requires a
separately reviewed change to the checked-in trust anchor.

Authorization assembly also requires an existing absolute
`--allowed-output-root`. Signing payloads and assembled receipts must be new
paths contained under that root; outside-root and link-mediated destinations
are rejected before parent directories are created.

## Separation of authority

The repository:

- loads and validates a public JWK;
- validates exact receipt scope, subject, bindings, issue time, and expiry;
- verifies the external RSA signature; and
- fails closed on missing, malformed, stale, revoked, or mismatched evidence.

The repository does not:

- generate, read, store, or transmit a production private key;
- sign a production authorization receipt;
- treat a synthetic HMAC receipt as production authority; or
- activate an authority merely because verification code exists.

## Hash-copy CLI

An externally authorized copy requires all of:

```text
--execute
--approval <reviewed migration approval>
--authorization <externally signed receipt>
--authority-registry <exact reviewed config/authorization_authorities.json>
--authority-key-id <exact key ID>
--public-key-file <matching RSA public JWK>
```

The receipt must bind the exact reviewed migration plan and implementation
evidence. `HASH_COPY_APPROVED=YES` remains a separate required execution gate.
The controlled-rebuild authority and external-authority inputs are mutually
exclusive.

Public-key configuration, external signing, and execution each require their
own explicit review and authorization.

## Mechanical-readiness publication authorization

Assessment remains read-only and needs no authorization. Publication with
`assess_mechanical_readiness --execute` requires an independently signed
`AUTHORIZE_MECHANICAL_READINESS_PUBLICATION` receipt plus the exact reviewed
authority registry, key ID, and public JWK. The receipt subject is the verified
assessment ID. Its bindings fix the foundation release, creation time,
accepted-release root, work root, the two dataset and receipt names, and an
exact publication count of two.

The receipt is verified with the production system UTC clock after the
assessment succeeds and before any work directory or accepted release is
created. Synthetic fixture publication remains a separate non-authorizing test
path and cannot be mixed with external authority inputs. Mechanical milestone
receipts remain non-authorizing for research, candidates, production, or
trading even when their publication was authorized.

## Provider network authorization

`ACTIVE` in `config/network_acquisition_registry.json` means only that the
reviewed endpoint and accepted-status catalog is available for request-plan
validation and response-capability checks. It is not permission to open a
connection, transmit credentials, or acquire data. It does not activate an
external signing authority and does not make landed evidence trust eligible.

While `config/authorization_authorities.json` is `NOT_CONFIGURED`, every real
network acquisition remains prohibited even when the network catalog is
`ACTIVE`, the provider CLI execution flag is present, or an environment token
is set. Those controls are necessary gates but never substitute for an active
external authority and an exact externally signed receipt.

Every provider request additionally requires an externally signed
`AUTHORIZE_NETWORK_ACQUISITION` receipt. The receipt identifies one exact
bounded acquisition plan and is durably consumed before transmission. The
environment token and execution flag remain separate gates. Missing, expired,
replayed, over-broad, wrong-source, wrong-URL, or wrong-registry receipts fail
before a provider connection is opened.

Request-plan construction is not itself trusted. Signing-request generation,
authorization consumption, and each request attempt independently recompute
the plan identity and revalidate its source, origin, method, limits,
pagination, and registry binding against the pinned network catalog.

Authorization preflight issues one in-memory request-attempt capability. The
guarded transport binds its exact response URL, status, byte hash, header hash,
and response limit to that attempt. Production snapshot landing requires and
irreversibly consumes the resulting transport evidence. Missing, forged,
mismatched, or replayed evidence fails before any network snapshot is landed.
Synthetic fixtures continue through the separate synthetic-only landing API
and are never network-as-received or trust eligible.

Nasdaq authorizes one exact GET. Alpaca bar and corporate-action receipts
authorize a bounded pagination family: the signed initial URL is exact and
only a verified preceding response may supply the next `page_token`.

## Corporate-action completeness authorization

Process-date acquisition evidence never authorizes outcome maturation.
Schema-v5 corporate-action coverage requires a separate
`AUTHORIZE_EFFECTIVE_EVENT_COMPLETENESS` receipt. Its subject is the immutable
effective-event coverage content ID; its bindings include the full provider
coverage identity and process-date bounds, snapshot and requested-symbol
censuses, acquisition mode, effective-session interval, asset census,
reviewed provider-contract hash, reviewed late-arrival policy hash, review
time, and source epoch.

The loader reconstructs those bindings from the retained provider receipt and
coverage payload, verifies the current pinned authority and receipt window,
then derives the operative coverage ID from the authorized content ID plus the
accepted release ID and epoch. Network evidence requires external authority;
synthetic authority is accepted only for synthetic provider evidence and
cannot produce production trust. With the external authority registry
`NOT_CONFIGURED`, production completeness promotion remains prohibited and
outcomes fail closed.
