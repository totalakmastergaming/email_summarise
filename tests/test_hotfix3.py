"""Hotfix round 3 regression tests — 2026-11-16.

ROOT CAUSE (live 08:46–08:48 IST, deploy dep-dbt7ab3bc2fs73daocc0,
commit fb2e20b6 confirmed live): the Urgent card showed count 2 with
subtext "Nothing marked urgent" and the "Urgent — handle first"
section said "Nothing urgent right now." — even though round 2 had
fixed both texts in static/js/app.js.

The text logic was NOT the bug. Verified against GitHub: fb2e20b6's
app.js HAS the round-2 fix ("open Urgent to see them"); fc3a8fa7's
app.js does NOT (its subtext was pool-only:
``firstUrgent ? "Priority: ..." : "Nothing marked urgent"``).
The live browser ran STALE JS: app/main.py told browsers to cache
/js/* for an hour (``Cache-Control: public, max-age=3600``) and
static/index.html loaded ``js/app.js`` with no version query, so the
08:46 check — ~15 minutes after the 08:31 check that loaded
fc3a8fa7's app.js — served the cached copy without revalidating.
Every backend-driven round-2 fix showed live (draft autosave,
awaiting-reply exclusion, counts); the one JS-only change did not.
The card number (2) and the subtext disagreed in fc3a8fa7 because
the number already came from INBOX_COUNTS while the subtext was
pool-only (Primary page 1 holds no urgent thread).

Fix (this round):
1. Static assets are served ``no-cache, must-revalidate`` instead of
   ``max-age=3600`` (app/main.py add_cache_headers).
2. The dashboard HTML versions its asset URLs with ``?v=<commit>``
   (RENDER_GIT_COMMIT on Render, app.js mtime locally), so each
   deploy is a new URL even for a warm cache
   (app/main.py _asset_version / serve_dashboard).
3. The urgent text rule is extracted into two pure helpers
   (urgentSubtext / urgentSectionHtml in static/js/app.js) and used
   by both render sites, so the rule is directly testable.

Tests here:
1. GET /js/app.js is 200 and is NOT cached for an hour.
2. GET / serves HTML with versioned js/css URLs.
3. The Node suite tests/test_hotfix3.js (extracts the helpers from
   the shipped app.js, asserts count>0 / count==0 behaviour and
   call-site wiring) exits 0.

OFFLINE ONLY: nothing here proves live behaviour on Render. The
re-verification browser must load the NEW URL (or hard-refresh):
the copy cached at 08:46 under max-age=3600 stays fresh until
~09:46 IST, and no server header can evict a fresh cache entry.

Run from the repo root with:
    /tmp/es-test-venv/bin/python -m pytest tests/test_hotfix3.py -v
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.chdir(REPO_ROOT)  # app.main mounts StaticFiles with relative dirs

from app.main import app
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    # No DB is touched by GET / or GET /js/*; plain TestClient (no
    # context entry, so the lifespan/scheduler never starts).
    return TestClient(app, raise_server_exceptions=False)


class TestStaticCacheBusting:
    def test_js_not_cached_for_an_hour(self, client):
        r = client.get("/js/app.js")
        assert r.status_code == 200
        cc = r.headers.get("cache-control", "")
        assert "no-cache" in cc, f"expected no-cache, got: {cc!r}"
        assert "max-age=3600" not in cc, f"stale 1h cache header still present: {cc!r}"

    def test_css_not_cached_for_an_hour(self, client):
        r = client.get("/css/style.css")
        assert r.status_code == 200
        cc = r.headers.get("cache-control", "")
        assert "no-cache" in cc, f"expected no-cache, got: {cc!r}"
        assert "max-age=3600" not in cc

    def test_dashboard_html_versions_assets(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")
        m_js = re.search(r"js/app\.js\?v=([^\"'&\s]+)", r.text)
        m_css = re.search(r"css/style\.css\?v=([^\"'&\s]+)", r.text)
        assert m_js, "dashboard HTML has no versioned js/app.js URL"
        assert m_css, "dashboard HTML has no versioned css/style.css URL"
        assert m_js.group(1), "empty js version token"
        assert m_css.group(1), "empty css version token"
        assert m_js.group(1) == m_css.group(1), "js/css versions differ"

    def test_dashboard_html_still_loads_the_app(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert 'id="statUrgentSub"' in r.text
        assert 'id="urgentList"' in r.text


class TestUrgentTextNodeSuite:
    def test_node_suite_passes(self):
        node = shutil.which("node")
        assert node, "node is required for the urgent-text suite"
        proc = subprocess.run(
            [node, str(REPO_ROOT / "tests" / "test_hotfix3.js")],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        assert proc.returncode == 0, (
            "tests/test_hotfix3.js failed:\n" + proc.stdout + proc.stderr
        )
