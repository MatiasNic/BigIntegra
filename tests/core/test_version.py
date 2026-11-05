import re
from pathlib import Path

from app import __version__

ROOT = Path(__file__).resolve().parents[2]


def test_version_has_changelog_entry():
    """Cada versión entregada debe tener su sección en CHANGELOG.md."""
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{__version__}]" in changelog


def test_version_is_not_hardcoded_elsewhere():
    for rel in ("pyproject.toml", "packaging/BigIntegra_Desktop.spec", "main.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert not re.search(rf"\b{re.escape(__version__)}\b", text), rel
