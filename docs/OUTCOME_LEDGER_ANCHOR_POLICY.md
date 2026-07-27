# Outcome Ledger Anchor Policy

## Decision

Outcome-ledger truncation and wholesale replacement are in scope. A valid local
hash chain is not sufficient completeness evidence because an attacker or
operator error could replace the entire chain with a separately consistent
history.

Every nonempty outcome ledger therefore requires an exact content-addressed
head receipt retained in a separate, non-nested anchor tree. The outcome and
prediction ledgers use the same `LedgerAnchorStore` contract.

## Required behavior

- Every outcome append atomically checks the expected record count and prior
  head before writing.
- The first append rejects a predecessor anchor. Every later append requires
  and verifies the exact retained prior anchor against the complete current
  history.
- A successful append returns the new anchor path. Downstream verification and
  census reconciliation require that exact current anchor.
- The anchor binds the ledger identity, record type, record count, head hash,
  full ledger hash, predecessor anchor, recording time, and clock authority.
- The ledger file and anchor root must be separate and non-nested. Links,
  malformed receipts, replacement histories, truncation, replayed predecessors,
  and stray anchor-tree entries fail closed.

## Authority and limitations

These receipts are independently retained local commitments. They are not an
external timestamp, WORM store, signature, or production authority. They do
not authorize outcome maturation, research, promotion, or live use.

Moving both trees together to one untrusted replacement remains outside the
guarantee of a local anchor. Any future external retention or signing design
requires a separate reviewed policy and implementation.

## Interruption and recovery

Two distinct interruption windows have different recovery contracts:

- Before a hash-chain append is committed, its exact precommit journal may be
  recovered. Recovery revalidates the complete envelope, expected sequence and
  predecessor, record hash, record type, time authority, and the verifier's
  explicit authorized synthetic-permit census. A journal from any unlisted
  permit or any malformed, stale, or conflicting envelope fails closed.
- After the ledger append is committed but before the separate outcome anchor
  is published, the ledger has an unanchored tail. The system must stop: it may
  not automatically bless, truncate, replace, or continue from that tail.

The implemented precommit-journal recovery is synthetic mechanics only and
does not repair or authorize an unanchored outcome tail. Recovery of an
already-committed unanchored tail requires explicit owner review of the ledger
bytes, the last retained anchor, the interrupted operation, and the intended
new record. Until a separately implemented outcome-anchor recovery operation
exists, restoration to the exact previously anchored ledger bytes is the only
supported recovery for that second window. All failed and restored evidence
must be retained according to the applicable incident and release-lifecycle
policy.
