# Audit Workflow

This document distinguishes audit preparation, audit execution, and project
readiness. It is coordination guidance; it does not authorize any action.

## Status Vocabulary

- `NOT_PREPARED`: no current canonical envelope exists.
- `PREPARED`: a canonical envelope exists but its host and contract validation
  has not completed.
- `VALIDATED`: preparation-time metadata, host, script, identities, ordering,
  and bounds passed. No reviewer has started.
- `STARTED`: the exact authorized reviewer invocation began.
- `INCOMPLETE`: the invocation stopped without a reliable final result.
- `COMPLETE`: the authorized audit produced its final result.

Audit completion never implies project readiness. Every audit-related result
states:

1. `Master Audit`
2. `Meta Audit`
3. `Project readiness`
4. `Next gate`

## Action Classes

`LOCAL_CORRECTABLE` work is repository-local and reversible. An implementation
request includes focused tests, static checks, and at most two materially
corrective edit-and-validation cycles.

`READ_ONLY_INVOCATION` work is a content-addressed audit, assessment, or
diagnostic whose manifest controls independence, ordering, attempt use, and
retry.

`MUTATING_OR_EXTERNAL` work includes generated evidence, data/release/receipt
mutation, providers, research, model activity, activation, trading, destructive
work, commit, push, and cutover. It retains exact action-specific authorization.

## Meta Audit Flow

1. Freeze the exact ordinary tracked reference corpus without emitting target
   content.
2. Run metadata-only `HostProfile` and `SelfTest` against the exact checked-in
   reader. A failure here invalidates preparation; it does not spend a review.
3. Greedily build ordered maximal read groups capped at 400 numbered lines and
   20,000 UTF-8 bytes. Reference groups precede B01; target groups follow it.
4. Canonicalize and content-address the envelope. Retention and execution are
   separately authorized.
5. Before reviewer creation, derive the validated target-content-free dispatch
   packet from the exact envelope. Pass the entire canonical packet directly
   to the reviewer without projection or reconstruction; each complete
   command record, including its literal `argv`, is authoritative. An envelope
   read outside a declared reader command invalidates the attempt.
6. Create a reviewer with no inherited turns or prior target access. Execute
   only the dispatch packet's envelope-bound literal commands. Every complete
   `ReadGroup` output ends with the exact checked-in completion footer. A
   missing footer means transport truncation and stops the attempt without an
   external measurement command or retry.
7. Bind the final findings to the exact envelope, dispatch, reviewer independence
   attestation, target identity, and group census.

The Meta Audit reviews `MASTER_AUDIT.md` as a specification. It does not execute
the Master Audit or classify current project readiness.

## Reviewer Transport Freeze

The current reviewer-creation interface cannot deliver the complete canonical
dispatch unchanged in the fresh reviewer’s initial context. While that host
capability is unchanged, do not prepare or materialize another envelope solely
to retry transport, request another reviewer invocation, or reconstruct,
project, compact, template, or complete the dispatch after reviewer creation.

Reopen reviewer execution only after a material interface change and a
target-free synthetic transport check proves that a packet at least as large as
the canonical dispatch reaches the fresh reviewer before creation, unchanged
and checksum-verified, without a retained dispatch file or undeclared process.
Preserve existing envelopes and stopped-attempt evidence; this freeze grants no
cleanup authority.

## Handoffs

`CODEX_HANDOFF.md` is for genuine thread transfer, context loss, or an
external/high-risk gate that depends on recorded continuation state. In one
live thread, reconcile stale handoff prose against Git but continue safe,
already-authorized work. Do not create handoff-only commits for routine prompt,
validation, or gate transitions.
