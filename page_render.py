"""
page_render.py — inline the shared page parts, at serve time and at build time.

The pages are single self-contained files by design: a static host wants one
file, and an external script would either be fetched after first paint (the
theme flash) or add a request that can fail on its own. But keeping them
self-contained by *copying* the shared plumbing into each one is how those
copies drifted apart in the first place.

So the shared parts live in one place and are inlined into the marker
comments below. Flask does it when serving a page and build_web.py does it
when writing web/, through this same function, so the two cannot disagree.
"""

from __future__ import annotations

import os

HERE = os.path.dirname(os.path.abspath(__file__))

HEAD_MARKER = "<!-- shared:head -->"
JS_MARKER = "<!-- shared:js -->"
CSS_MARKER = "<!-- shared:styles -->"
SHELL_MARKER = "<!-- shared:shell -->"

HEAD_FILE = os.path.join(HERE, "shared_head.html")
JS_FILE = os.path.join(HERE, "shared_desk.js")
CSS_FILE = os.path.join(HERE, "shared_styles.css")
SHELL_FILE = os.path.join(HERE, "shared_shell.html")


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def render(html: str) -> str:
    """Replace the shared markers with their content."""
    if HEAD_MARKER in html:
        html = html.replace(HEAD_MARKER, _read(HEAD_FILE).strip(), 1)
    if CSS_MARKER in html:
        html = html.replace(CSS_MARKER, "<style>\n" + _read(CSS_FILE).rstrip() + "\n</style>", 1)
    if SHELL_MARKER in html:
        html = html.replace(SHELL_MARKER, _read(SHELL_FILE).strip(), 1)
    if JS_MARKER in html:
        html = html.replace(
            JS_MARKER, "<script>\n" + _read(JS_FILE).rstrip() + "\n</script>", 1)
    return html


def render_file(path: str) -> str:
    return render(_read(path))
