from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from us_stocks_swing_model_v2.research import (
    NegativeControlOutcome,
    NegativeControlState,
    PortfolioCharter,
    PortfolioState,
    RobustnessState,
    SleeveState,
    SleeveThresholds,
    SyntheticSleeveMetrics,
    apply_negative_control_indices,
    circular_block_derangement_indices,
    evaluate_negative_controls,
    evaluate_portfolio_mechanics,
    evaluate_synthetic_sleeve,
    make_synthetic_permit,
    synthetic_noise_control,
)


def test_block_derangement_is_shared_deterministic_and_preserves_blocks() -> None:
    indices = circular_block_derangement_indices(
        n_observations=8,
        block_size=2,
        seed=7,
    )
    np.testing.assert_array_equal(
        indices,
        np.asarray([2, 3, 4, 5, 6, 7, 0, 1], dtype=np.int64),
    )
    matrix = np.column_stack(
        (np.arange(8, dtype=np.float64), np.arange(8, dtype=np.float64) + 10.0)
    )
    controlled = apply_negative_control_indices(matrix, indices)
    np.testing.assert_array_equal(controlled[:, 1] - controlled[:, 0], 10.0)


def test_noise_control_is_seeded_float64() -> None:
    first = synthetic_noise_control(shape=(5, 3), seed=91)
    second = synthetic_noise_control(shape=(5, 3), seed=91)
    assert first.dtype == np.float64
    np.testing.assert_array_equal(first, second)


def test_negative_controls_fail_on_leakage_or_incompleteness() -> None:
    clear = evaluate_negative_controls(
        (
            NegativeControlOutcome("block-shift", True, False),
            NegativeControlOutcome("noise", True, False),
        )
    )
    assert clear.state == NegativeControlState.CLEAR

    suspicious = evaluate_negative_controls(
        (NegativeControlOutcome("block-shift", True, True),)
    )
    assert suspicious.state == NegativeControlState.LEAKAGE_SUSPECTED
    assert suspicious.suspicious_controls == ("block-shift",)

    incomplete = evaluate_negative_controls(
        (NegativeControlOutcome("noise", False, False),)
    )
    assert incomplete.state == NegativeControlState.INVALID


def _metrics(*, adjusted_p: float) -> SyntheticSleeveMetrics:
    return SyntheticSleeveMetrics(
        mean_after_costs=0.020,
        confidence_lower_bound=0.015,
        minimum_economically_effective_mean=0.010,
        romano_wolf_adjusted_p=adjusted_p,
        dsr_probability=0.99,
        pbo_conservative=0.10,
        power_sufficient=True,
        negative_controls_clear=True,
        numerically_valid=True,
        robustness_state=RobustnessState.MECHANICS_READY,
    )


def test_sleeves_are_independent_and_portfolio_cannot_cross_subsidize() -> None:
    fixture = np.asarray([[0.0, 1.0], [1.0, 0.0]], dtype=np.float64)
    permit = make_synthetic_permit(fixture, generator_id="sleeve-oracle", seed=4)
    thresholds = SleeveThresholds(
        alpha=0.05,
        dsr_probability_minimum=0.95,
        pbo_conservative_maximum=0.20,
    )
    stock_long = evaluate_synthetic_sleeve(
        sleeve_id="stock_long",
        metrics=_metrics(adjusted_p=0.01),
        thresholds=thresholds,
        permit=permit,
        fixture=fixture,
    )
    with np.testing.assert_raises_regex(ValueError, "exact fixture"):
        evaluate_synthetic_sleeve(
            sleeve_id="stock_long",
            metrics=_metrics(adjusted_p=0.01),
            thresholds=thresholds,
            permit=permit,
            fixture=fixture + 1.0,
        )
    etf_short = evaluate_synthetic_sleeve(
        sleeve_id="etf_short",
        metrics=_metrics(adjusted_p=0.06),
        thresholds=thresholds,
        permit=permit,
        fixture=fixture,
    )
    assert stock_long.state == SleeveState.MECHANICS_READY
    assert etf_short.state == SleeveState.MECHANICS_FAIL_CLOSED
    assert etf_short.failed_gates == ("ROMANO_WOLF",)

    robustness_inconclusive = evaluate_synthetic_sleeve(
        sleeve_id="stock_long",
        metrics=replace(
            _metrics(adjusted_p=0.01),
            robustness_state=RobustnessState.MECHANICS_INCONCLUSIVE,
        ),
        thresholds=thresholds,
        permit=permit,
        fixture=fixture,
    )
    assert (
        robustness_inconclusive.state
        is SleeveState.MECHANICS_INCONCLUSIVE_ROBUSTNESS
    )
    failure_precedes_robustness = evaluate_synthetic_sleeve(
        sleeve_id="etf_short",
        metrics=replace(
            _metrics(adjusted_p=0.06),
            robustness_state=RobustnessState.MECHANICS_INCONCLUSIVE,
        ),
        thresholds=thresholds,
        permit=permit,
        fixture=fixture,
    )
    assert failure_precedes_robustness.state is SleeveState.MECHANICS_FAIL_CLOSED

    with np.testing.assert_raises_regex(ValueError, "explicit real float"):
        evaluate_synthetic_sleeve(
            sleeve_id="stock_long",
            metrics=replace(_metrics(adjusted_p=0.01), mean_after_costs=True),
            thresholds=thresholds,
            permit=permit,
            fixture=fixture,
        )
    with np.testing.assert_raises_regex(ValueError, "exact bool"):
        evaluate_synthetic_sleeve(
            sleeve_id="stock_long",
            metrics=replace(
                _metrics(adjusted_p=0.01), power_sufficient=np.bool_(True)
            ),
            thresholds=thresholds,
            permit=permit,
            fixture=fixture,
        )
    with np.testing.assert_raises_regex(ValueError, "explicit real"):
        evaluate_synthetic_sleeve(
            sleeve_id="stock_long",
            metrics=_metrics(adjusted_p=0.01),
            thresholds=replace(thresholds, alpha=True),
            permit=permit,
            fixture=fixture,
        )

    stock_short = replace(stock_long, sleeve_id="stock_short")
    etf_long = replace(stock_long, sleeve_id="etf_long")
    etf_short_ready = replace(stock_long, sleeve_id="etf_short")
    required_sleeves = ("stock_long", "stock_short", "etf_long", "etf_short")
    all_included = PortfolioCharter.create(
        registered_sleeves=required_sleeves,
        included_sleeves=required_sleeves,
    )
    assert (
        evaluate_portfolio_mechanics(
            all_included,
            (stock_long, stock_short, etf_long, etf_short),
        )
        == PortfolioState.MECHANICS_FAIL_CLOSED
    )
    with np.testing.assert_raises_regex(ValueError, "exact four required sleeves"):
        PortfolioCharter.create(
            registered_sleeves=required_sleeves,
            included_sleeves=required_sleeves[:-1],
        )
    with np.testing.assert_raises_regex(ValueError, "non-empty"):
        PortfolioCharter.create(
            registered_sleeves=required_sleeves,
            included_sleeves=(),
        )
    with np.testing.assert_raises_regex(ValueError, "non-empty"):
        evaluate_portfolio_mechanics(
            PortfolioCharter(
                registered_sleeves=required_sleeves,
                included_sleeves=(),
                charter_hash="0" * 64,
            ),
            (stock_long, stock_short, etf_long, etf_short),
        )
    assert (
        evaluate_portfolio_mechanics(
            all_included,
            (robustness_inconclusive, stock_short, etf_long, etf_short_ready),
        )
        is PortfolioState.MECHANICS_INCONCLUSIVE_ROBUSTNESS
    )

    forged_charter = replace(all_included, charter_hash="0" * 64)
    with np.testing.assert_raises_regex(ValueError, "charter hash"):
        evaluate_portfolio_mechanics(forged_charter, (stock_long, etf_short))

    with np.testing.assert_raises_regex(ValueError, "exact SleeveState"):
        evaluate_portfolio_mechanics(
            all_included,
            (
                replace(stock_long, state="MECHANICS_READY"),
                stock_short,
                etf_long,
                etf_short_ready,
            ),
        )
    with np.testing.assert_raises_regex(ValueError, "terminal sleeve"):
        evaluate_portfolio_mechanics(
            all_included,
            (
                replace(stock_long, state=SleeveState.REGISTERED),
                stock_short,
                etf_long,
                etf_short_ready,
            ),
        )
    with np.testing.assert_raises_regex(ValueError, "non-failed sleeve"):
        evaluate_portfolio_mechanics(
            all_included,
            (
                replace(stock_long, failed_gates=("FORGED",)),
                stock_short,
                etf_long,
                etf_short_ready,
            ),
        )
    with np.testing.assert_raises_regex(ValueError, "identify failed gates"):
        evaluate_portfolio_mechanics(
            all_included,
            (
                stock_long,
                stock_short,
                etf_long,
                replace(etf_short, failed_gates=()),
            ),
        )


ALLOWED_IMPORT_ROOTS = {
    "__future__",
    "collections",
    "dataclasses",
    "enum",
    "hashlib",
    "itertools",
    "json",
    "math",
    "numbers",
    "numpy",
    "re",
    "scipy",
    "typing",
}
FORBIDDEN_DYNAMIC_NAMES = {
    "__import__",
    "compile",
    "eval",
    "exec",
    "getattr",
}
FORBIDDEN_IO_CALL_NAMES = {
    "open",
    "read_bytes",
    "read_csv",
    "read_parquet",
    "read_text",
    "request",
    "urlopen",
    "write_bytes",
    "write_csv",
    "write_parquet",
    "write_text",
}
NDARRAY_IO_CALL_NAMES = {
    "dump",
    "tofile",
}
FORBIDDEN_IMPORT_ROOTS = {
    "aiohttp",
    "http",
    "io",
    "os",
    "pathlib",
    "requests",
    "socket",
    "urllib",
}
FORBIDDEN_IMPORT_PREFIXES = {
    "scipy.io",
}
NUMPY_IO_CALL_NAMES = {
    "DataSource",
    "fromfile",
    "fromregex",
    "genfromtxt",
    "load",
    "loadtxt",
    "memmap",
    "open_memmap",
    "save",
    "savetxt",
    "savez",
    "savez_compressed",
    "tofile",
}
AUTHORIZATION_FIELDS = {
    "alpha_evidence",
    "candidate_eligible",
    "real_history_authorized",
    "candidate_sealing_authorized",
}
FORBIDDEN_TRANSITION_MARKERS = {"historical_pass", "alpha_pass"}
ALLOWED_BOUNDED_GETATTR = {
    ("contracts.py", "self", "name"),
    ("sleeves.py", "metrics", "name"),
}


def _target_names(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        return (node.attr,)
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(
            name for item in node.elts for name in _target_names(item)
        )
    return ()


def _research_capability_violations(
    source: str,
    *,
    relative: str,
) -> tuple[str, ...]:
    tree = ast.parse(source, filename=relative)
    parent_by_node = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    relative_parts = tuple(Path(relative).with_suffix("").parts)
    current_package = list(relative_parts[:-1])
    package_depth = len(current_package) + 1

    def relative_import_path(node: ast.ImportFrom) -> str:
        if node.level == 0:
            return node.module or ""
        if node.level > package_depth:
            return ""
        keep = len(current_package) - (node.level - 1)
        if keep < 0:
            return ""
        parts = ["research", *current_package[:keep]]
        if node.module:
            parts.extend(node.module.split("."))
        return ".".join(parts)

    import_bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".", maxsplit=1)[0]
                imported = (
                    alias.name
                    if alias.asname
                    else alias.name.split(".", maxsplit=1)[0]
                )
                import_bindings[local] = imported
        elif isinstance(node, ast.ImportFrom):
            module = relative_import_path(node)
            for alias in node.names:
                if alias.name != "*" and module:
                    import_bindings[alias.asname or alias.name] = (
                        f"{module}.{alias.name}"
                    )

    def resolved_import_path(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return import_bindings.get(node.id, "")
        if isinstance(node, ast.Attribute):
            base = resolved_import_path(node.value)
            return f"{base}.{node.attr}" if base else ""
        return ""

    def is_library_io_capability(node: ast.AST) -> bool:
        resolved = resolved_import_path(node)
        return (
            (
                resolved.startswith("numpy.")
                and resolved.rsplit(".", maxsplit=1)[-1]
                in NUMPY_IO_CALL_NAMES
            )
            or resolved == "scipy.io"
            or resolved.startswith("scipy.io.")
        )

    violations: set[str] = set()
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            modules = tuple(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                resolved = relative_import_path(node)
                if not resolved:
                    violations.add(
                        f"{relative}:{line}:relative-import-escape:"
                        f"{'.' * node.level}{node.module or ''}"
                    )
                modules = ()
            else:
                modules = (node.module or "",)
        else:
            modules = ()
        for module in modules:
            root = module.split(".", maxsplit=1)[0]
            if (
                root in FORBIDDEN_IMPORT_ROOTS
                or root not in ALLOWED_IMPORT_ROOTS
                or any(
                    module == prefix or module.startswith(f"{prefix}.")
                    for prefix in FORBIDDEN_IMPORT_PREFIXES
                )
            ):
                violations.add(f"{relative}:{line}:import:{module}")

        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                call_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                call_name = node.func.attr
            else:
                call_name = ""
            bounded_getattr = (
                call_name == "getattr"
                and len(node.args) == 2
                and not node.keywords
                and isinstance(node.args[0], ast.Name)
                and isinstance(node.args[1], ast.Name)
                and (
                    relative,
                    node.args[0].id,
                    node.args[1].id,
                )
                in ALLOWED_BOUNDED_GETATTR
            )
            forbidden_dynamic = (
                isinstance(node.func, ast.Name)
                and call_name in FORBIDDEN_DYNAMIC_NAMES
                and not bounded_getattr
            )
            if (
                call_name == "fit"
                or call_name in FORBIDDEN_IO_CALL_NAMES
                or call_name in NDARRAY_IO_CALL_NAMES
                or is_library_io_capability(node.func)
                or forbidden_dynamic
            ):
                violations.add(f"{relative}:{line}:call:{call_name}")
            for keyword in node.keywords:
                if (
                    keyword.arg in AUTHORIZATION_FIELDS
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                ):
                    violations.add(
                        f"{relative}:{line}:authorization:{keyword.arg}"
                    )
        elif isinstance(node, ast.Attribute):
            if (
                node.attr == "fit"
                or node.attr in FORBIDDEN_IO_CALL_NAMES
                or node.attr in NDARRAY_IO_CALL_NAMES
                or is_library_io_capability(node)
            ):
                violations.add(f"{relative}:{line}:reference:{node.attr}")
        elif isinstance(node, ast.Name):
            parent = parent_by_node.get(node)
            parent_is_allowed_call = (
                isinstance(parent, ast.Call)
                and parent.func is node
                and node.id == "getattr"
                and len(parent.args) == 2
                and not parent.keywords
                and isinstance(parent.args[0], ast.Name)
                and isinstance(parent.args[1], ast.Name)
                and (
                    relative,
                    parent.args[0].id,
                    parent.args[1].id,
                )
                in ALLOWED_BOUNDED_GETATTR
            )
            if (
                (
                    node.id in FORBIDDEN_DYNAMIC_NAMES | {"open"}
                    or node.id in NDARRAY_IO_CALL_NAMES
                    or is_library_io_capability(node)
                )
                and not parent_is_allowed_call
            ):
                violations.add(f"{relative}:{line}:reference:{node.id}")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = (
                node.targets if isinstance(node, ast.Assign) else (node.target,)
            )
            value = node.value
            if isinstance(value, ast.Constant) and value.value is True:
                for target in targets:
                    for name in _target_names(target):
                        if name in AUTHORIZATION_FIELDS:
                            violations.add(
                                f"{relative}:{line}:authorization:{name}"
                            )
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and key.value in AUTHORIZATION_FIELDS
                    and isinstance(value, ast.Constant)
                    and value.value is True
                ):
                    violations.add(
                        f"{relative}:{line}:authorization:{key.value}"
                    )
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.casefold() in FORBIDDEN_TRANSITION_MARKERS
        ):
            violations.add(
                f"{relative}:{line}:transition:{node.value.casefold()}"
            )
    return tuple(sorted(violations))


def _scan_research_package(package: Path) -> tuple[str, ...]:
    violations: list[str] = []
    for path in sorted(package.rglob("*.py")):
        violations.extend(
            _research_capability_violations(
                path.read_text(encoding="utf-8"),
                relative=path.relative_to(package).as_posix(),
            )
        )
    return tuple(violations)


def test_research_package_has_no_io_estimator_network_or_alpha_transition() -> None:
    package = (
        Path(__file__).parents[1]
        / "src"
        / "us_stocks_swing_model_v2"
        / "research"
    )
    assert _scan_research_package(package) == ()


@pytest.mark.parametrize(
    "source",
    [
        "import requests as transport",
        "from pathlib import Path as P",
        "reader = frame.read_parquet\nreader('x')",
        "model . fit (values)",
        "loader = getattr(module, 'reader')",
        "exec('open(path)')",
        "state = {'real_history_authorized': True}",
        "Candidate(candidate_sealing_authorized=True)",
        "result = {'alpha_evidence': True}",
        "candidate_eligible = True",
        "state = 'alpha_pass'",
    ],
)
def test_research_capability_ast_rejects_aliases_and_format_variants(
    source: str,
) -> None:
    assert _research_capability_violations(
        source,
        relative="fixture.py",
    )


@pytest.mark.parametrize(
    ("source", "relative"),
    [
        ("from ..providers import alpaca\n", "builder.py"),
        ("from ...providers import alpaca\n", "nested/escape.py"),
    ],
)
def test_research_capability_ast_rejects_relative_import_escape(
    source: str,
    relative: str,
) -> None:
    violations = _research_capability_violations(
        source,
        relative=relative,
    )
    assert any(":relative-import-escape:" in item for item in violations)


@pytest.mark.parametrize(
    ("source", "relative"),
    [
        ("from .contracts import finite_float64\n", "builder.py"),
        ("from ..contracts import finite_float64\n", "nested/helper.py"),
    ],
)
def test_research_capability_ast_allows_package_relative_imports(
    source: str,
    relative: str,
) -> None:
    assert _research_capability_violations(
        source,
        relative=relative,
    ) == ()


@pytest.mark.parametrize(
    "source",
    [
        "import numpy as np\nvalues = np.load('history.npy')",
        "import numpy as numbers\nnumbers.save('history.npy', values)",
        "from numpy import memmap as mapped\nvalues = mapped('history.bin')",
        "from numpy import fromfile\nvalues = fromfile('history.bin')",
        "import numpy.lib.format as fmt\nvalues = fmt.open_memmap('history.npy')",
        "import numpy as np\nvalues = np.asarray([1.0])\nvalues.tofile('history.bin')",
        "import numpy as np\nvalues = np.asarray([1.0])\nvalues.dump('history.pkl')",
        "import scipy.io as sio\nsio.savemat('history.mat', {'x': [1.0]})",
        "from scipy import io as sio\nsio.savemat('history.mat', {'x': [1.0]})",
        "from scipy.io import savemat as write_mat\nwrite_mat('history.mat', {'x': [1.0]})",
    ],
)
def test_research_capability_ast_rejects_resolved_library_io(
    source: str,
) -> None:
    assert _research_capability_violations(
        source,
        relative="fixture.py",
    )


def test_research_capability_ast_allows_pure_numpy_computation() -> None:
    source = (
        "import numpy as np\n"
        "values = np.asarray([1.0, 2.0])\n"
        "centered = values - np.mean(values)\n"
    )
    assert _research_capability_violations(
        source,
        relative="fixture.py",
    ) == ()


def test_research_capability_scan_recurses_into_subpackages(tmp_path: Path) -> None:
    package = tmp_path / "research"
    nested = package / "nested"
    nested.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (nested / "escape.py").write_text(
        "import urllib.request\n",
        encoding="utf-8",
    )
    assert any("nested/escape.py" in item for item in _scan_research_package(package))


def test_bounded_getattr_exemption_is_exact_call_node_only() -> None:
    allowed = "value = getattr(self, name)\n"
    assert _research_capability_violations(
        allowed,
        relative="contracts.py",
    ) == ()

    mixed = "value = getattr(self, name)\nalias = getattr\n"
    violations = _research_capability_violations(
        mixed,
        relative="contracts.py",
    )
    assert "contracts.py:2:reference:getattr" in violations
