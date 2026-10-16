"""The pocketchat page's static guarantees (spec c34): no HTML injection sinks, no inline
script or style, nothing loaded from another origin."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "pocketsync" / "web" / "static"
FILES = ("index.html", "app.css", "app.js", "icon.svg")


@pytest.mark.parametrize("name", FILES)
def test_static_file_ships(name):
    assert (STATIC / name).is_file()


@pytest.mark.parametrize(
    "sink",
    ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"],
)
def test_app_js_has_no_html_or_code_sinks(sink):
    assert sink not in (STATIC / "app.js").read_text()


def test_index_has_no_inline_script_or_style():
    html = (STATIC / "index.html").read_text()
    for script in re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S):
        assert script.strip() == ""
    assert "<style" not in html
    assert not re.search(r"\sstyle=", html)
    assert not re.search(r"\son[a-z]+=", html)


@pytest.mark.parametrize("name", FILES)
def test_nothing_loads_from_another_origin(name):
    text = (STATIC / name).read_text()
    urls = re.findall(r"https?://[^\s\"')]+", text)
    assert [u for u in urls if not u.startswith("http://www.w3.org/")] == []
