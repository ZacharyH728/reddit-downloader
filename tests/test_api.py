"""HTTP API tests via Flask's test client. No network, no credentials."""
import threading

import pytest

from core import config, creators, jobs
from core.jobs import JobManager
from tests.test_download import Handler, QuietServer
from tests.test_jobs import make_desc
from web import mediaproxy
from web.server import create_app


@pytest.fixture(scope="module")
def server():
    httpd = QuietServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:%d" % httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def stubs(tmp_path, monkeypatch, server):
    """Stub both platform APIs at the creators seam."""
    monkeypatch.setattr(config, "CREATOR_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "DOWNLOAD_LOCATION", str(tmp_path))
    monkeypatch.setattr(config, "MIN_FREE_DISK_MB", 0)

    state = {
        "search": {"reddit": {"results": [], "error": None},
                   "redgifs": {"results": [], "error": None}},
        "items": {"platform": "reddit", "creator": "someone", "dest": "x",
                  "items": [], "next": None, "total": None,
                  "page": None, "pages": None, "truncated_reason": None},
        "creator": {"platform": "reddit", "creator": "someone", "profile": {"name": "someone"},
                    "have": {"items": 0, "files": 0, "bytes": 0}, "dest": "x",
                    "listing_cap": 1000},
        "descs": [make_desc("aa11", server + "/good.jpg")],
        "library": [],
    }
    monkeypatch.setattr(creators, "search", lambda *a, **k: state["search"])
    monkeypatch.setattr(creators, "list_items", lambda *a, **k: state["items"])
    monkeypatch.setattr(creators, "get_creator", lambda *a, **k: state["creator"])
    monkeypatch.setattr(creators, "local_library", lambda: state["library"])
    monkeypatch.setattr(creators, "resolve_selected",
                        lambda p, n, ids: ([d for d in state["descs"] if d["id"] in ids],
                                           [i for i in ids
                                            if i not in {d["id"] for d in state["descs"]}]))
    monkeypatch.setattr(creators, "iter_all_items",
                        lambda p, n, **k: iter(state["descs"]))
    monkeypatch.setattr(creators, "total_estimate", lambda p, n: None)
    return state


@pytest.fixture
def manager():
    m = JobManager(concurrency=2, history=10)
    m.start()
    yield m
    m.stop()


@pytest.fixture
def client(stubs, manager):
    app = create_app(manager)
    app.config.update(TESTING=True)
    return app.test_client()


# --- basics ---------------------------------------------------------------

def test_health_reports_config(client):
    body = client.get("/api/health").get_json()
    assert body["ok"] is True
    assert "concurrency" in body and "listing_cap" in body


def test_health_never_leaks_a_secret(client, monkeypatch):
    """No response should ever contain the Reddit password or client secret."""
    monkeypatch.setattr(config, "PASSWORD", "super-secret-password")
    monkeypatch.setattr(config, "CLIENT_SECRET", "client-secret-value")
    body = client.get("/api/health").get_data(as_text=True)
    for secret in ("super-secret-password", "client-secret-value"):
        assert secret not in body


def test_security_headers_are_present(client):
    headers = client.get("/api/health").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"


def test_unknown_api_route_returns_a_json_error(client):
    response = client.get("/api/nope")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "not_found"


def test_unknown_page_route_serves_the_spa(client):
    response = client.get("/some/client/route")
    assert response.status_code == 200
    assert b"<!DOCTYPE html" in response.data or b"<!doctype html" in response.data


# --- search ---------------------------------------------------------------

def test_search_requires_two_characters(client):
    assert client.get("/api/search?q=a").status_code == 400
    assert client.get("/api/search").status_code == 400
    assert client.get("/api/search?q=" + "x" * 100).status_code == 400


def test_search_rejects_an_unknown_platform(client):
    response = client.get("/api/search?q=abc&platform=twitter")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_platform"


def test_search_returns_both_sections(client, stubs):
    stubs["search"]["reddit"]["results"] = [{"platform": "reddit", "name": "spez"}]
    stubs["search"]["redgifs"]["error"] = "RedGifs search is unavailable."
    body = client.get("/api/search?q=spez").get_json()
    # One platform failing must still be a 200 with the other's results.
    assert body["reddit"]["results"][0]["name"] == "spez"
    assert body["redgifs"]["error"]


# --- creators -------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/api/creators/reddit/..",
    "/api/creators/reddit/a",
    "/api/creators/twitter/someone",
    "/api/creators/reddit/with%20space",
    "/api/creators/reddit/%2e%2e",
    "/api/creators/reddit/%252e%252e",
])
def test_invalid_creator_paths_are_rejected(client, path):
    response = client.get(path)
    assert response.status_code in (400, 404), path
    if response.status_code == 400:
        assert response.get_json()["error"]["code"] in (
            "invalid_creator", "invalid_platform")


def test_creator_items_passes_through_paging(client, stubs):
    stubs["items"]["items"] = [{"id": "aa11", "have": False}]
    stubs["items"]["next"] = "t3_aa11"
    body = client.get("/api/creators/reddit/someone/items?limit=5").get_json()
    assert body["next"] == "t3_aa11"
    assert body["items"][0]["id"] == "aa11"


def test_creator_404s_when_missing_and_no_local_files(client, stubs):
    stubs["creator"]["profile"] = None
    response = client.get("/api/creators/reddit/someone")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "creator_not_found"


def test_creator_still_returned_when_local_files_exist(client, stubs):
    stubs["creator"]["profile"] = None
    stubs["creator"]["have"] = {"items": 3, "files": 5, "bytes": 100}
    assert client.get("/api/creators/reddit/someone").status_code == 200


# --- jobs -----------------------------------------------------------------

def test_download_and_poll_a_job(client, manager, tmp_path):
    response = client.post("/api/downloads", json={
        "platform": "reddit", "creator": "someone", "mode": "selected", "ids": ["aa11"]})
    assert response.status_code == 202
    job_id = response.get_json()["job"]["id"]

    from tests.test_jobs import wait_for
    wait_for(manager, job_id)

    body = client.get("/api/jobs/%s" % job_id).get_json()["job"]
    assert body["state"] == jobs.DONE
    assert body["downloaded"] == 1
    assert (tmp_path / "reddit" / "someone" / "Post aa11.jpg").exists()


def test_duplicate_job_returns_409(client, stubs, server):
    stubs["descs"] = [make_desc("bb%02d" % i, server + "/slow.jpg") for i in range(20)]
    first = client.post("/api/downloads", json={
        "platform": "reddit", "creator": "someone", "mode": "all"})
    assert first.status_code == 202
    second = client.post("/api/downloads", json={
        "platform": "reddit", "creator": "someone", "mode": "all"})
    assert second.status_code == 409
    assert second.get_json()["error"]["code"] == "job_exists"
    client.post("/api/jobs/%s/cancel" % first.get_json()["job"]["id"])


def test_download_rejects_bad_input(client):
    assert client.post("/api/downloads", json={
        "platform": "reddit", "creator": "someone", "mode": "selected",
        "ids": []}).status_code == 400
    assert client.post("/api/downloads", json={
        "platform": "twitter", "creator": "someone", "mode": "all"}).status_code == 400
    assert client.post("/api/downloads", json={
        "platform": "reddit", "creator": "../etc", "mode": "all"}).status_code == 400
    assert client.post("/api/downloads", json={
        "platform": "reddit", "creator": "someone", "mode": "sideways"}).status_code == 400


def test_download_ignores_a_client_supplied_url(client, manager, tmp_path):
    """Only IDs are honored. A url field in the body must be ignored entirely,
    never fetched - otherwise this endpoint writes arbitrary content to disk."""
    response = client.post("/api/downloads", json={
        "platform": "reddit", "creator": "someone", "mode": "selected",
        "ids": ["aa11"], "url": "https://evil.example/payload"})
    assert response.status_code == 202

    from tests.test_jobs import wait_for
    job = wait_for(manager, response.get_json()["job"]["id"])
    assert job["downloaded"] == 1
    # The file came from the stubbed desc, not from the supplied url.
    written = sorted(p.name for p in (tmp_path / "reddit" / "someone").glob("*.jpg"))
    assert written == ["Post aa11.jpg"]


def test_unknown_job_is_404(client):
    assert client.get("/api/jobs/deadbeef").status_code == 404
    assert client.post("/api/jobs/deadbeef/cancel").status_code == 404
    assert client.post("/api/jobs/deadbeef/retry-failed").status_code == 404


def test_jobs_list_shape(client):
    body = client.get("/api/jobs").get_json()
    assert "jobs" in body and "active" in body


def test_cancel_is_accepted(client, stubs, server):
    stubs["descs"] = [make_desc("cc%02d" % i, server + "/slow.jpg") for i in range(30)]
    created = client.post("/api/downloads", json={
        "platform": "reddit", "creator": "someone", "mode": "all"}).get_json()["job"]
    response = client.post("/api/jobs/%s/cancel" % created["id"])
    assert response.status_code == 202
    assert response.get_json()["job"]["state"] in (jobs.CANCELLED, jobs.RUNNING)


# --- sync -----------------------------------------------------------------

def test_sync_status(client):
    body = client.get("/api/sync").get_json()
    assert "enabled" in body and "interval" in body


def test_sync_run_triggers_the_wake_event(client, monkeypatch):
    from core import sync as sync_module
    monkeypatch.setattr(config, "SYNC_SAVED_ENABLED", True)
    monkeypatch.setattr(sync_module, "get_state", lambda: {"running": False})
    sync_module.WAKE.clear()
    assert client.post("/api/sync/run").status_code == 202
    assert sync_module.WAKE.is_set()
    sync_module.WAKE.clear()


def test_sync_run_is_403_when_disabled(client, monkeypatch):
    monkeypatch.setattr(config, "SYNC_SAVED_ENABLED", False)
    response = client.post("/api/sync/run")
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "sync_disabled"


def test_sync_run_is_409_while_running(client, monkeypatch):
    from core import sync as sync_module
    monkeypatch.setattr(config, "SYNC_SAVED_ENABLED", True)
    monkeypatch.setattr(sync_module, "get_state", lambda: {"running": True})
    assert client.post("/api/sync/run").status_code == 409


# --- proxy endpoint -------------------------------------------------------

@pytest.mark.parametrize("url,status", [
    ("http://i.redd.it/x.jpg", 400),
    ("https://evil-redgifs.com/x.mp4", 400),
    ("https://169.254.169.254/latest/meta-data/", 400),
    ("https://api.redgifs.com/v2/gifs/x", 400),
    ("https://i.redd.it@evil.com/x.jpg", 400),
    ("", 400),
])
def test_proxy_rejects_bad_urls(client, url, status):
    response = client.get("/api/proxy", query_string={"url": url})
    assert response.status_code == status
    assert response.get_json()["error"]["code"] in (
        "bad_url", "bad_scheme", "bad_host")


def test_proxy_streams_and_forwards_range(client, server, monkeypatch):
    monkeypatch.setattr(mediaproxy, "check_url", lambda url: url)

    plain = client.get("/api/proxy", query_string={"url": server + "/good.jpg"})
    assert plain.status_code == 200
    assert plain.headers["Content-Type"] == "image/jpeg"
    assert plain.headers["Accept-Ranges"] == "bytes"

    ranged = client.get("/api/proxy", query_string={"url": server + "/good.jpg"},
                        headers={"Range": "bytes=0-1023"})
    # Range must reach upstream and 206 must pass back, or iOS won't play video.
    assert ranged.status_code == 206
    assert ranged.headers["Content-Range"] == "bytes 0-1023/5004"
    assert len(ranged.get_data()) == 1024


def test_proxy_maps_gone_upstream_to_410(client, server, monkeypatch):
    monkeypatch.setattr(mediaproxy, "check_url", lambda url: url)
    response = client.get("/api/proxy", query_string={"url": server + "/gone.jpg"})
    assert response.status_code == 410
    assert response.get_json()["error"]["code"] == "gone_upstream"


def test_proxy_allowlist_is_enforced_through_the_route(client, server):
    """There is no auth, so the host allowlist is the only thing standing between
    this endpoint and being an open request forwarder."""
    for url in ("https://169.254.169.254/latest/meta-data/",
                "https://evil-redgifs.com/x.mp4",
                "http://i.redd.it/x.jpg"):
        response = client.get("/api/proxy", query_string={"url": url})
        assert response.status_code == 400, url
