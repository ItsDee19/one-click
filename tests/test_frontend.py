"""Frontend integration checks without Flask, network calls, or a browser.

Run with: python -B -m unittest discover -s tests -p "test_*.py" -v
The production build is generated only inside a temporary directory. Node is
optional for JavaScript syntax and shared asynchronous request checks.
"""

from collections import Counter
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
import build_web  # noqa: E402
import page_render  # noqa: E402

NODE = shutil.which("node")
MARKERS = (page_render.HEAD_MARKER, page_render.JS_MARKER,
           page_render.CSS_MARKER, page_render.SHELL_MARKER)
BUILD_BLOCK = re.compile(re.escape(build_web.MARKER) + r"\s*<script>.*?</script>\n?", re.S)


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.scripts = []
        self._script = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))
        if tag == "script":
            self._script = []

    def handle_data(self, data):
        if self._script is not None:
            self._script.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._script is not None:
            self.scripts.append("".join(self._script))
            self._script = None


class FrontendIntegrationTests(unittest.TestCase):
    def rendered_pages(self):
        for source, output in build_web.PAGES:
            yield source, output, page_render.render_file(str(ROOT / source))

    def test_every_page_inlines_all_shared_components(self):
        for source, _, html in self.rendered_pages():
            with self.subTest(page=source):
                original = (ROOT / source).read_text(encoding="utf-8")
                for marker in MARKERS:
                    self.assertEqual(original.count(marker), 1, marker)
                    self.assertNotIn(marker, html)
                self.assertIn((ROOT / "shared_desk.js").read_text(encoding="utf-8").rstrip(), html)
                self.assertIn((ROOT / "shared_styles.css").read_text(encoding="utf-8").rstrip(), html)
                self.assertEqual(page_render.render(html), html, "Rendering an expanded page must be idempotent")

    def test_documents_have_unique_ids_and_working_semantic_targets(self):
        for source, _, html in self.rendered_pages():
            with self.subTest(page=source):
                doc = Document(html)
                ids = Counter(attrs["id"] for _, attrs in doc.elements if "id" in attrs)
                self.assertEqual([key for key, count in ids.items() if count > 1], [])
                self.assertEqual(sum(tag == "main" for tag, _ in doc.elements), 1)
                self.assertEqual(sum(tag == "h1" for tag, _ in doc.elements), 1)
                self.assertTrue(any(tag == "html" and attrs.get("lang") for tag, attrs in doc.elements))
                for tag, attrs in doc.elements:
                    if tag == "label" and attrs.get("for"):
                        self.assertIn(attrs["for"], ids, "Label must target a real input")
                    for relation in ("aria-controls", "aria-labelledby", "aria-describedby"):
                        for target in attrs.get(relation, "").split():
                            self.assertIn(target, ids, relation + " must target a real element")
                    if tag == "a" and attrs.get("href", "").startswith("#"):
                        if attrs.get("id") == "desk-skip":
                            # The shared shell chooses #board for the overview
                            # at runtime; the Node suite executes this wiring.
                            self.assertTrue("board" in ids or "main-content" in ids)
                            continue
                        self.assertIn(attrs["href"][1:], ids, "In-page navigation target must exist")
                links = {attrs.get("href") for tag, attrs in doc.elements if tag == "a" and "data-desk" in attrs}
                self.assertEqual(links, {"/", "/intraday-desk", "/ipo-desk", "/quality-desk"})

    def test_pages_need_no_remote_font_or_script_downloads(self):
        for source, _, html in self.rendered_pages():
            with self.subTest(page=source):
                doc = Document(html)
                for tag, attrs in doc.elements:
                    if tag == "script":
                        self.assertNotIn("src", attrs, "The frontend is served as a self-contained document")
                    if tag == "link" and attrs.get("rel") in ("stylesheet", "preconnect", "dns-prefetch"):
                        self.assertFalse(re.match(r"(?:https?:)?//", attrs.get("href", "")), attrs)
                self.assertNotRegex(html, r"fonts\.(?:googleapis|gstatic)\.com")
                self.assertNotRegex(html, r"@import\s+(?:url\()?['\"]?(?:https?:)?//")

    def test_static_build_matches_runtime_render_for_every_route(self):
        with tempfile.TemporaryDirectory(prefix="dalal-frontend-") as directory:
            with patch.object(build_web, "OUT_DIR", directory):
                paths = build_web.build("https://api.example.invalid/")
            self.assertEqual(len(paths), len(build_web.PAGES))
            for source, output, html in self.rendered_pages():
                with self.subTest(page=source):
                    built = (Path(directory) / output).read_text(encoding="utf-8")
                    self.assertEqual(len(BUILD_BLOCK.findall(built)), 1)
                    self.assertEqual(BUILD_BLOCK.sub("", built), html)
                    self.assertIn('window.__API_BASE__ = "https://api.example.invalid";', built)

    def test_checked_in_static_pages_are_current(self):
        for source, output, html in self.rendered_pages():
            with self.subTest(page=source):
                built = (ROOT / "web" / output).read_text(encoding="utf-8")
                self.assertEqual(BUILD_BLOCK.sub("", built), html,
                                 "Re-run build_web.py after source or shared component changes")

    def test_api_base_is_data_and_cannot_create_an_extra_script(self):
        urls = [
            'https://api.example.invalid/</script><script>globalThis.injected=true</script>',
            'https://api.example.invalid/quoted"value\\path\nline\u2028separator\u2029paragraph',
        ]
        original = Document(page_render.render_file(str(ROOT / "dashboard.html")))
        with tempfile.TemporaryDirectory(prefix="dalal-inline-") as directory:
            with patch.object(build_web, "OUT_DIR", directory):
                for value in urls:
                    with self.subTest(value=value):
                        path = build_web.build_page("dashboard.html", "index.html", value)
                        html = Path(path).read_text(encoding="utf-8")
                        doc = Document(html)
                        self.assertEqual(len(doc.scripts), len(original.scripts) + 1,
                                         "An API value must not escape the generated script")
                        block = BUILD_BLOCK.search(html).group(0)
                        match = re.search(r"window\.__API_BASE__\s*=\s*(.+);", block)
                        self.assertIsNotNone(match)
                        self.assertEqual(json.loads(match.group(1)), value)
                        self.assertNotIn("<script>globalThis.injected", block)

    def test_replacing_a_build_block_preserves_literal_backslashes(self):
        value = r"https://api.example.invalid/\g<1>\1"
        old = "<html><head>" + build_web.MARKER + '<script>window.__API_BASE__="old";</script></head><body></body></html>'
        with tempfile.TemporaryDirectory(prefix="dalal-rebuild-") as directory:
            with patch.object(build_web, "OUT_DIR", directory), patch.object(page_render, "render_file", return_value=old):
                output = build_web.build_page("dashboard.html", "index.html", value)
            html = Path(output).read_text(encoding="utf-8")
            self.assertEqual(html.count(build_web.MARKER), 1)
            assignment = re.search(r"window\.__API_BASE__\s*=\s*(.+);", html)
            self.assertEqual(json.loads(assignment.group(1)), value)

    def test_build_rejects_non_http_backend_schemes(self):
        for value in ("javascript:alert(1)", "file:///etc/passwd", "data:text/html,hello", "//api.example.invalid"):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                build_web.build(value)

    @unittest.skipUnless(NODE, "Node is needed to parse JavaScript")
    def test_every_rendered_inline_script_has_valid_javascript(self):
        scripts = [{"name": source + ":script:" + str(i), "source": script}
                   for source, _, html in self.rendered_pages()
                   for i, script in enumerate(Document(html).scripts)]
        runner = "const vm=require('node:vm');const scripts=" + json.dumps(scripts) + ";for(const s of scripts)new vm.Script(s.source,{filename:s.name});"
        result = subprocess.run([NODE], input=runner, text=True, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(NODE, "Node is needed for async request tests")
    def test_shared_request_and_loading_lifecycle(self):
        result = subprocess.run([NODE, str(ROOT / "tests" / "shared_request.test.cjs")],
                                text=True, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(NODE, "Node is needed for dashboard behavior tests")
    def test_dashboard_filters_polling_and_run_lifecycle(self):
        result = subprocess.run([NODE, str(ROOT / "tests" / "test_dashboard_behavior.cjs")],
                                text=True, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
