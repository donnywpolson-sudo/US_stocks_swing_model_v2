# Codex Handoff

## Freshness and Authority

- Updated: `2026-07-28`
- Exact root: `C:\Users\donny\Desktop\US_stocks_swing_model_v2`
- Branch: `main`
- State-base commit:
  `8c40890f0edc08f1974925a1cb7a821fd45889fd`
- State-base tree:
  `71bf7d0dc70c03f0fbf4274c7d010e982af3e471`
- Expected worktree: modified only:
  `CODEX_HANDOFF.md`,
  `src/us_stocks_swing_model_v2/master_audit_runner.py`,
  `src/us_stocks_swing_model_v2/cli/run_master_audit.py`, and
  `tests/test_master_audit_runner.py`.

This handoff is coordination context only. `AGENTS.md`, the Constitution,
Harness, current code/configuration/tests, and accepted manifests remain
authoritative.

## Verified Current State

- Highest documented milestone remains
  `HISTORICAL_RESEARCH_READY_MECHANICAL_DISCOVERY_ONLY`, mapping to Master target
  `HISTORICAL_RESEARCH_READY`.
- `MASTER_AUDIT.md` SHA-256 remains
  `9dc6826c4a3a2e5abef8e650c630a094c7f99625b54a0cbea52cc21f20fac64a`.
- `META_MASTER_AUDIT.md` SHA-256 remains
  `15720b3a1c4d4984ff0b9687e17d38503e385eb97d7fcabfd02b0e2a0b3c408a`.
- The runner remediation is validated but uncommitted. It emits immediately
  flushed, content-addressed and hash-chained start/terminal JSONL evidence to
  stderr for all nine steps. Internal steps run in isolated workers under their
  declared timeouts. JSON stdout and validation-only behavior remain preserved.
- Definitive pytest scope remains exact `python -m pytest -q`, explicitly
  including the three `local_evidence` migration tests.
- Targeted synthetic validation passed once: `59 passed` for
  `tests/test_master_audit_runner.py`,
  `tests/test_meta_audit_remediation.py`, and
  `tests/test_research_governance_contract.py`.
- The migration, XNYS, foundation, rebuild, historical-ready, Nasdaq baseline,
  identity, Meta review, and eleven component identities remain unchanged.

## Only Active Gate

`UNAUTHORIZED`: review, stage, and commit exactly the four expected modified
paths. Only after a clean committed state may a newly bound content-addressed
invocation manifest be separately generated and validation-checked.

No manifest generation, calibration, audit execution, report publication,
provider/network request, secret-byte read, data mutation, activation, research,
training, evaluation, prediction, push, or trading is authorized.

## Invalidation

Replace this handoff after any unexpected path, branch, commit, worktree,
accepted evidence, source configuration, validation result, or authorization.
