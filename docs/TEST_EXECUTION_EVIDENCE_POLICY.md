# Test Execution Evidence Policy

Repository tests establish deterministic code-level behavior. They do not by
themselves authenticate that a particular test command ran in a particular
worktree.

Trust-sensitive source qualification therefore has two distinct evidence
layers:

1. Offline integration tests exercise the real external-signature verifier,
   request-plan binding, single-use authorization store, guarded provider
   entry point, transport-response binding, atomic snapshot landing, detached
   acquisition attestation, and verified reload. Only the network transport is
   substituted.
2. Current operating evidence is retained outside the repository by the
   assessment launcher and is bound to its exact target inventory and complete
   file-population receipts.

Neither layer authorizes a provider request. A real request still requires the
exact external authorization workflow, and a downloaded snapshot remains
non-trust-eligible until its independent acquisition attestation verifies.

The checked-in authority registry remains `NOT_CONFIGURED`. Consequently this
repository must not claim that a local pytest log is a production-authenticated
run artifact. Configuring an authority or signing an operating receipt is a
separate owner action; synthetic fixture keys are test-only and prohibited from
the production registry.
