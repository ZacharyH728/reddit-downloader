"""End-to-end smoke checks: the app boots, serves its assets, and wires together.

Runs against the real create_app() with no stubs and no credentials, which is
also the "someone started this with an empty .env" case.
"""
import json
import os
import re

import pytest

from core import config
from web.server import STATIC_DIR, create_app

STATIC_FILES = ["index.html", "app.js", "style.css"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CREATOR_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "DOWNLOAD_LOCATION", str(tmp_path))
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


def test_entrypoint_module_imports():
    """app.py must import cleanly - it is the container's CMD."""
    import app as entrypoint
    assert callable(entrypoint.main)


def test_legacy_entrypoint_still_exists():
    """`python reddit_downloader.py` is the old CMD and people's muscle memory."""
    import reddit_downloader
    assert hasattr(reddit_downloader, "run_forever")


@pytest.mark.parametrize("name", STATIC_FILES)
def test_static_assets_exist(name):
    assert os.path.isfile(os.path.join(STATIC_DIR, name)), name


def test_index_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-cache"
    body = response.get_data(as_text=True)
    assert "<title>Creator Downloader</title>" in body


@pytest.mark.parametrize("name", ["app.js", "style.css"])
def test_static_route_serves_assets(client, name):
    response = client.get("/static/" + name)
    assert response.status_code == 200
    assert response.get_data()


def test_index_references_only_assets_that_exist(client):
    """A typo'd asset path is a blank page, and nothing else would catch it."""
    body = client.get("/").get_data(as_text=True)
    referenced = set(re.findall(r'(?:src|href)="/static/([^"?]+)', body))
    assert referenced, "index.html references no static assets"
    for name in referenced:
        assert client.get("/static/" + name).status_code == 200, name


def test_health_works_without_any_credentials(client):
    body = client.get("/api/health").get_json()
    assert body["ok"] is True
    codes = {w["code"] for w in body["warnings"]}
    # No .env in a fresh checkout, so this must be reported rather than crash.
    assert "reddit_credentials_missing" in codes


def test_no_endpoint_requires_authentication(client):
    """Access control was removed deliberately: this runs on a private network.
    If something starts 401ing, that is a regression, not a hardening."""
    for path in ("/api/health", "/api/jobs", "/api/library", "/api/sync"):
        assert client.get(path).status_code != 401, path


def test_library_is_empty_but_valid_on_a_fresh_install(client):
    assert client.get("/api/library").get_json() == {"creators": []}


def test_library_lists_a_creator_directory(client, tmp_path):
    directory = tmp_path / "reddit" / "somebody"
    directory.mkdir(parents=True)
    (directory / "A Post.jpg").write_bytes(b"x" * 10)
    (directory / config.MANIFEST_NAME).write_text(
        json.dumps({"posts": {"abc123": ["A Post.jpg"]}}))

    creators = client.get("/api/library").get_json()["creators"]
    assert len(creators) == 1
    assert creators[0]["platform"] == "reddit"
    assert creators[0]["creator"] == "somebody"
    assert creators[0]["items"] == 1
    assert creators[0]["files"] == 1
    assert creators[0]["bytes"] == 10


def test_library_ignores_junk_directory_names(client, tmp_path):
    """A stray directory must not be reported as a creator."""
    for name in ("not a creator", "..hidden", "x"):
        junk = tmp_path / "reddit" / name
        junk.mkdir(parents=True, exist_ok=True)
        (junk / config.MANIFEST_NAME).write_text(json.dumps({"posts": {"a": ["b.jpg"]}}))
    assert client.get("/api/library").get_json()["creators"] == []


def test_jobs_endpoint_works_with_the_default_manager(client):
    body = client.get("/api/jobs").get_json()
    assert body == {"jobs": [], "active": 0}


def test_sync_status_reports_disabled_state(client):
    body = client.get("/api/sync").get_json()
    assert set(body) >= {"enabled", "interval", "running", "summary"}


def test_config_defaults_are_sane():
    assert config.WEB_HOST == "0.0.0.0", "must bind all interfaces or -p can't reach it"
    assert config.WEB_PORT == 8080
    assert config.DOWNLOAD_CONCURRENCY >= 1
    assert config.WEB_THREADS > config.MAX_STREAMS, \
        "a saturated proxy would otherwise starve the API of worker threads"
    assert config.CREATOR_ROOT == config.DOWNLOAD_LOCATION
