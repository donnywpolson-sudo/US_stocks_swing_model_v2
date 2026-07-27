# Test Execution Evidence Policy

Repository tests establish deterministic code-level behavior. They do not by
themselves authenticate that a particular test command ran in a particular
worktree.

Trust-sensitive source qualification therefore has three distinct evidence
layers:

1. Offline integration tests exercise exact request-plan binding, unforgeable
   process-local sessions, ordered single-use attempts, the guarded provider
   entry point, transport-response binding, atomic snapshot landing, local
   integrity verification, verified reload, and tamper refusal. Only the
   network transport is substituted.
2. Portable GitHub Actions runs execute the fresh-checkout test population and
   retain JUnit output, the installed environment, and a machine-readable
   evidence record bound to the source commit, tree, workflow, and run. These
   artifacts are retained for 90 days. The three tests marked
   `local_evidence` require the ignored completed-migration capsule and are
   deliberately neither run in this lane nor uploaded to GitHub.
3. Current operating evidence is retained outside the repository by the
   assessment launcher and is bound to its exact target inventory and complete
   file-population receipts.

None of these layers initiates a provider request. A real request still
requires the explicit CLI flag plus the local owner confirmation environment
value. A downloaded snapshot can establish local integrity after complete
offline verification, but it cannot establish independent provenance.

A local pytest log and a GitHub Actions artifact are review evidence, not
independently anchored immutable records, provider attestations, or alpha
evidence.
