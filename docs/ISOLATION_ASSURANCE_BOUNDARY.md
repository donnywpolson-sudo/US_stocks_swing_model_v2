# Research and Project-Isolation Assurance Boundary

The repository uses two different control classes. They must not be described
as equivalent.

## Static regression controls

Recursive AST scans detect reviewed imports, names, attributes, string/path
references, dynamic-execution primitives, estimator `fit` calls, and forbidden
state transitions. They are finite source-regression checks over the current
Python tree. Their assurance label is
`STATIC_SOURCE_AST_ONLY_NOT_RUNTIME_PROOF`.

These scans do not form an operating-system sandbox and do not prove
confinement against arbitrary native extensions, generated code, a malicious
same-process monkeypatch, interpreter compromise, or an unreviewed dependency.
Adding more deny-list spellings does not change that boundary.

Package-relative imports are resolved against each file's package depth.
Imports that stay inside `research` remain reviewable package dependencies;
relative imports that would escape `research` are violations. Relative syntax
cannot bypass the prohibited import/capability checks.

Foreign-project path literals are compared as exact separator-delimited path
components after slash normalization. The foreign project may be the first
component (`foreign_project/data`) or an embedded component
(`C:/workspace/foreign_project/data`). Exact project-name constants used by the
reviewed registry and Git-isolation guard are not paths, and prefix-like
components such as `foreign_project_notes` are not treated as the registered
project.

Sensitive imports remain sensitive through local aliases. Calls through aliases
of `eval`, `exec`, `__import__`, `import_module`, or the other reviewed dynamic
execution primitives are violations just like direct references. Unrelated
ordinary aliases remain permitted.

## Runtime capability evidence

The reviewed public synthetic executor supplies phase one with the exact
`OuterBuilderRequest` capability. That object contains fit labels and audit
features but has no outer-audit target field. Frozen predictions are completed
before the separate evaluator receives the corresponding target slice.

Runtime regression tests exercise the public executor and verify:

- dynamic attribute and `vars` inspection of phase-one requests cannot obtain
  `audit_targets` or `outer_audit_targets`;
- the selected synthetic execution path completes while filesystem and network
  entry points are disabled;
- poisoning outer labels cannot change frozen model selection or predictions;
- evaluator and inference artifacts remain fit-free and synthetic/non-alpha
  states cannot become candidate states.

This runtime evidence proves the reviewed public-path data-capability contract,
not general hostile-code confinement. Stronger isolation would require a
separate process or operating-system sandbox design and separate authorization.

## Threat model and claim

Protected assets are outer-fold labels before prediction freeze, outcome and
evaluation artifacts outside their roles, provider/data paths, and non-alpha
state gates. The in-scope adversary is an accidental or reviewed-source
regression using ordinary Python behavior through repository entry points.
Arbitrary code already executing with interpreter or operating-system control
is outside this mechanism’s claim.

Accordingly, readiness may claim only static source regression plus the named
public-path runtime capability tests. It must never claim exhaustive runtime
project isolation.
