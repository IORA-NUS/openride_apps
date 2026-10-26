"""Locks that the CLI carries no hand-copied policy lists (plan §4 P0.9).

``openride/config.py`` once held ``SOLVERS``/``DEFAULT_SOLVER`` (whose default
disagreed with the engine's) and the datagen ``POLICIES``, fed to argparse
``choices=``. Names now come from the engine (``known-solvers``/``known-policies``),
so a newly registered policy is selectable without a CLI edit.
"""

from __future__ import annotations

import ast
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parents[1] / "openride"
FORBIDDEN_CONFIG_NAMES = {"SOLVERS", "DEFAULT_SOLVER", "POLICIES", "DEFAULT_POLICIES"}


def _assigned_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
    return names


def test_cli_config_assigns_no_policy_lists():
    """Verified to have teeth: re-adding ``SOLVERS = ["GreedyNearest"]`` to
    ``openride/config.py`` failed this test."""
    tree = ast.parse((CLI_DIR / "config.py").read_text())
    leaked = _assigned_names(tree) & FORBIDDEN_CONFIG_NAMES
    assert not leaked, f"openride/config.py hand-copies policy lists again: {sorted(leaked)}"


def _choices_from_config(path: Path) -> list[str]:
    hits = []
    for node in ast.walk(ast.parse(path.read_text())):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            continue
        for kw in node.keywords:
            if kw.arg != "choices":
                continue
            for sub in ast.walk(kw.value):
                if (isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name)
                        and sub.value.id == "config"):
                    hits.append(f"{path.name}:{node.lineno} choices=config.{sub.attr}")
    return hits


def test_no_argparse_choices_read_from_cli_config():
    """Verified to have teeth: restoring ``choices=config.SOLVERS`` on ``--solver`` in
    ``run_group.py`` failed this test."""
    files = sorted(CLI_DIR.glob("*.py"))
    assert files, f"no CLI modules found under {CLI_DIR}"
    hits = [hit for path in files for hit in _choices_from_config(path)]
    assert not hits, f"argparse choices still read from openride/config.py: {hits}"
