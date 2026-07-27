# Test Authority Isolation

The checked-in `config/authorization_authorities.json` file is the only
production authority registry accepted by public project entry points. It is
currently `NOT_CONFIGURED`, so no external authority is active.

Tests use the public RFC 7515 RSA example solely to exercise asymmetric
verification mechanics. Its explicit non-production identity is:

- key ID: `RFC7515-TEST-FIXTURE-ONLY`
- canonical public-JWK SHA-256:
  `a58c7fb8f3607028fb44a39b05c65d8caa876cdde4afc1012091aeb08efa5b82`

The production registry loader rejects either that key ID or that public-key
fingerprint. This is enforced even if a future registry entry otherwise has a
valid authority class and signature algorithm. The fixture private exponent
must remain under `tests/`; regression coverage scans shipped Python source to
ensure the private fixture value is not packaged.

Temporary registries used by isolated unit tests are not production trust
anchors. Their acceptance requires an explicit test-only replacement of the
reviewed-registry locator and does not change the repository-pinned production
path.

Reviewers must verify all of the following before activating any real
authority:

1. The checked-in registry contains neither prohibited fixture identifier nor
   fingerprint.
2. Public loading still requires the exact checked-in registry path.
3. The production registry and verification key agree on the declared
   fingerprint.
4. No test private material is present beneath `src/` or release packages.
5. Activation is reviewed separately and does not inherit authority from test
   receipts or synthetic permits.
