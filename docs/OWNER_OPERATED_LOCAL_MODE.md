# Owner-Operated Local Mode

This repository is a personal, single-owner project. It does not rely on a
second signing party to approve research, release, recovery, or provider
actions.

The local mode separates two concerns:

- explicit operation: a mutating command or API call must be invoked
  deliberately, and provider downloads additionally require the CLI flag plus
  the owner confirmation environment value; and
- integrity evidence: inputs, policies, releases, observations, and results
  remain exact, content-addressed, hash-chained, and fail closed on mismatch.

Research and release boundaries use schema-v2
`OWNER_OPERATED_LOCAL_INTEGRITY` records. Each record binds an exact scope,
subject, complete string-to-string evidence map, repository-issued clock mode,
creation time, and optional synthetic-test permit. Its ID is the SHA-256 of the
canonical record content.

These records are not signatures and do not claim independent approval. They
make the owner’s exact local action reproducible and tamper-evident. Frozen
schema-v1 signed records are rejected by schema-v2 consumers; they are not
migrated, relabeled, or treated as local records.

The following controls remain binding:

- accepted-release manifests and exact-tree verification;
- atomic publication and lock/containment checks;
- immutable raw acquisition snapshots and normalized response metadata;
- point-in-time and bitemporal identity/action rules;
- frozen trial, gate, robustness, monitoring, and outcome contracts;
- hash-chain ledgers and local anchors;
- synthetic-only permits that cannot become production evidence; and
- dry-run/plan-only defaults for provider and copy commands.

Local integrity does not prove that a provider is correct, that a hypothesis is
profitable, or that a result was independently witnessed. Those are evidence
questions, not reasons to retain an unused key-management system.
