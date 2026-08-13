import ast
from collections import Counter
from pathlib import Path
import re

import pytest

from us_stocks_swing_model_v2.common import require_contained_path
from us_stocks_swing_model_v2.errors import ContractError


FOREIGN_PROJECT_IDENTIFIERS = (
    "futures_rebuild",
    "futures_intraday_model",
    "futures_intraday_model_v2",
    "us_stocks_swing_model",
)
APPROVED_FOREIGN_LITERAL_COUNTS: dict[tuple[str, str, str], int] = {}
LOCAL_IMPORT_ALLOWLISTS = {
    "causal_foundation.py": {
        "bounded_universe",
        "common",
        "corporate_actions",
        "errors",
        "identity",
    },
    "outcome_firewall.py": {"capabilities", "common", "errors"},
    "research/builder.py": {"research.artifacts", "research.contracts"},
    "research/evaluator.py": {"research.artifacts", "research.contracts"},
    "feature_release.py": {"errors", "releases", "schemas"},
    "outcomes.py": {
        "calendar",
        "common",
        "corporate_actions",
        "errors",
        "releases",
        "schemas",
    },
    "inference.py": {
        "bundle",
        "capabilities",
        "clock",
        "common",
        "eligibility",
        "errors",
        "feature_release",
        "ledger",
        "schemas",
    },
}


def _matches_foreign_identifier(value: str) -> bool:
    normalized = value.casefold()
    return any(
        re.search(
            rf"(?<![a-z0-9_]){re.escape(identifier)}(?![a-z0-9_])",
            normalized,
        )
        is not None
        for identifier in FOREIGN_PROJECT_IDENTIFIERS
    )


def _literal_assignment_context(
    node: ast.Constant,
    parents: dict[ast.AST, ast.AST],
) -> str:
    def scope(assignment: ast.AST) -> str:
        current_scope = assignment
        while current_scope in parents:
            current_scope = parents[current_scope]
            if isinstance(current_scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return f"function:{current_scope.name}"
            if isinstance(current_scope, ast.ClassDef):
                return f"class:{current_scope.name}"
        return "module"

    current: ast.AST = node
    shape = "direct"
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.Tuple, ast.List, ast.Set)):
            shape = type(current).__name__.casefold()
        elif isinstance(current, ast.BinOp) and isinstance(current.op, ast.Div):
            shape = "path"
        elif isinstance(current, ast.Assign):
            if len(current.targets) == 1 and isinstance(current.targets[0], ast.Name):
                return (
                    f"{scope(current)}:assignment:"
                    f"{current.targets[0].id}:{shape}"
                )
            return "assignment:non_simple"
        elif isinstance(current, ast.AnnAssign):
            if isinstance(current.target, ast.Name):
                return (
                    f"{scope(current)}:assignment:"
                    f"{current.target.id}:{shape}"
                )
            return "assignment:non_simple"
        elif isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            break
    return "unbound"


def _project_isolation_observations(
    source: str,
    *,
    relative: str,
) -> tuple[tuple[str, ...], Counter[tuple[str, str, str]]]:
    tree = ast.parse(source, filename=relative)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    violations: list[str] = []
    approved: Counter[tuple[str, str, str]] = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _matches_foreign_identifier(alias.name):
                    violations.append(f"{relative}:{node.lineno}:import:{alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if _matches_foreign_identifier(node.module):
                violations.append(f"{relative}:{node.lineno}:from:{node.module}")
        elif isinstance(node, ast.Name) and _matches_foreign_identifier(node.id):
            violations.append(f"{relative}:{node.lineno}:name:{node.id}")
        elif isinstance(node, ast.Attribute) and _matches_foreign_identifier(node.attr):
            violations.append(f"{relative}:{node.lineno}:attribute:{node.attr}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            normalized = node.value.casefold()
            if normalized in FOREIGN_PROJECT_IDENTIFIERS:
                context = _literal_assignment_context(node, parents)
                key = (relative, context, normalized)
                if key in APPROVED_FOREIGN_LITERAL_COUNTS:
                    approved[key] += 1
                else:
                    violations.append(
                        f"{relative}:{node.lineno}:literal:{context}:{normalized}"
                    )
            elif _matches_foreign_identifier(normalized):
                violations.append(
                    f"{relative}:{node.lineno}:literal:{normalized}"
                )
    return tuple(violations), approved


def _local_imports(source: str, *, relative: str) -> set[str]:
    tree = ast.parse(source, filename=relative)
    module_parts = list(Path(relative).with_suffix("").parts)
    package_parts = module_parts[:-1]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                prefix = "us_stocks_swing_model_v2."
                if alias.name.startswith(prefix):
                    found.add(alias.name.removeprefix(prefix))
        elif isinstance(node, ast.ImportFrom) and node.level:
            keep = len(package_parts) - (node.level - 1)
            if keep < 0:
                found.add("<relative-import-escape>")
                continue
            resolved = [*package_parts[:keep]]
            if node.module:
                resolved.extend(node.module.split("."))
                found.add(".".join(resolved))
            else:
                found.update(".".join((*resolved, alias.name)) for alias in node.names)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and (
                node.module == "us_stocks_swing_model_v2"
                or node.module.startswith("us_stocks_swing_model_v2.")
            )
        ):
            if node.module == "us_stocks_swing_model_v2":
                found.update(alias.name for alias in node.names)
            else:
                found.add(node.module.removeprefix("us_stocks_swing_model_v2."))
    return found


def test_source_tree_has_only_approved_foreign_project_references() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src"
    violations: list[str] = []
    approved: Counter[tuple[str, str, str]] = Counter()
    for path in sorted(source_root.rglob("*.py")):
        relative = path.relative_to(source_root).as_posix()
        found, allowed = _project_isolation_observations(
            path.read_text(encoding="utf-8"),
            relative=relative,
        )
        violations.extend(found)
        approved.update(allowed)
    assert violations == []
    assert approved == Counter(APPROVED_FOREIGN_LITERAL_COUNTS)


def test_builder_evaluator_feature_outcome_and_inference_import_boundaries() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src" / "us_stocks_swing_model_v2"
    for relative, allowed in LOCAL_IMPORT_ALLOWLISTS.items():
        source = (source_root / relative).read_text(encoding="utf-8")
        assert _local_imports(source, relative=relative) <= allowed


@pytest.mark.parametrize(
    "source",
    (
        "from .outcomes import build_outcome\n",
        "from us_stocks_swing_model_v2 import outcomes\n",
    ),
)
def test_local_import_boundary_detects_cross_layer_import(source: str) -> None:
    observed = _local_imports(
        source,
        relative="inference.py",
    )
    assert observed == {"outcomes"}
    assert not observed <= LOCAL_IMPORT_ALLOWLISTS["inference.py"]


@pytest.mark.parametrize(
    "source",
    [
        "import futures_rebuild",
        "from futures_intraday_model import engine as alias",
        "value = futures_intraday_model_v2.runtime",
        "path = 'C:/foreign/futures_intraday_model/data'",
        "import US_stocks_swing_model",
        "from US_stocks_swing_model.runtime import loader",
        "module = __import__('US_stocks_swing_model.runtime')",
        "path = 'C:/Users/donny/Desktop/US_stocks_swing_model/data'",
    ],
)
def test_project_isolation_detects_import_name_attribute_and_path_references(
    source: str,
) -> None:
    violations, approved = _project_isolation_observations(
        source,
        relative="fixture.py",
    )
    assert violations
    assert approved == Counter()


def test_project_isolation_rejects_all_foreign_literals() -> None:
    violations, approved = _project_isolation_observations(
        'foreign = "futures_rebuild"', relative="fixture.py"
    )
    assert violations
    assert approved == Counter()


@pytest.mark.parametrize(
    "source",
    [
        "import us_stocks_swing_model_v2",
        "from us_stocks_swing_model_v2 import releases",
        "path = 'C:/Users/donny/Desktop/US_stocks_swing_model_v2/src'",
    ],
)
def test_project_isolation_does_not_match_the_active_v2_project(source: str) -> None:
    violations, approved = _project_isolation_observations(
        source,
        relative="fixture.py",
    )
    assert violations == ()
    assert approved == Counter()


@pytest.mark.parametrize(
    "foreign_name",
    (
        "futures_intraday_model",
        "futures_intraday_model_v2",
        "US_stocks_swing_model",
    ),
)
def test_active_containment_guard_rejects_foreign_roots(foreign_name: str) -> None:
    active = Path(__file__).resolve().parents[1]
    foreign = Path.home() / "Desktop" / foreign_name / "forbidden-write.json"
    with pytest.raises(ContractError, match="escapes its approved root"):
        require_contained_path(foreign, active, must_exist=False)
