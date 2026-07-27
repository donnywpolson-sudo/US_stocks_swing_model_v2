# Outcome Knowledge-Clock Contract

Outcome maturation uses one conservative evidence-view cutoff for every input
needed to construct an outcome. The persisted schema-v1 field remains named
`action_view_as_of` for compatibility; `OutcomeRow.evidence_view_as_of` is its
explicit semantic alias.

The cutoff means: neither a corporate action nor an outcome bar may be used
unless its availability timestamp is at or before this instant. The
release-backed maturation path loads both evidence streams and obtains this
single cutoff from the required production `TrustedClock`. It does not accept a
caller-supplied historical instant.

This unified contract is intentionally conservative:

- an action received after the cutoff is not visible;
- an outcome bar available after the cutoff is not visible;
- equality is accepted because the evidence is available at the cutoff;
- one microsecond before availability fails closed;
- moving the cutoff later may expose newly available evidence, but never
  rewrites an earlier outcome row.

Separate action and bar clocks are not supported by schema v1. Introducing
them would require a new schema and an explicit migration; existing rows must
not be reinterpreted.
