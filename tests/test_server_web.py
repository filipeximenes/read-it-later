"""Tests for serving the reader itself.

The page is no longer one file: it links stylesheets and ES modules that the
server has to hand out, and that a wheel has to carry. These cover both, plus
the rule that keeps `/static/` from reaching anything else.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ril.server import build_app


@pytest.fixture
def client(tmp_path):
    return TestClient(build_app(tmp_path))


def _linked_assets(html: str) -> set[str]:
    import re

    return set(re.findall(r"/static/([A-Za-z0-9_.-]+)", html))


def test_page_links_only_assets_that_are_served(client):
    html = client.get("/").text
    linked = _linked_assets(html)
    assert linked, "the page should link its stylesheets and modules"
    for name in linked:
        assert client.get(f"/static/{name}").status_code == 200, name


def test_modules_import_only_modules_that_are_served(client):
    """Every `import … from './x.js'` has to resolve, or the page is dead."""
    import re

    to_visit = _linked_assets(client.get("/").text)
    seen: set[str] = set()
    while to_visit:
        name = to_visit.pop()
        if name in seen:
            continue
        seen.add(name)
        response = client.get(f"/static/{name}")
        assert response.status_code == 200, name
        for imported in re.findall(r"from '\./([A-Za-z0-9_.-]+)'", response.text):
            assert client.get(f"/static/{imported}").status_code == 200, imported
            to_visit.add(imported)


def test_assets_carry_their_content_type(client):
    assert client.get("/static/app.css").headers["content-type"].startswith("text/css")
    assert client.get("/static/main.js").headers["content-type"].startswith("text/javascript")


@pytest.mark.parametrize(
    "name",
    [
        "../server.py",
        "..%2Fserver.py",
        "index.html",
        "config.py",
        "nothing-here.js",
        ".env.js",
        "Main.js",
    ],
)
def test_static_serves_nothing_else(client, name):
    assert client.get(f"/static/{name}").status_code == 404
