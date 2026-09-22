#!/usr/bin/env python3
"""仓库根直跑 shim：实现已搬进 video2md.evals（容器内 python -m video2md.evals，
因为 .dockerignore 排除 scripts/）。参数见 `python3 scripts/eval_notes.py --help`。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from video2md.evals import main

sys.exit(main())
