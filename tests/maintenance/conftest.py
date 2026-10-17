"""Test setup for ``core.api.maintenance``.

The library is pure stdlib, so the suite also runs on a bare host without the
core's dependencies (``pytest tests/maintenance`` from the repo root). Inside
the dev core (``PYTHONPATH=/app``, tests copied anywhere) the regular package
import is used — ``core.services`` first, which breaks the core's import cycle.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types


def _find_library() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve()
    candidates = [here.parents[2] / "app"] + [pathlib.Path(p) for p in sys.path if p]
    for base in candidates:
        path = base / "core" / "api" / "maintenance.py"
        if path.is_file():
            return path
    raise ImportError(
        "core/api/maintenance.py not found next to the tests or on sys.path"
    )


def _ensure_module() -> None:
    try:
        import core.services  # noqa: F401  (core import order, see CLAUDE.md)
    except Exception:  # pragma: no cover - host without the core stack
        pass
    try:
        import core.api.maintenance  # noqa: F401

        return
    except Exception:  # pragma: no cover - host without the core stack
        pass
    path = _find_library()
    spec = importlib.util.spec_from_file_location("core.api.maintenance", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    for name in ("core", "core.api"):
        if name not in sys.modules:
            stub = types.ModuleType(name)
            stub.__path__ = []  # type: ignore[attr-defined]
            sys.modules[name] = stub
    sys.modules["core.api.maintenance"] = module
    spec.loader.exec_module(module)
    setattr(sys.modules["core.api"], "maintenance", module)


_ensure_module()
