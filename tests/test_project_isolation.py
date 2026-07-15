import ast
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.common import require_contained_path
from us_stocks_swing_model_v2.errors import ContractError


def test_source_tree_has_no_futures_project_import() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src"
    violations: list[str] = []
    for path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                modules = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = (node.module,)
            for module in modules:
                if module.startswith("futures_rebuild"):
                    violations.append(f"{path.relative_to(source_root)}:{node.lineno}:{module}")
    assert violations == []


@pytest.mark.parametrize(
    "foreign_name", ("futures_intraday_model", "futures_intraday_model_v2")
)
def test_active_containment_guard_rejects_futures_roots(foreign_name: str) -> None:
    active = Path(__file__).resolve().parents[1]
    foreign = Path.home() / "Desktop" / foreign_name / "forbidden-write.json"
    with pytest.raises(ContractError, match="escapes its approved root"):
        require_contained_path(foreign, active, must_exist=False)
