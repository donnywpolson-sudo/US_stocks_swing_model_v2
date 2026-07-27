# Executable Dependency Closure

The supported runtime is the repository-pinned Windows CPython 3.11.9
environment. Its executable project dependency closure is the complete set of
22 distributions listed identically in:

- `requirements.lock`, which pins normalized distribution names and versions;
- `requirements.sha256.lock`, which pins the same names and versions to exact
  supported wheel hashes.

`config/environment.lock.json` authenticates the bytes of both files and their
exact distribution count. Runtime validation parses both authenticated locks,
requires their normalized name/version maps to agree, and compares every
locked distribution with installed package metadata. A missing distribution or
any direct or transitive version drift fails closed.

The closure is not a claim that every package installed in a developer's global
Python environment belongs to the project. Unrelated global tools may coexist,
but they cannot substitute for or change any locked distribution. Project
dependencies, test dependencies, build requirements, and their complete
resolved transitive closure are all in scope.

Changing a dependency, version, wheel, hash, supported Python runtime, platform,
or closure count requires separately reviewed lock and environment-contract
updates. Runtime validation never resolves, downloads, installs, or rewrites
dependencies.
