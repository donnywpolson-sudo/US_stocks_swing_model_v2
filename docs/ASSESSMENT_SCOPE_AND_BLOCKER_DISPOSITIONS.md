# Assessment Scope and Blocker Dispositions

Technical assessments must distinguish an implementation defect from a
correctly enforced, explicitly disclosed release prerequisite.

## Production prerequisites

The current repository milestone is mechanical historical-research readiness,
not candidate or production readiness. Prospective point-in-time evidence,
the exact legacy-trial census, a committed and GitHub-backed local trial
registration, production eligibility-census materialization, holdout
authorization, and a trust-eligible readiness receipt are deliberately
unresolved.

Those states are acceptance prerequisites and must remain fail-closed. Their
presence is not a defect unless implementation or documentation:

- claims that the prerequisite is satisfied;
- permits a candidate, production inference, provider activation, or trusted
  gate to bypass it; or
- makes the blocker unreachable, mutable, or non-binding.

An assessment may report these prerequisites as readiness limitations. It must
not propose clearing them through repository-only edits, synthetic evidence,
weaker gates, or changed status text. Closure requires the named external or
real-evidence operation under separate authorization.

## Preserved Nasdaq receipt

`config/nasdaq_qualification_receipt.json` is immutable historical acquisition
and negative qualification evidence. It is intentionally `NOT_ACTIVE`, and
its old parser, snapshot-store, and network-registry hashes must remain the
hashes of the event it actually records. Rewriting those hashes to current
code would falsely attest that the historical request ran under controls that
did not yet exist.

The operative source status in `config/sources.json` is
`preserved_snapshot_not_trust_eligible_pending_authenticated_acquisition_receipt`.
The preserved receipt therefore grants no current capability and cannot
activate identity evidence.

A future bounded acquisition must create new owner-operated, locally
integrity-verified evidence. The first trusted count requires the frozen
two-fresh-capture bootstrap in `config/nasdaq_bootstrap_policy.json`; the old
count is comparison-only and is never a bootstrap gate input. The bootstrap
assessment cannot overwrite or cosmetically refresh the preserved receipt and
cannot publish or activate a source. Until a separately authorized snapshot B
capture, offline pair assessment, and later publication are complete, staleness
is an enforced provenance fact and a release limitation, not a remediable
integrity defect. Local integrity does not claim independent provenance.

## Assessment gate

New bypasses, false readiness claims, or broken controls are findings.
Correctly enforced open prerequisites and immutable negative evidence are
reported as limitations. This distinction never authorizes execution or
weakens a gate.
