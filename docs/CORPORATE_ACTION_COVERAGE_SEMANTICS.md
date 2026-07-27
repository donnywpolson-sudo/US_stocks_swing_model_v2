# Corporate-Action Coverage Semantics

Corporate-action acquisition coverage and effective-event completeness are
different evidence and cannot substitute for one another.

## Provider acquisition evidence

`CorporateActionCoverageEvidence` schema 2 records
`PROVIDER_PROCESS_DATE_ACQUISITION_ONLY`. Its bounds are named
`process_date_start` and `process_date_end` because they bind the exact
provider request. They prove that the bounded, fully paginated response for
those process dates was landed. They do not prove that every action effective
in the same calendar interval was returned.

The requested process-date end cannot follow the UTC date of the trusted
acquisition clock, and completed coverage cannot extend past the UTC date of
its latest landed page. Equality is allowed. These checks prevent future dates
from being represented as completed acquisition evidence without changing the
separate effective-event semantics.

An action may have a process date inside the request and an effective date
outside it, or may arrive later as a correction. Parsing preserves both dates
and never promotes the process-date interval into causal completeness.
When one provider row supplies both `ex_date` and `effective_date`, both must
be canonical ISO dates and must identify the same session. A disagreement is
ambiguous effective-event evidence and rejects the response; neither field is
silently preferred.

The authorization-bound `requested_at` is acquisition chronology, not an
arbitrary report label. Before any corporate-action request attempt is issued,
it must be no later than the trusted clock and no more than 15 minutes old.
This is the same Alpaca request-freshness window used for daily bars. Future or
stale requests fail before transport authorization is consumed.

Provider rows may name more than one security for mergers, spin-offs,
reorganizations, or symbol changes. `CorporateActionEvidence.involved_symbols`
preserves the sorted unique census of every recognized participant field while
`symbol` retains the provider's primary `symbol` when present. The immutable
landed row and its hash preserve the provider's role-specific fields. A
symbol-scoped response is accepted only when at least one preserved participant
belongs to the exact request; missing or malformed participant text still
fails closed.

## Outcome completeness evidence

`CorporateActionCoverage` records
`EFFECTIVE_EVENT_COMPLETENESS`. Its bounds are
`effective_start_session` and `effective_end_session`. Outcome construction
requires this exact evidence for the complete entry-through-exit interval,
the asset census, the applicable as-of time, and the accepted source release.

There is no automatic conversion from provider process-date coverage to
effective-event completeness. `prepare_effective_event_coverage` preserves
the complete provider acquisition receipt and creates a non-operative
candidate bound to the proposed effective interval, asset census, accepted
release epoch, reviewed provider-contract hash, and reviewed late-arrival
policy hash. `authorize_effective_event_coverage` turns that candidate into
release evidence only after an exact current
`AUTHORIZE_EFFECTIVE_EVENT_COMPLETENESS` receipt validates against the pinned
authority. `build_governed_corporate_action_release_payload` is the sole
schema-v5 payload builder: it revalidates every authorization, rejects missing
or duplicate coverage, and requires every action's source epoch, snapshot,
asset, and effective session to fall within one exact governed coverage row.
The verified-release loader independently repeats this row-to-coverage check;
an authenticated payload cannot gain trust merely because a different action
or interval elsewhere in the release is covered.

External authority is required for network-as-received provider evidence.
Synthetic authority is accepted only with synthetic provider evidence and
remains non-authorizing mechanics. Because the production authority registry
is currently `NOT_CONFIGURED`, no production completeness receipt can be
issued or loaded; outcomes continue to fail closed as `MISSING_SOURCE`.

Corporate-action release schemas 2, 3, and 4 are rejected: schema 2 has
generic coverage bounds, schema 3 lacks a container-bound coverage identity,
and schema 4 lacks governed completeness authorization. Schema 5 retains the
complete process-date acquisition receipt, contract and late-arrival policy
hashes, and signed completeness authorization. Its effective-event content ID
is authorization-bound before publication; the loader then derives the
operative coverage ID from that content ID plus the verified release ID and
source epoch. Identical authorized evidence in two releases therefore has the
same content ID but distinct operative IDs. Schema 1 remains readable as
immutable historical action rows, but is explicitly not trust eligible,
supplies no coverage, and therefore cannot mature an outcome.
