# Codex Handoff

## Freshness and Authority

- Updated: `2026-07-29T11:36:24-07:00`
- Root: `C:\Users\donny\Desktop\US_stocks_swing_model_v2`
- Branch: `main`
- State-base commit: `bcb390e6fe8fb86683d10203a34efc70b24c7a4d`
- State-base tree: `d78b8479ff2f43ba4005bf815fdaa455608d2bb8`
- Expected worktree: clean at the state base. If separately approved, the only coordination delta is replacement of `CODEX_HANDOFF.md`.

This handoff is coordination context, not evidence or execution authority. `AGENTS.md`, current implementation/tests, and accepted manifests remain binding.

## Verified Current State

- Highest documented milestone remains `HISTORICAL_RESEARCH_READY_MECHANICAL_DISCOVERY_ONLY`, mapping to Master target `HISTORICAL_RESEARCH_READY`.
- Master SHA-256: `9dc6826c4a3a2e5abef8e650c630a094c7f99625b54a0cbea52cc21f20fac64a`.
- Meta SHA-256: `15720b3a1c4d4984ff0b9687e17d38503e385eb97d7fcabfd02b0e2a0b3c408a`.
- Ignored invocation manifest SHA-256 `e929dc21c1f0f872e961f3d54c3d5d4c69f82d7072a4ca0dec6d809598663093`, manifest ID `c0554b6587407e0768059f015b90039a2e2f38c1e236567adbf3dcfc41ec7f3d`, is spent and must never be rerun.
- Execution `b12f56746672baab2cff14e5871508d6ef4e16dc513d148be9ca3c4f58c47485` passed steps 1-4; step 5 timed out after 600.110 seconds with record `8e7e4182227037041377c9a2ff64ecf54cfc4db814a2203911ca4c14b2c4ccf5`. Steps 6-9 did not run; no report exists.
- The single stdin diagnostic retry timed out at 600.012 seconds with no mutation or retry. It measured HFDL verifications of 137.842 and 152.826 seconds, 9,065 Parquet reads consuming 25.204 seconds, 591 symbol derivations, and 1,181 downstream derivations. The assessor did not complete; readiness remains unproven.
- Current code confirms both duplicated paths: `_verify_foundation` verifies HFDL before `load_hfdl_historical_foundation` verifies it again; `_derive_symbol_tables` derives downstream tables that the loader discards before deriving them again from the observed causal table. Existing focused tests use two-symbol fixtures and do not assert these call counts.
- Python 3.11.9, all 16 accepted-release manifest hashes, and all 11 component-manifest hashes remain matched.

## Only Active Gate

`UNAUTHORIZED`: a semantics-preserving remediation limited to `src/us_stocks_swing_model_v2/mechanical_readiness.py`, `src/us_stocks_swing_model_v2/historical_foundation.py`, `tests/test_mechanical_readiness.py`, and `tests/test_historical_foundation_bridge.py`. It must reuse one verified HFDL result while preserving the exact readiness-receipt-to-bridge HFDL binding; derive causal bars from source once; derive feature/outcome inputs once from the verified observed causal table; preserve every schema, equality, census, provenance, release, and fail-closed check; and add focused one-HFDL-verification and one-downstream-derivation-per-pair regression coverage.

No handoff replacement, implementation edit, test, diagnostic, spent-manifest execution, timeout increase, manifest generation, report publication, commit, push, provider/network activity, secret read, data mutation, activation, research, training, evaluation, prediction, or trading is authorized until separately approved.

## Invalidation

Replace this handoff after any unexpected path, branch, commit, worktree, accepted evidence, source configuration, validation result, diagnostic result, or authorization.
