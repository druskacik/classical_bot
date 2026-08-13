from __future__ import annotations

import ast
from typing import Any


def module_literal_constants(tree: ast.Module) -> dict[str, Any]:
    constants: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target = node.target
            value = node.value
        else:
            continue
        if not isinstance(target, ast.Name):
            continue
        try:
            constants[target.id] = ast.literal_eval(value)
        except (ValueError, TypeError):
            constants.pop(target.id, None)
    return constants


def resolve_module_literal(node: ast.expr, constants: dict[str, Any]) -> Any:
    if isinstance(node, ast.Name):
        if node.id not in constants:
            raise ValueError(f"unresolved module constant: {node.id}")
        return constants[node.id]
    return ast.literal_eval(node)
