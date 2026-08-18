"""Media proxy tests.

/api/proxy is the one endpoint that fetches a URL supplied in a request, so it is
the SSRF surface. check_url is tested exhaustively and directly; the streaming
half is tested with validation stubbed, since a local test server can never be an
allowlisted host (by design).
"""
import threading

import pytest

from core import config
from web import mediaproxy
from web.mediaproxy import ProxyError, check_url

ALLOWED = [
    "https://i.redd.it/abc123.jpg",
    "https://preview.redd.it/abc.jpg?width=640&crop=smart&s=deadbeef",
    "https://external-preview.redd.it/abc.jpg?s=x",
    "https://v.redd.it/abc/DASH_1080.mp4",
    "https://i.imgur.com/abc.png",
    "https://media.redgifs.com/DisfiguredFirebrickShrew.mp4",
    "https://thumbs4.redgifs.com/abc-mobile.jpg",
    "https://userpic.redgifs.com/f/bb/abc.png",
    "https://redgifs.com/x.jpg",
    "https://REDGIFS.COM/x.jpg",
    "https://media.redgifs.com./x.mp4",       # trailing dot is still the same host
    "https://www.redditmedia.com/x.jpg",
    "https://styles.redditmedia.com/x.png",
    "https://pbs.twimg.com/media/ABC123?format=jpg&name=orig",
    "https://video.twimg.com/ext_tw_video/1/pu/vid/1280x720/x.mp4",
]

REJECTED = [
    # wrong scheme
    ("http://i.redd.it/abc.jpg", "bad_scheme"),
    ("file:///etc/passwd", "bad_scheme"),
    ("ftp://i.redd.it/x.jpg", "bad_scheme"),
    ("//i.redd.it/x.jpg", "bad_scheme"),
    ("javascript:alert(1)", "bad_scheme"),
    # credentials smuggling the real host into the userinfo
    ("https://media.redgifs.com@evil.com/x.mp4", "bad_url"),
    ("https://i.redd.it@169.254.169.254/latest/meta-data/", "bad_url"),
    # lookalike domains
    ("https://evil-redgifs.com/x.mp4", "bad_host"),
    ("https://redgifs.com.evil.com/x.mp4", "bad_host"),
    ("https://notredd.it/x.jpg", "bad_host"),
    ("https://redd.it.evil.com/x.jpg", "bad_host"),
    ("https://myi.redd.it.evil.com/x.jpg", "bad_host"),
    # the API must never be proxied
    ("https://api.redgifs.com/v2/gifs/abc", "bad_host"),
    # twimg is allowlisted by exact host, so the rest of the domain stays out -
    # abs.twimg.com serves site JavaScript, and api.x.com is an API.
    ("https://abs.twimg.com/responsive-web/client-web/main.js", "bad_host"),
    ("https://pbs.twimg.com.evil.com/x.jpg", "bad_host"),
    ("https://evil-pbs.twimg.com/x.jpg", "bad_host"),
    ("https://api.x.com/1.1/statuses/show.json", "bad_host"),
    ("https://x.com/someone/media", "bad_host"),
    # IP literals: cloud metadata and LAN services
    ("https://169.254.169.254/latest/meta-data/", "bad_host"),
    ("https://127.0.0.1/x.jpg", "bad_host"),
    ("https://10.0.0.5/x.jpg", "bad_host"),
    ("https://[::1]/x.jpg", "bad_host"),
    ("https://[fd00::1]/x.jpg", "bad_host"),
    # explicit ports
    ("https://i.redd.it:8080/x.jpg", "bad_url"),
    ("https://i.redd.it:notaport/x.jpg", "bad_url"),
    # punycode homoglyphs
    ("https://xn--redgifs-x0e.com/x.mp4", "bad_host"),
    # nothing at all
    ("", "bad_url"),
    (None, "bad_url"),
    ("https:///x.jpg", "bad_url"),
]


@pytest.mark.parametrize("url", ALLOWED)
def test_allowed_urls(url):
    assert check_url(url) == url


@pytest.mark.parametrize("url,code", REJECTED)
def test_rejected_urls(url, code):
    with pytest.raises(ProxyError) as exc:
        check_url(url)
    assert exc.value.code == code, "%s gave %s, expected %s" % (url, exc.value.code, code)


def test_oversized_url_is_rejected():
    with pytest.raises(ProxyError):
        check_url("https://i.redd.it/" + "x" * 3000 + ".jpg")


def test_query_string_is_never_modified():
    """preview.redd.it URLs are HMAC-signed; touching the query breaks them."""
    signed = "https://preview.redd.it/a.jpg?width=640&format=pjpg&auto=webp&s=abcdef123"
    assert check_url(signed) == signed


# --- streaming ------------------------------------------------------------

@pytest.fixture
def local(monkeypatch):
    """Start a local server and let the proxy talk to it.

    check_url is stubbed here on purpose: a loopback address can never be an
    allowlisted host, which is exactly the property the tests above verify.
    """
    from tests.test_download import Handler, QuietServer

    httpd = QuietServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(mediaproxy, "check_url", lambda url: url)
    yield "http://127.0.0.1:%d" % httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def drain(url, **kwargs):
    response, headers, status = mediaproxy.open_stream(url, **kwargs)
    body = b"".join(mediaproxy.iter_body(response)) if response is not None else b""
    return body, headers, status


def test_streams_a_body_and_sets_headers(local):
    body, headers, status = drain(local + "/good.jpg")
    assert status == 200
    assert len(body) == 5004
    assert headers["Content-Type"] == "image/jpeg"
    assert headers["Accept-Ranges"] == "bytes"
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_content_type_comes_from_the_url_not_upstream(local):
    """An upstream that mislabels a .jpg as text/html must not have that echoed."""
    _body, headers, _status = drain(local + "/htmlerror.jpg")
    assert headers["Content-Type"] == "image/jpeg"


@pytest.mark.parametrize("url,expected", [
    # pbs.twimg.com leaves the path extensionless and puts the format in the
    # query, so there is nothing for the path-based branch to find.
    ("https://pbs.twimg.com/media/ABC123?format=jpg&name=orig", "image/jpeg"),
    ("https://pbs.twimg.com/media/ABC123?format=png&name=small", "image/png"),
    ("https://pbs.twimg.com/media/ABC123?format=webp&name=orig", "image/webp"),
    # A path extension still wins over the query.
    ("https://video.twimg.com/ext_tw_video/1/pu/vid/720x1280/x.mp4", "video/mp4"),
    # An unknown format falls through to the upstream header, not to a guess.
    ("https://pbs.twimg.com/media/ABC123?format=exe&name=orig",
     "application/octet-stream"),
])
def test_twimg_content_type_is_read_from_the_query(url, expected):
    assert mediaproxy._content_type_for(url, None) == expected


def test_gone_upstream_maps_to_410(local):
    with pytest.raises(ProxyError) as exc:
        drain(local + "/gone.jpg")
    assert exc.value.code == "gone_upstream"
    assert exc.value.status == 410


def test_upstream_error_maps_to_502(local):
    with pytest.raises(ProxyError) as exc:
        drain(local + "/boom.jpg")
    assert exc.value.status == 502


def test_stream_slots_are_released(local):
    """Every path - success, 410, 502 - must give the semaphore slot back."""
    for _ in range(config.MAX_STREAMS + 2):
        drain(local + "/good.jpg")
    for _ in range(config.MAX_STREAMS + 2):
        with pytest.raises(ProxyError):
            drain(local + "/gone.jpg")
    # If slots had leaked, this would raise too_many_streams.
    body, _headers, status = drain(local + "/good.jpg")
    assert status == 200 and body


def test_saturation_returns_503(local, monkeypatch):
    held = []
    try:
        for _ in range(config.MAX_STREAMS):
            held.append(mediaproxy.open_stream(local + "/good.jpg"))
        with pytest.raises(ProxyError) as exc:
            mediaproxy.open_stream(local + "/good.jpg")
        assert exc.value.code == "too_many_streams"
        assert exc.value.status == 503
    finally:
        for response, _h, _s in held:
            response.close()
            mediaproxy.release()


def test_body_cap_truncates_a_lying_content_length(local, monkeypatch):
    """Content-Length can lie, so the streaming counter is the real limit."""
    response, _headers, _status = mediaproxy.open_stream(local + "/good.jpg")
    body = b"".join(mediaproxy.iter_body(response, max_bytes=1000))
    assert len(body) <= 1000 + 65536
