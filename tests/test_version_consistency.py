"""The three version sources must never diverge.

The wheel ships the BUNDLED manifest (src/hermes_mobile_plugin/plugin.yaml),
not the root one. If they drift, a released wheel deploys a stale version
(the shadowing-version bug the CI release verify step catches). Pin that
root, bundled, and pyproject all report the same version.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _yaml_version(text: str) -> str:
    m = re.search(r'^version:\s*"?([^"\s]+)"?', text, re.M)
    assert m, "no version line found"
    return m.group(1)


def test_root_bundled_pyproject_versions_agree():
    root = _yaml_version((ROOT / "plugin.yaml").read_text())
    bundled = _yaml_version(
        (ROOT / "src" / "hermes_mobile_plugin" / "plugin.yaml").read_text())
    m = re.search(r'^version\s*=\s*"([^"]+)"',
                  (ROOT / "pyproject.toml").read_text(), re.M)
    assert m, "no version in pyproject.toml"
    py = m.group(1)
    assert bundled == root, (
        f"bundled plugin.yaml ({bundled}) diverges from root ({root}); "
        "the wheel would ship a stale manifest")
    assert py == root, (
        f"pyproject ({py}) diverges from root ({root})")
