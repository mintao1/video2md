"""The published bundle gate must follow what index.html actually loads, not source files."""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts/check_frontend_bundle.mjs"
MARKERS = "const a='https://www.kaggle.com/account/login';const b='登录 Kaggle';"


def _dist(tmp_path: Path, entry: str, assets: dict[str, str]) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(f'<script type="module" crossorigin src="/assets/{entry}"></script>')
    for name, body in assets.items():
        (dist / "assets" / name).write_text(body)
    return dist


def _gate(dist: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["node", str(GATE), str(dist)], capture_output=True, text=True)


def test_bundle_gate_accepts_markers_in_entry_or_lazy_chunk(tmp_path):
    direct = _gate(_dist(tmp_path / "direct", "index-new.js", {"index-new.js": MARKERS}))
    assert direct.returncode == 0, direct.stderr
    lazy = _gate(_dist(tmp_path / "lazy", "index-new.js", {
        "index-new.js": "const p=()=>import('./Settings-x1.js');",
        "Settings-x1.js": MARKERS,
    }))
    assert lazy.returncode == 0, lazy.stderr


def test_bundle_gate_rejects_stale_entry_even_if_a_fresh_bundle_sits_beside_it(tmp_path):
    result = _gate(_dist(tmp_path, "index-old.js", {"index-old.js": "console.log(1)", "index-new.js": MARKERS}))
    assert result.returncode != 0
    assert "missing required marker" in result.stderr


def test_bundle_gate_rejects_source_only_fix_without_markers_in_dist(tmp_path):
    result = _gate(_dist(tmp_path, "index-new.js", {"index-new.js": "const a='https://www.kaggle.com/';"}))
    assert result.returncode != 0
    assert "missing required marker" in result.stderr
