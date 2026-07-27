"""AST-only lookup for explicitly configured Python workflow libraries."""

from __future__ import annotations

import ast
from pathlib import Path

from .from_python_script import WorkflowSourceResolver


class WorkflowSourceLibrary:
    """Resolve ``from <module> import <workflow>`` without importing code."""

    def __init__(self, libraries: list[tuple[str, str | Path]]) -> None:
        self._sources: dict[tuple[str, str], Path] = {}
        for module, raw_root in libraries:
            if not module or any(not part.isidentifier() for part in module.split(".")):
                raise ValueError(f"invalid workflow import module: {module!r}")
            root = Path(raw_root).resolve()
            if not root.is_dir():
                raise ValueError(f"workflow source root is not a directory: {root}")
            for path in sorted(root.rglob("*.py")):
                if path.name == "__init__.py" or not path.is_file():
                    continue
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(path))
                for statement in tree.body:
                    if not isinstance(statement, ast.FunctionDef):
                        continue
                    if not any(
                        isinstance(decorator, ast.Call)
                        and isinstance(decorator.func, ast.Name)
                        and decorator.func.id == "workflow_definition"
                        for decorator in statement.decorator_list
                    ):
                        continue
                    key = (module, statement.name)
                    existing = self._sources.get(key)
                    if existing is not None:
                        raise ValueError(
                            f"duplicate workflow function {statement.name!r}: "
                            f"{existing} and {path}"
                        )
                    self._sources[key] = path

    def resolve(self, module: str, symbol: str) -> str | None:
        path = self._sources.get((module, symbol))
        return None if path is None else path.read_text(encoding="utf-8")

    @property
    def resolver(self) -> WorkflowSourceResolver:
        return self.resolve


def parse_workflow_library(value: str) -> tuple[str, Path]:
    """Parse the CLI form ``python.module=/absolute/or/relative/root``."""

    module, separator, raw_root = value.partition("=")
    if not separator or not module or not raw_root:
        raise ValueError(
            "workflow library must use python.module=/path/to/sources"
        )
    return module, Path(raw_root)


__all__ = ["WorkflowSourceLibrary", "parse_workflow_library"]
