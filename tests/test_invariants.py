"""The two architectural invariants, enforced rather than trusted.

Invariant 1: every model call goes through router.py, so no vendor SDK is
imported outside sloane/providers/.
Invariant 2: every SQL statement lives in memory/store.py.

Both are the kind of rule that holds for six months and then quietly breaks in a
hurry at 11 PM. A test is cheaper than the discipline.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "sloane"
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []

VENDOR_MODULES = {"anthropic", "groq", "openai"}
PROVIDERS_DIR = PKG / "providers"
STORE = PKG / "memory" / "store.py"

SQL_VERB = re.compile(
    r"\b(select|insert\s+into|update|delete\s+from|create\s+table|alter\s+table|drop\s+table)\b",
    re.IGNORECASE,
)


def modules() -> list[Path]:
    return sorted(p for p in PKG.rglob("*.py"))


# --- invariant 1 -------------------------------------------------------------
for path in modules():
    if PROVIDERS_DIR in path.parents:
        continue
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            if name.split(".")[0] in VENDOR_MODULES:
                FAILURES.append(
                    f"invariant 1: {path.relative_to(ROOT)} imports {name!r} "
                    f"outside sloane/providers/"
                )

# --- invariant 2 -------------------------------------------------------------
# Only string literals are inspected: a comment mentioning "select" is prose,
# a string containing it is a query.
for path in modules():
    if path == STORE:
        continue
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        text = node.value
        if len(text) < 12:
            continue
        match = SQL_VERB.search(text)
        if match and ("from" in text.lower() or "into" in text.lower() or "set" in text.lower()):
            FAILURES.append(
                f"invariant 2: {path.relative_to(ROOT)}:{node.lineno} contains SQL "
                f"({match.group(0)!r}) outside memory/store.py"
            )

# --- invariant 4: the gate is code, and matches the seeded record ------------
# Imported defensively: an invariant-1 violation makes this import fail, and the
# findings above are more useful than a traceback.
try:
    from sloane.agent import HARD_LINES  # noqa: E402

    schema = (ROOT / "sql" / "001_init.sql").read_text()
    seeded = set(re.findall(r"\('(\w+)',\s*'(\w+)',\s*true,", schema))
    if seeded != set(HARD_LINES):
        FAILURES.append(
            "invariant 4: agent.HARD_LINES and the seeded trust rows disagree\n"
            f"     code: {sorted(HARD_LINES)}\n"
            f"    schema: {sorted(seeded)}"
        )
except Exception as exc:  # noqa: BLE001
    FAILURES.append(f"invariant 4: could not import the hard-line gate: {exc}")

# --- invariant 6: only contract.py constructs a Reply from raw model output ---
for path in modules():
    if path.name in {"contract.py"}:
        continue
    text = path.read_text()
    if "Reply(" in text and "parse(" not in text and path.name not in {"agent.py", "telegram.py"}:
        FAILURES.append(f"invariant 6: {path.relative_to(ROOT)} builds a Reply without parse()")

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print(f"invariants: {len(modules())} modules clean (no vendor SDK leak, no stray SQL, gate matches schema)")
