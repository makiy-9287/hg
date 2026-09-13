#!/usr/bin/env python3
"""Static self-check: catch broken references before they hit the runtime.

py_compile only proves the syntax parses. A stale reference to a removed config name compiles
fine and then dies mid-run, which is exactly how the HTF_CANDLES crash got out.
Run this after any edit:  python3 scripts/selfcheck.py
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402


def main() -> int:
    files = [f for f in ROOT.rglob("*.py") if "__pycache__" not in str(f)]
    problems = []

    have = set(dir(config))
    for f in files:
        text = f.read_text()
        rel = f.relative_to(ROOT)
        try:
            ast.parse(text)
        except SyntaxError as e:
            problems.append(f"{rel}:{e.lineno}  syntax error: {e.msg}")
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for name in re.findall(r"\bconfig\.([A-Za-z_][A-Za-z0-9_]*)", line):
                if name not in have:
                    problems.append(f"{rel}:{i}  config.{name} does not exist")

    # every module must import cleanly
    import importlib
    for f in files:
        if f.name == "selfcheck.py" or f.parent.name == "scripts":
            continue
        mod = str(f.relative_to(ROOT)).replace("/", ".")[:-3]
        if mod.endswith(".__init__"):
            mod = mod[:-9]
        try:
            importlib.import_module(mod)
        except Exception as e:
            problems.append(f"{mod}  import failed: {type(e).__name__}: {e}")

    if problems:
        print(f"{len(problems)} problem(s):")
        for p in problems:
            print("  " + p)
        return 1
    print(f"OK — {len(files)} files, all config references resolve, "
          f"all modules import")
    return 0


if __name__ == "__main__":
    sys.exit(main())
