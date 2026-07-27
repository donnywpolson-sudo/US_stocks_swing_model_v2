# Ledger Recovery Journal Lifecycle

A hash-chain ledger recovery journal is tentative append evidence. It is never
accepted history merely because the journal file exists.

On every public `read_verified`:

1. The already committed ledger is verified first.
2. A valid journal is replayed exactly, the resulting ledger is verified, and
   the active journal is removed.
3. If the committed ledger is sound but the journal is malformed, conflicting,
   unauthorized, or otherwise invalid, its exact bytes are preserved under the
   content-addressed sibling name
   `.<ledger-name>.rejected-journal-<sha256>.json`.
4. The active journal name is then removed durably and the rejecting read raises
   `IntegrityError`. A later public read can verify the unchanged committed
   ledger without repeatedly processing the rejected bytes.

Rejected journal evidence is not replayed, promoted, or deleted automatically.
An existing content-addressed destination must contain the exact same bytes and
be an independent plain file; otherwise disposition fails closed. If the
committed ledger itself is invalid, or rejected evidence cannot be preserved,
the active recovery path remains failed rather than hiding the integrity
problem.

This lifecycle is local recovery mechanics only. It does not make synthetic
history trust eligible, authorize a production operation, or repair a corrupt
committed ledger.
