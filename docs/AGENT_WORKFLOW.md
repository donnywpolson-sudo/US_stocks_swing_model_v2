# Agent Workflow

This policy applies to future multi-step work in this repository. It reduces
coordination overhead while preserving every scientific, data, provider,
secret, commit, push, and destructive-action boundary in `AGENTS.md`.

## Phase Envelope

A user request establishes a bounded larger goal and completion criteria.
Repository-local implementation authority includes its routine edits, focused
validation, static checks, read-only verification, and at most two materially
corrective cycles. Those steps proceed without serial “do that” prompts.

Actions are classified before execution:

- `LOCAL_CORRECTABLE`: reversible repository-local work.
- `READ_ONLY_INVOCATION`: manifest-bound audit, assessment, or diagnostic work.
- `MUTATING_OR_EXTERNAL`: generated evidence, external effects, project data,
  scientific execution, commit, push, or destructive work.

Only simultaneously decision-complete actions with the same class, scope,
risks, outputs, and stop conditions may share one approval. Unknown
evidence-dependent actions remain separate.

## Preparation Before Invocation

Before content-addressing or requesting approval for an invocation:

1. Resolve the exact repository, executable, script, runtime, and lock identity.
2. Validate literal transport, quoting, encoding, hashing, path, and host
   capabilities with metadata-only inputs.
3. Freeze exact targets, outputs, run limits, timeouts, and failure class.
4. Minimize declared processes using deterministic maximal safe batches.
5. Content-address the plan or envelope.
6. Before requesting execution or creating the reviewer, derive a
   target-content-free, content-addressed reviewer dispatch packet from the
   exact validated envelope. The packet supplies every exact command and
   substitutes the envelope path and file hash; the reviewer must not inspect
   the envelope outside a declared reader command.

A preparation failure does not spend a substantive invocation when no declared
evidence read or side effect began.

Every bounded read group ends with the exact checked-in completion footer.
Missing or truncated footer output stops the invocation as incomplete without
an external measurement command, retry, or later command.

## Live Checkpoint

Long workflows keep one concise checkpoint:

- larger goal and done criteria;
- completed and active phase;
- next genuine authority boundary;
- blockers and remaining local-correction budget;
- Master Audit, Meta Audit, and project-readiness status when relevant.

Use verified live state instead of copying chronology across prompts.
`CODEX_HANDOFF.md` is reserved for genuine thread transfer or a high-risk gate
that depends on persistent continuation state.

## Efficiency Retrospective

Stop and repair the workflow mechanism before repeating it when a phase causes:

- more than two avoidable user round trips;
- more than one preparation-detectable host or tooling failure;
- a handoff-only commit during one live thread; or
- repeated user uncertainty about whether the requested outcome ran.

The remediation belongs in the controlling policy, script, schema, or template.
Do not answer a systemic failure with another one-off continuation prompt.

Success means fewer avoidable approvals, process launches, handoff-only commits,
and repeated diagnostics with unchanged safety and evidence quality.
