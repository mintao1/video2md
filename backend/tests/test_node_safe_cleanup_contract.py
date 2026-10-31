"""Guard the deploy-side disk cleanup boundary.

The cleanup may remove rebuildable Docker caches only; production data and
rollback state remain outside its deletion surface.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/node-safe-cleanup.sh"


def test_node_cleanup_prunes_only_rebuildable_caches() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "docker builder prune" in text
    assert "docker image prune" in text
    assert "KEEP_HOURS=$((KEEP_DAYS * 24))" in text
    assert "--filter \"until=${KEEP_HOURS}h\"" in text
    assert "docker system prune" not in text
    assert "docker volume prune" not in text
    assert "k3s ctr images rm" in text
    assert "KEEP_ROLLOUTS" in text
    assert "keep set unavailable" in text


def test_node_cleanup_keeps_production_paths_out_of_delete_commands() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    for protected in ("/app/data", "/vault", "secrets", "backups"):
        assert protected not in text
    assert "video2md-images-*" in text
