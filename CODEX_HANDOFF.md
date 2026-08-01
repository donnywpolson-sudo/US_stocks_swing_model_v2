# Codex Handoff

This file is optional, transfer-only coordination context. It is not proof,
authorization, or a running log. In an ordinary same-thread workflow, do not
read or update it.

## Current State

No active cross-session handoff is recorded. Reconcile Git, the applicable
contracts, and the user request before acting.

## When To Use This File

Replace this file only for a genuine fresh-thread transfer, context loss, or an
external/high-risk gate that needs durable coordination. Keep it under 450
words and include only the current repository identity, verified state, one
active gate, forbidden actions, and invalidation conditions. Do not use it for
routine progress, continuation prompts, or handoff-only commits.
