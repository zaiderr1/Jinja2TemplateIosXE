"""Checks for netgen.html (the offline browser interface). Standard library only.

The HTML carries its own copy of the templates: these tests fail if a template
was changed without running  python build_html.py  afterwards, or if the page
could reach the network.
"""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "netgen.html"


class HtmlInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.html = HTML.read_text(encoding="utf-8")

    def test_embedded_templates_match_templates_folder(self):
        block = re.search(r'<script type="application/json" id="std-templates">(.*?)</script>', self.html, re.S)
        self.assertIsNotNone(block, "template block missing in netgen.html")
        embedded = json.loads(block.group(1))
        for os_dir in (ROOT / "templates").iterdir():
            if not os_dir.is_dir():
                continue
            for f in os_dir.glob("*.j2"):
                self.assertEqual(embedded[os_dir.name][f.name], f.read_text(encoding="utf-8").replace("\r\n", "\n"),
                                 f"{os_dir.name}/{f.name} changed: run python build_html.py")

    def test_no_network_possible(self):
        self.assertIn("default-src 'none'", self.html)
        self.assertIn("connect-src 'none'", self.html)
        for pattern in (r"https?://", r"\bfetch\(", r"XMLHttpRequest", r"WebSocket", r"sendBeacon"):
            self.assertIsNone(re.search(pattern, self.html), f"unexpected {pattern} in netgen.html")


if __name__ == "__main__":
    unittest.main()
