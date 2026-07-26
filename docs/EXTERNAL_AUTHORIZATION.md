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

## Provider network authorization

Every provider request additionally requires an externally signed
`AUTHORIZE_NETWORK_ACQUISITION` receipt. The receipt identifies one exact
bounded acquisition plan and is durably consumed before transmission. The
environment token and execution flag remain separate gates. Missing, expired,
replayed, over-broad, wrong-source, wrong-URL, or wrong-registry receipts fail
before a provider connection is opened.

Nasdaq authorizes one exact GET. Alpaca bar and corporate-action receipts
authorize a bounded pagination family: the signed initial URL is exact and
only a verified preceding response may supply the next `page_token`.
