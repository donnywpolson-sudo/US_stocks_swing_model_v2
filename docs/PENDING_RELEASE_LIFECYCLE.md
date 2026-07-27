# Pending Release Lifecycle

Pending release directories are failed or interrupted publication evidence.
They are never accepted releases, cannot be selected as inputs, and must not
be promoted or reused directly.

Derived HFDL `.pending-*` pair capsules have the same governed
failure-evidence classification. Although they live beneath the synthetic
publisher's build work root rather than an accepted-release root, they are not
reproducible scratch that may be deleted automatically.

## Detection

After a publication interruption and before retrying the same dataset, the
operator must inventory exact `.pending-*` directories beneath that dataset's
accepted-release root. Detection is read-only and records the dataset,
absolute contained path, detection time, and reason for inspection. It must
not recursively search alternate roots or open protected payload content
without separate authorization.

## Quarantine

The operator uses `AtomicReleasePublisher.quarantine_orphans(dataset)` for the
exact dataset. The operation acquires the dataset release lock, authenticates
each pending path beneath the accepted root, rejects links, and atomically
moves the directory to `.quarantine/<dataset>/`. A quarantine action records:

- the original and quarantine paths;
- the dataset and detection time;
- the responsible operator or automation identity;
- the interruption or failure reason; and
- hashes of evidence that the operator was separately authorized to inspect.

Quarantine does not validate, accept, repair, or authorize the payload.

The synthetic HFDL publisher performs the equivalent operation while holding
its exact build lock. It inventories and hashes the complete pending-capsule
tree, atomically moves it from `pairs/` into the build-local `.quarantine/`
directory, and writes a canonical receipt beside the moved `payload/`. The
receipt binds the original and quarantine paths, payload census and hash,
quarantine time, reason, automation identity, retention state, and prohibition
on direct reuse or promotion. A retry builds a new capsule from authenticated
source staging and never reads a quarantined payload.

## Retention and access

Quarantined payloads are retained indefinitely until the owner explicitly
approves recovery or deletion. There is no age-based or automatic deletion.
Access remains limited to the minimum metadata needed for incident review;
reading protected payload content requires its own bounded authorization.

## Recovery and disposition

Recovery starts a new publication attempt from authenticated source staging
and a newly verified manifest. A quarantined directory is never renamed into
an accepted release and never supplies files to a new attempt.

Deletion is a separate destructive action. It requires an exact owner-approved
target census, evidence-preservation decision, and confirmation that every
target remains inside `.quarantine/<dataset>/`. Until that authorization
exists, the only permitted disposition is continued retention.
