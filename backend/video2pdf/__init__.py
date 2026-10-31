"""Compatibility package for the historical :mod:`video2pdf` import path.

The canonical implementation is now :mod:`video2md`. This package keeps old
 deployments and third-party scripts working while they migrate imports.
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys

_OLD, _NEW = "video2pdf", "video2md"


class _AliasLoader(importlib.abc.Loader):
    def create_module(self, spec):
        return importlib.import_module(_NEW + spec.name[len(_OLD):])

    def exec_module(self, module):
        sys.modules[module.__spec__.name] = module


class _AliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(_OLD + ".") or fullname == _OLD + ".__main__":
            return None
        if importlib.util.find_spec(_NEW + fullname[len(_OLD):]) is None:
            return None
        return importlib.util.spec_from_loader(fullname, _AliasLoader())


sys.meta_path.insert(0, _AliasFinder())
