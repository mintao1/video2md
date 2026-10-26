"""pytest 收集口径统一：把 backend/（包根）放进 sys.path。

package=false 项目——video2pdf/acctkit 不进 venv，pytest 跑在 backend/ 下时
tests/ 目录会被自动插入 sys.path，但包在上一级；此处补上。
脚本式测试（uv run python3 tests/test_x.py）不经 conftest，各自头部有独立引导。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
