# Agent Workflow Examples

`AGENTS.md` is binding and contains the canonical task-routing and gate
checklist. This guide explains ordinary workflow examples without repeating its
scientific, source, secret, generated-data, destructive-action, revalidation,
or approval controls.

## One outcome, one local phase

Treat a request to pursue, continue, or implement an outcome as one plan to its
next real durable-state or external gate. A repository-local implementation
request includes routine discovery, edits, focused validation, static checks,
read-only verification, and up to two evidenced corrective cycles. Do not ask
for serial “do that” approvals during those steps.

Before adding a module, policy, abstraction, or control, identify the concrete
outcome or required safeguard it improves. Prefer the simplest robust approach
that produces verified progress; surface complexity is not evidence of quality.

After an authorized commit, do the safe no-network, no-write planning needed to
identify the next real gate. Do not detour into non-blocking hardening or
cleanup unless it blocks the outcome or the user includes it in scope.

## Use The Canonical Checklist

Classify the work with the [canonical task-routing and gate checklist](../AGENTS.md#canonical-task-routing-and-gate-checklist)
before choosing an ordinary workflow example.

A direct “Yes” or “Run that gate” applies only to the uniquely identifiable
preceding gate after live-state revalidation. Never require copied plan IDs,
hashes, commands, or authorization text.

For repeated known units, use one campaign gate with its full census, limits,
verification, and stop-on-first-failure rule. Unknown-evidence-dependent work
remains a separate gate.

## Prepare once, then run

Before a bounded invocation, verify the repository, executable, transport,
targets, outputs, timeout, and failure class with metadata-only inputs. Use the
fewest safe deterministic batches. Content-address a plan only when the
applicable contract requires it. A preparation failure stops before the
substantive invocation and does not spend it.

For generated-data staging, calibrate file and byte limits before the first real
attempt. Audit reviewer transport and completion-footer rules belong only to
[`AUDIT_WORKFLOW.md`](AUDIT_WORKFLOW.md).

## Keep context light

Use verified live state rather than copied chronology. Keep only the current
outcome, blocker (or `none`), and next meaningful phase. Use
[`CODEX_HANDOFF.md`](../CODEX_HANDOFF.md) only for a genuine fresh-thread
transfer, context loss, or high-risk gate that needs durable coordination.

After two avoidable clarification or approval exchanges without a genuine
boundary, state the outcome, blocker, and next real gate once, then continue
the active local phase. Do not replace progress with another continuation prompt
or handoff update.

## Read the specialist guide only when needed

- Provider, source, acquisition, or publication work:
  [`NETWORK_ACQUISITION.md`](NETWORK_ACQUISITION.md).
- Historical-research planning, evaluation, or claims:
  [`HISTORICAL_RESEARCH_HARNESS.md`](HISTORICAL_RESEARCH_HARNESS.md).
- Audit preparation, invocation, or reporting:
  [`AUDIT_WORKFLOW.md`](AUDIT_WORKFLOW.md).

These guides add task-specific controls; they do not authorize execution.
