#!/usr/bin/env python3
"""
build_html.py - copy the standard templates into netgen.html.

netgen.html is the offline browser interface: one file, no server, no network.
It carries its own copy of templates/ios-xe/*.j2 and templates/ios-xr/*.j2.
Run this script after changing a template, so the HTML uses the new version:

    python build_html.py

Standard library only. It rewrites the block between
<script type="application/json" id="std-templates"> and </script> in netgen.html,
and nothing else.
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HTML = HERE / "netgen.html"
OPEN = '<script type="application/json" id="std-templates">'


def templates_json():
    """{"ios-xe": {"main.j2": "...", ...}, "ios-xr": {...}} as safe JSON for an HTML page."""
    data = {}
    for os_dir in sorted((HERE / "templates").iterdir()):
        if os_dir.is_dir():
            data[os_dir.name] = {f.name: f.read_text(encoding="utf-8").replace("\r\n", "\n")
                                 for f in sorted(os_dir.glob("*.j2"))}
    text = json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True)
    return text.replace("</", "<\\/")      # never close the <script> tag early


def main():
    html = HTML.read_text(encoding="utf-8")
    pattern = re.compile(re.escape(OPEN) + r".*?</script>", re.S)
    if not pattern.search(html):
        print(f"{HTML.name}: template block not found, nothing changed.")
        return 1
    new = pattern.sub(lambda m: OPEN + templates_json() + "</script>", html, count=1)
    if new == html:
        print(f"{HTML.name} is already up to date.")
        return 0
    HTML.write_text(new, encoding="utf-8", newline="\n")
    print(f"{HTML.name} updated with the templates of templates/.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
