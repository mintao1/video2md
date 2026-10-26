"""改名兼容垫片：video2md 已改名为 video2pdf。

`import video2md.xxx` 解析为同一个 `video2pdf.xxx` 模块对象（不重复加载、不复制状态），
旧部署清单里的 `python -m video2md ...` 与历史脚本仍可运行；新代码请直接用 video2pdf。
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys

_OLD, _NEW = "video2md", "video2pdf"


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
