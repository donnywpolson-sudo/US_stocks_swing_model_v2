# Test Execution Evidence Policy

Repository tests establish deterministic code-level behavior. They do not by
themselves authenticate that a particular test command ran in a particular
worktree.

Trust-sensitive source qualification therefore has three distinct evidence
layers:

1. Offline integration tests exercise the real external-signature verifier,
   request-plan binding, single-use authorization store, guarded provider
   entry point, transport-response binding, atomic snapshot landing, detached
   acquisition attestation, and verified reload. Only the network transport is
   substituted.
2. Portable GitHub Actions runs execute the fresh-checkout test population and
   retain JUnit output, the installed environment, and a machine-readable
   evidence record bound to the source commit, tree, workflow, and run. These
   artifacts are retained for 90 days. The three tests marked
   `local_evidence` require the ignored completed-migration capsule and are
   deliberately neither run in this lane nor uploaded to GitHub.
3. Current operating evidence is retained outside the repository by the
   assessment launcher and is bound to its exact target inventory and complete
   file-population receipts.

None of these layers authorizes a provider request. A real request still requires the
exact external authorization workflow, and a downloaded snapshot remains
non-trust-eligible until its independent acquisition attestation verifies.

The checked-in authority registry remains `NOT_CONFIGURED`. Consequently this
repository must not claim that a local pytest log is a production-authenticated
run artifact. A GitHub Actions artifact is time-limited review evidence, not an
independently anchored immutable record, a production-authenticated receipt, or
alpha evidence. Configuring an authority or signing an operating receipt is a
separate owner action; synthetic fixture keys are test-only and prohibited from
the production registry.
