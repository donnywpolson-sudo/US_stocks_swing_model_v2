import ast
from collections import Counter
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.common import require_contained_path
from us_stocks_swing_model_v2.errors import ContractError


FOREIGN_PROJECT_IDENTIFIERS = (
    "futures_rebuild",
    "futures_intraday_model",
    "futures_intraday_model_v2",
)
APPROVED_FOREIGN_LITERAL_COUNTS: dict[tuple[str, str, str], int] = {}


def _matches_foreign_identifier(value: str) -> bool:
    normalized = value.casefold()
    return any(
        normalized == identifier
        or normalized.startswith(f"{identifier}.")
        or identifier in normalized
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


def test_source_tree_has_only_approved_futures_project_references() -> None:
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


@pytest.mark.parametrize(
    "source",
    [
        "import futures_rebuild",
        "from futures_intraday_model import engine as alias",
        "value = futures_intraday_model_v2.runtime",
        "path = 'C:/foreign/futures_intraday_model/data'",
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
    "foreign_name", ("futures_intraday_model", "futures_intraday_model_v2")
)
def test_active_containment_guard_rejects_futures_roots(foreign_name: str) -> None:
    active = Path(__file__).resolve().parents[1]
    foreign = Path.home() / "Desktop" / foreign_name / "forbidden-write.json"
    with pytest.raises(ContractError, match="escapes its approved root"):
        require_contained_path(foreign, active, must_exist=False)
