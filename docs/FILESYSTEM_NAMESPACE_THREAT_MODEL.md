# Filesystem Namespace Threat Model

## Supported operating boundary

The pinned deployment is Windows CPython on a local NTFS volume owned by one
trusted operating-system principal. Concurrent project writers are
cooperative: they use the repository's `ExclusiveFileLock`, content-addressed
identities, exact-tree verification, pending directories, and atomic rename
publication.

Within that boundary the controls:

- reject traversal, alternate data streams, device aliases, links, junctions,
  and reparse points at reviewed path boundaries;
- keep one-writer operations under an approved-root lock;
- authenticate a lock's descriptor/path identity before release;
- on Windows, deny delete sharing while a lock is held and retire the exact
  open handle;
- verify hashes, payload censuses, and manifests again when evidence is loaded.

These controls address accidental path mistakes, cooperating-process races,
partial writes, replay, and leaf-lock substitution. They fail closed when the
authenticated lock pathname or evidence identity changes.

## Explicit exclusion

A hostile process running as the same Windows account and able to rename or
replace arbitrary ancestor directory entries concurrently is outside this
assurance boundary. Python's portable `pathlib` and `os` interfaces do not
provide a complete Windows descriptor-relative traversal and mutation API.
Repeated `resolve` or `stat` calls can narrow a race but cannot prove
continuous namespace identity.

That exclusion is not a production-readiness waiver. Such a process can also
modify project code, interpreter state, keys, or authenticated configuration.
Deployments requiring protection from it must use a separately authorized
isolation design: a dedicated service identity, restrictive ACLs, a protected
volume, and native handle-relative operations for every protected read and
mutation.

No project document or readiness state may describe the current pathname
checks as hostile same-principal confinement or an operating-system sandbox.
