"""Names one module reads from another exist.

A port that copies part of a file leaves such gaps, and they surface only when the one
line that uses the name runs: a build once failed to paint the TAB card while it gave
way to the next, because the window asked the paint package for a name it did not export.
"""

import ast
import importlib
import pathlib
import unittest

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "predoverlay"


def _unknown(path):
    """(line, "module.name") for attributes of imported project modules that do not exist."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = ["predoverlay", *path.relative_to(PACKAGE).parts[:-1]]
    modules, local = {}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = package[: len(package) - (node.level - 1)] if node.level else []
            owner = ".".join(base + ([node.module] if node.module else []))
            if not owner.startswith("predoverlay"):
                continue
            for alias in node.names:
                try:
                    modules[alias.asname or alias.name] = importlib.import_module(
                        f"{owner}.{alias.name}"
                    )
                except ImportError:
                    pass  # a name inside the module, not a submodule
        elif isinstance(node, ast.arg):
            local.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            local.add(node.id)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            name = node.value.id
            if name in modules and name not in local and not hasattr(modules[name], node.attr):
                found.append((node.lineno, f"{name}.{node.attr}"))
    return found


class ExportTests(unittest.TestCase):
    def test_every_name_read_from_another_module_exists(self):
        for path in sorted(PACKAGE.rglob("*.py")):
            with self.subTest(path=str(path.relative_to(PACKAGE))):
                self.assertEqual(_unknown(path), [])


if __name__ == "__main__":
    unittest.main()
