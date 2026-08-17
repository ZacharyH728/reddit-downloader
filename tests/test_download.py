"""download_file() tests against a real local HTTP server.

The important ones are the truncation cases. Before the rewrite, a stream that
died mid-write left a short file at the final path, and every later run treated
that file as complete - so the item was silently, permanently corrupt.
"""
import os
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core import net


class QuietServer(ThreadingHTTPServer):
    """ThreadingHTTPServer without the startup DNS stall.

    HTTPServer.server_bind() calls socket.getfqdn(), a reverse DNS lookup on the
    bind address that can block for 30+ seconds depending on the network.
    """

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

GOOD_BODY = b"\xff\xd8\xff\xe0" + b"x" * 5000  # JPEG magic + filler


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_HEAD(self):
        if self.path == "/good.jpg":
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(GOOD_BODY)))
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if self.path == "/good.jpg":
            # Honor Range so the proxy's 206 passthrough can be exercised - real
            # CDNs do, and video seeking depends on it.
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                spec = rng[6:].split(",")[0]
                start_s, _, end_s = spec.partition("-")
                start = int(start_s) if start_s else 0
                end = int(end_s) if end_s else len(GOOD_BODY) - 1
                end = min(end, len(GOOD_BODY) - 1)
                chunk = GOOD_BODY[start:end + 1]
                self.send_response(206)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(chunk)))
                self.send_header("Content-Range", "bytes %d-%d/%d"
                                 % (start, end, len(GOOD_BODY)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                self.wfile.write(chunk)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(GOOD_BODY)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(GOOD_BODY)

        elif self.path == "/truncated.jpg":
            # Declares a large body, sends a small one, then hangs up. This is
            # the connection-dropped-mid-download case.
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", "5000000")
            self.end_headers()
            self.wfile.write(b"\xff\xd8\xff\xe0" + b"x" * 2000)
            self.close_connection = True

        elif self.path == "/htmlerror.jpg":
            # 200 OK with an HTML error page - a CDN failure mode that used to
            # get saved as a .jpg and cached as "already downloaded".
            body = b"<html><body>Access denied</body></html>" * 40
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/tiny.jpg":
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", "3")
            self.end_headers()
            self.wfile.write(b"abc")

        elif self.path == "/gone.jpg":
            self.send_response(410)
            self.end_headers()

        elif self.path == "/missing.jpg":
            self.send_response(404)
            self.end_headers()

        elif self.path == "/boom.jpg":
            self.send_response(500)
            self.end_headers()

        elif self.path == "/slow.jpg":
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(64 * 1024 * 40))
            self.end_headers()
            try:
                for _ in range(40):
                    self.wfile.write(b"\xff\xd8\xff\xe0" + b"y" * (64 * 1024 - 4))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture(scope="module")
def server():
    httpd = QuietServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:%d" % httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def session():
    s = net.make_session()
    # The retry policy would otherwise turn each 5xx case into a slow test.
    s.adapters.clear()
    import requests.adapters
    s.mount("http://", requests.adapters.HTTPAdapter(max_retries=0))
    return s


def no_residue(tmp_path):
    """No .part files and no other leftovers beside the target."""
    return [p.name for p in tmp_path.iterdir() if p.name.endswith(net.PART_SUFFIX)]


def test_successful_download(server, session, tmp_path):
    dest = str(tmp_path / "good.jpg")
    assert net.download_file(server + "/good.jpg", dest, session=session) == net.DOWNLOADED
    assert os.path.getsize(dest) == len(GOOD_BODY)
    assert no_residue(tmp_path) == []


def test_truncated_download_is_rejected(server, session, tmp_path):
    """The regression this whole rewrite exists for."""
    dest = str(tmp_path / "truncated.jpg")
    result = net.download_file(server + "/truncated.jpg", dest, session=session)
    assert result == net.FAILED
    assert not os.path.exists(dest), "a truncated file was published to the final path"
    assert no_residue(tmp_path) == [], "left a .part orphan behind"


def test_html_error_page_is_rejected(server, session, tmp_path):
    dest = str(tmp_path / "htmlerror.jpg")
    assert net.download_file(server + "/htmlerror.jpg", dest, session=session) == net.FAILED
    assert not os.path.exists(dest)
    assert no_residue(tmp_path) == []


def test_tiny_body_is_rejected(server, session, tmp_path):
    dest = str(tmp_path / "tiny.jpg")
    assert net.download_file(server + "/tiny.jpg", dest, session=session) == net.FAILED
    assert not os.path.exists(dest)


def test_410_and_404_are_gone_not_failed(server, session, tmp_path):
    """GONE is permanent, so callers record it and never retry. FAILED is not."""
    assert net.download_file(server + "/gone.jpg", str(tmp_path / "a.jpg"),
                             session=session) == net.GONE
    assert net.download_file(server + "/missing.jpg", str(tmp_path / "b.jpg"),
                             session=session) == net.GONE
    assert net.download_file(server + "/boom.jpg", str(tmp_path / "c.jpg"),
                             session=session) == net.FAILED
    assert no_residue(tmp_path) == []


def test_existing_file_is_skipped(server, session, tmp_path):
    dest = tmp_path / "good.jpg"
    dest.write_bytes(b"whatever")
    assert net.download_file(server + "/good.jpg", str(dest), session=session) == net.SKIPPED
    assert dest.read_bytes() == b"whatever", "skip must not overwrite"


def test_check_size_redownloads_a_wrong_sized_file(server, session, tmp_path):
    """The RedGifs path uses check_size to repair a previously-partial file."""
    dest = tmp_path / "good.jpg"
    dest.write_bytes(b"short")
    result = net.download_file(server + "/good.jpg", str(dest),
                               session=session, check_size=True)
    assert result == net.DOWNLOADED
    assert dest.stat().st_size == len(GOOD_BODY)


def test_check_size_skips_a_matching_file(server, session, tmp_path):
    dest = tmp_path / "good.jpg"
    dest.write_bytes(GOOD_BODY)
    assert net.download_file(server + "/good.jpg", str(dest),
                             session=session, check_size=True) == net.SKIPPED


def test_cancellation_cleans_up(server, session, tmp_path):
    dest = str(tmp_path / "slow.jpg")
    calls = {"n": 0}

    def should_cancel():
        calls["n"] += 1
        return calls["n"] > 2

    result = net.download_file(server + "/slow.jpg", dest, session=session,
                               should_cancel=should_cancel)
    assert result == net.CANCELLED
    assert not os.path.exists(dest)
    assert no_residue(tmp_path) == []


def test_sweep_removes_only_old_part_files(tmp_path):
    old = tmp_path / "old.jpg.part"
    new = tmp_path / "new.jpg.part"
    keep = tmp_path / "real.jpg"
    for p in (old, new, keep):
        p.write_bytes(b"x")
    os.utime(str(old), (0, 0))

    assert net.sweep_part_files(str(tmp_path), older_than=3600) == 1
    assert not old.exists()
    assert new.exists()
    assert keep.exists()


def test_sweep_recurses_into_creator_directories(tmp_path):
    nested = tmp_path / "reddit" / "someone"
    nested.mkdir(parents=True)
    orphan = nested / "x.mp4.part"
    orphan.write_bytes(b"x")
    os.utime(str(orphan), (0, 0))
    assert net.sweep_part_files(str(tmp_path)) == 1
    assert not orphan.exists()


def test_audio_url_derivation():
    assert net.audio_url_for(
        "https://v.redd.it/abc/DASH_1080.mp4?source=fallback"
    ) == "https://v.redd.it/abc/DASH_audio.mp4"
