"""vendor 哨兵（X18）：本仓 acctkit/ 快照必须与 acctkit-core SSOT 逐字节同源。

SSOT= 平台共享仓 ``../../acctkit-core``（工作区约定位；缺席自动 skip——同
label-studio test_vendor_sync / M4 vendor 哨兵先例）。改内核先改 SSOT，
本仓快照随哨兵红跟进；providers.py 允许尾部「本仓扩展自注册」段。
"""
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[3] / "acctkit-core" / "acctkit_core"
_EXT_MARK = "# === 本仓扩展自注册"


def _core(name: str) -> str:
    return (_CORE / name).read_text()


@pytest.mark.skipif(not _CORE.exists(), reason="acctkit-core 仓不在工作区（skip）")
class TestVendorAcctkit:
    def test_kernel_files_byte_identical(self):
        for f in ("models.py", "store.py", "engine.py"):
            vendored = (Path(__file__).resolve().parent.parent / "acctkit" / f).read_text()
            assert vendored == _core(f), f"acctkit/{f} 与 acctkit-core 漂移——以 SSOT 为准回灌"

    def test_providers_prefix_snapshot(self):
        vendored = (Path(__file__).resolve().parent.parent / "acctkit" / "providers.py").read_text()
        core = _core("providers.py")
        assert vendored.startswith(core), "acctkit/providers.py 内核段与 SSOT 漂移"
        assert _EXT_MARK in vendored, "扩展自注册段丢失（GoogleDeviceProvider 注册面）"

    def test_extension_registered(self):
        from acctkit import providers

        assert "google_device" in providers._FACTORIES, "GoogleDevice 工厂未注册"
