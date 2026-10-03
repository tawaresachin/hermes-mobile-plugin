"""Keep the README install pin honest.

The README documents the install with a pinned tag reference
(`hermes-mobile-plugin@vX.Y.Z`). The release flow updates nothing
else in the README, so the pin silently goes stale on every release
— the exact mismatch caught by hand after v0.0.19. The auto-tag
workflow runs :func:`sync_readme_pin` after each new release so the
docs on ``main`` always point at the newest tag.
"""

import re
import sys
from pathlib import Path

# every "hermes-mobile-plugin@v<digits...>" pin in the docs. The ``v``
# prefix is part of the tag; the version part matches the tag grammar
# used by auto-tag.yml (digits, dots, dashes, letters after the first
# digit — so a stale pin never regex-matches a newer one by accident).
_PIN_RE = re.compile(r"(hermes-mobile-plugin@)v[0-9][0-9A-Za-z.\-]*")


def _normalize_tag(tag: str) -> str:
    tag = str(tag).strip()
    return tag if tag.startswith("v") else f"v{tag}"


def sync_readme_pin(readme: Path, tag: str) -> dict:
    """Rewrite every install pin in *readme* to *tag* (e.g. ``v0.0.20``).

    Idempotent: a README already pointing at *tag* reports
    ``updated: False`` and rewrites nothing. Never raises on a missing
    file — returns a report instead, so CI must not die on docs.

    Returns::

        {"readme": str, "tag": str, "pins": int, "updated": bool,
         "error": str | None}
    """
    tag = _normalize_tag(tag)
    report: dict = {"readme": str(readme), "tag": tag, "pins": 0,
                    "updated": False, "error": None}
    try:
        text = readme.read_text(encoding="utf-8")
    except OSError:
        report["error"] = "README not found"
        return report
    new_text, n = _PIN_RE.subn(lambda m: m.group(1) + tag, text)
    report["pins"] = n
    if n and new_text != text:
        readme.write_text(new_text, encoding="utf-8")
        report["updated"] = True
    return report


def main(argv=None) -> int:
    """Console entry (used by the auto-tag workflow): one argument, the
    target tag. Operates on README.md in the current directory."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or not args[0].strip():
        print("usage: python -m hermes_mobile_plugin.readme_pin <tag>",
              file=sys.stderr)
        return 2
    report = sync_readme_pin(Path("README.md"), args[0])
    if report["error"]:
        print(f"⚠️  {report['error']} — nothing done")
        return 0
    if report["updated"]:
        print(f"📝  README: {report['pins']} install pin(s) -> {report['tag']}")
    else:
        print(f"✅  README install pin already at {report['tag']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
