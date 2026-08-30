"""Sibling-import closure over `scripts/`, computed once for every caller.

The scripts in this repo import each other by bare module name after putting
their own directory on `sys.path`, so "what does module X drag in" is a real
question with a mechanical answer, and two tests now need it: the deploy
manifest must be import-closed (a profile copy missing a sibling fails to
import, which kills the cron that uses it), and the observability guard must
bound the sibling imports its three named roots have.

`closure()` is the transitive form and has one caller today, the manifest test.
The observability guard deliberately uses `sibling_imports` alone and does not
token-scan the closure: reaching a module is not using its execution surface,
and that sweep failed on an import of one path helper (PR #60). What it pins
instead is the import EDGE.

One implementation, deliberately — two copies of one computation agree only
until one of them changes.
"""

from __future__ import annotations

import ast
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def sibling_imports(module: str, scripts: Path = SCRIPTS) -> set[str]:
    """Names under `scripts/` that `module` imports directly.

    Only bare, top-level names are considered, because that is the only form
    the sibling-import convention produces: `from scripts import x` is a
    package import that a profile copy never uses, and a relative import
    cannot appear in a file that is also run as a script.
    """
    path = scripts / module
    if not path.is_file():
        return set()
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        for name in names:
            head = name.split(".", 1)[0]
            if (scripts / f"{head}.py").is_file():
                found.add(f"{head}.py")
    return found


def closure(roots: list[str] | set[str] | tuple[str, ...], scripts: Path = SCRIPTS) -> set[str]:
    """`roots` plus everything they transitively import from `scripts/`."""
    seen: set[str] = set()
    pending = list(roots)
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        pending.extend(sibling_imports(module, scripts) - seen)
    return seen
