"""Every import of a stateful frontend module must use one specifier.

A browser treats `./settings.js` and `./settings.js?v=…` as two modules, so
module-level state (init flags, caches) is duplicated and DOM listeners get
wired twice — see the double "Test reminder" pushes and the cookbook note in
static/app.js.
"""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "static"
IMPORT = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(\s*|<script[^>]*\bsrc=)['"]([^'"]*/(settings|cookbook)\.js(?:\?[^'"]*)?)['"]"""
)


def test_singleton_modules_are_imported_without_cache_busting_query():
    offenders = []
    for path in [STATIC / "index.html", STATIC / "app.js", *sorted((STATIC / "js").rglob("*.js"))]:
        for match in IMPORT.finditer(path.read_text(encoding="utf-8")):
            if "?" in match.group(1):
                offenders.append(f"{path.relative_to(STATIC)}: {match.group(1)}")
    assert offenders == []
