# Codex Handoff

## Freshness and Authority

- Updated: `2026-07-28`
- Exact root: `C:\Users\donny\Desktop\US_stocks_swing_model_v2`
- Branch: `main`
- State-base commit:
  `eba1ca26ac6b60dc2f76045d0a9d766b37660bf0`
- State-base tree:
  `8eeef859f734a369c8bf8cea63a290a66997bc42`
- Expected worktree: only this coordination update to `CODEX_HANDOFF.md`.

This handoff is coordination context only. `AGENTS.md`, the Constitution,
Harness, current code/configuration/tests, and accepted manifests remain
authoritative.

## Verified Current State

- Highest documented milestone remains
  `HISTORICAL_RESEARCH_READY_MECHANICAL_DISCOVERY_ONLY`, mapping to the
  cumulative Master target `HISTORICAL_RESEARCH_READY`.
- `MASTER_AUDIT.md` remains at SHA-256
  `9dc6826c4a3a2e5abef8e650c630a094c7f99625b54a0cbea52cc21f20fac64a`.
- `META_MASTER_AUDIT.md` remains at SHA-256
  `15720b3a1c4d4984ff0b9687e17d38503e385eb97d7fcabfd02b0e2a0b3c408a`.
- The Git object-ID remediation is committed at the state base. Repository
  commit and tree bindings now require exact lowercase 40-character Git SHA-1
  object IDs. All specification, manifest, evidence, file-content, and report
  identities remain exact lowercase SHA-256.
- Focused rejection tests cover empty, uppercase, non-hexadecimal, wrong-length,
  and 64-character Git object IDs while preserving SHA-256 file bindings.
- Targeted synthetic validation passed: `55 passed` for
  `tests/test_master_audit_runner.py`,
  `tests/test_meta_audit_remediation.py`, and
  `tests/test_research_governance_contract.py`.
- No invocation manifest, Master Audit, or audit report was created.
- The previously bound migration, XNYS, foundation, rebuild, historical-ready,
  Nasdaq baseline, identity, Meta review, and eleven component identities
  remain unchanged.

## Only Active Gate

`UNAUTHORIZED`: generate one exact content-addressed Master Audit invocation
manifest against the clean state base, then perform one validation-only,
no-write invocation. The manifest must bind the exact authorities, two
lockfiles, admitted evidence and component manifests, six secret-scan surfaces,
commands, timeouts, and later report policy.

No manifest generation or validation, audit execution, report publication,
provider/network request, secret-byte read, data mutation, activation,
research, training, evaluation, prediction, push, or trading is authorized.

## Invalidation

Replace this handoff after any unexpected path, branch, commit, worktree,
accepted evidence, source configuration, validation result, or authorization.
