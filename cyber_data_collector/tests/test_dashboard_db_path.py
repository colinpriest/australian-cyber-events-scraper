"""Regression: dashboard helpers must read the database the caller passed.

Four OAIC helpers were called without ``db_path`` and silently opened the
default instance/cyber_events.db, so building from any other database left the
Source of Breaches, Time-to-Identify/Notify and related charts empty.
"""
from __future__ import annotations

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "scripts" / "build_static_dashboard.py"


def test_build_dashboard_file_passes_db_path_to_every_helper_that_takes_it():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    takes_db = {
        f.name for f in ast.walk(tree)
        if isinstance(f, ast.FunctionDef)
        and any(a.arg == "db_path" for a in f.args.args + f.args.kwonlyargs)
    }
    build = next(f for f in ast.walk(tree)
                 if isinstance(f, ast.FunctionDef) and f.name == "build_dashboard_file")
    missing = []
    for call in ast.walk(build):
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in takes_db:
            passed = (any(isinstance(a, ast.Name) and a.id == "db_path" for a in call.args)
                      or any(k.arg == "db_path" for k in call.keywords))
            if not passed:
                missing.append(f"{call.func.id} (line {call.lineno})")
    assert not missing, f"called without db_path: {missing}"
