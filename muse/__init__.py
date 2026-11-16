"""Compatibility shim. Import :mod:`wardmuse` instead.

Importing this package emits :class:`DeprecationWarning`. Submodules such as
``muse.cli`` and ``muse.paths`` are the real :mod:`wardmuse` modules, so
existing imports and the ``muse`` console script keep working.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import sys
import warnings

warnings.warn(
    "The 'muse' package has been renamed to 'wardmuse'. "
    "Import wardmuse, or run the 'wardmuse' command. "
    "The 'muse' name is a compatibility alias.",
    DeprecationWarning,
    stacklevel=2,
)


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, real: str) -> None:
        self._real = real

    def create_module(self, spec):  # noqa: ANN001
        module = importlib.import_module(self._real)
        sys.modules[spec.name] = module
        return module

    def exec_module(self, module) -> None:  # noqa: ANN001
        return None

    def get_code(self, fullname: str):
        """Let ``python -m muse...`` run the real module's code."""
        real = "wardmuse" + fullname[len("muse") :]
        spec = importlib.util.find_spec(real)
        loader = getattr(spec, "loader", None) if spec is not None else None
        get_code = getattr(loader, "get_code", None)
        if get_code is None:
            raise ImportError(f"cannot run {fullname}")
        return get_code(real)

    def get_source(self, fullname: str):
        real = "wardmuse" + fullname[len("muse") :]
        spec = importlib.util.find_spec(real)
        loader = getattr(spec, "loader", None) if spec is not None else None
        get_source = getattr(loader, "get_source", None)
        if get_source is None:
            return None
        return get_source(real)


class _AliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):  # noqa: ANN001
        if not fullname.startswith("muse."):
            return None
        real = "wardmuse" + fullname[len("muse") :]
        real_spec = importlib.util.find_spec(real)
        if real_spec is None:
            return None
        is_pkg = real_spec.submodule_search_locations is not None
        spec = importlib.machinery.ModuleSpec(
            fullname,
            _AliasLoader(real),
            is_package=is_pkg,
            origin=getattr(real_spec, "origin", None),
        )
        if is_pkg:
            spec.submodule_search_locations = list(real_spec.submodule_search_locations or [])
        return spec


if not any(finder.__class__.__name__ == "_AliasFinder" for finder in sys.meta_path):
    sys.meta_path.insert(0, _AliasFinder())

import wardmuse as _wardmuse  # noqa: E402


def __getattr__(name: str):
    try:
        return getattr(_wardmuse, name)
    except AttributeError:
        return importlib.import_module(f"wardmuse.{name}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_wardmuse)))
