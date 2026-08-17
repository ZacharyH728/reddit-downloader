"""The media proxy: a fallback path for CDN URLs the browser can't load itself.

Most media is hotlinked straight from the CDN, which is faster and cheaper. This
exists for the cases that don't work that way - notably preview.redd.it, whose
URLs are HMAC-signed and may be refused - and as the target of the frontend's
one-shot retry when an <img>/<video> errors.

It is also the one endpoint that fetches an arbitrary URL on request, so it is the
SSRF surface. The rules below are deliberately strict and are covered by tests.
"""
import ipaddress
import re
import threading
from http.cookiejar import DefaultCookiePolicy
from urllib.parse import urlsplit

import requests

from core import config, net
from core.config import logger

# Hosts we will fetch from. Exact names plus anchored parent domains - the leading
# dot in the suffix check is what makes `evil-redgifs.com` fail.
_EXACT_HOSTS = frozenset([
    "i.redd.it",
    "preview.redd.it",
    "external-preview.redd.it",
    "v.redd.it",
    "i.imgur.com",
])
# thumbs1..N.redgifs.com and media.redgifs.com are unbounded in number, so the
# redgifs domain is matched by suffix. api.redgifs.com is excluded on purpose:
# this endpoint must never become an API pass-through.
_SUFFIX_HOSTS = ("redgifs.com", "redd.it", "redditmedia.com", "redditstatic.com")
_BLOCKED_HOSTS = frozenset(["api.redgifs.com"])

MAX_REDIRECTS = 3

# Response headers worth passing back to the browser.
_PASS_THROUGH = ("content-length", "content-range", "etag", "last-modified")
_EXT_TYPES = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "gif": "image/gif", "webp": "image/webp",
    "mp4": "video/mp4", "webm": "video/webm", "m4v": "video/mp4",
}
_ALLOWED_TYPE_RE = re.compile(r"^(image/|video/|application/octet-stream)", re.I)

_streams = threading.BoundedSemaphore(max(1, config.MAX_STREAMS))


class ProxyError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def check_url(raw):
    """Validate an outbound URL. Returns it unchanged, or raises ProxyError.

    The URL's query string is never modified: preview.redd.it signs its URLs and
    editing any parameter turns them into 403s.
    """
    if not raw or len(raw) > 2048:
        raise ProxyError("bad_url", "Missing or oversized url parameter.")

    parts = urlsplit(raw)
    if parts.scheme != "https":
        raise ProxyError("bad_scheme", "Only https URLs can be proxied.")
    # Credentials in the netloc are how `https://media.redgifs.com@evil.com/`
    # sneaks a foreign host past a naive prefix check.
    if "@" in parts.netloc:
        raise ProxyError("bad_url", "URLs with credentials are not allowed.")
    try:
        if parts.port is not None:
            raise ProxyError("bad_url", "Explicit ports are not allowed.")
    except ValueError:
        # urlsplit raises rather than returning None for a malformed port.
        raise ProxyError("bad_url", "Malformed port in URL.")

    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise ProxyError("bad_url", "Missing host.")
    # An IP literal can never be one of our CDNs, and is the shape used to reach
    # cloud metadata endpoints and LAN services.
    try:
        ipaddress.ip_address(host)
        raise ProxyError("bad_host", "IP addresses are not allowed.")
    except ValueError:
        pass
    if host.startswith("xn--") or ".xn--" in host:
        raise ProxyError("bad_host", "Punycode hosts are not allowed.")
    if host in _BLOCKED_HOSTS:
        raise ProxyError("bad_host", "%s is not proxied." % host)
    if host not in _EXACT_HOSTS and not any(
            host == suffix or host.endswith("." + suffix) for suffix in _SUFFIX_HOSTS):
        raise ProxyError("bad_host", "%s is not an allowed media host." % host)
    return raw


def _content_type_for(url, upstream_type):
    """Prefer a type derived from the URL over whatever upstream claims."""
    path = urlsplit(url).path.lower()
    ext = path.rsplit(".", 1)[-1] if "." in path else ""
    if ext in _EXT_TYPES:
        return _EXT_TYPES[ext]
    base = (upstream_type or "").split(";")[0].strip().lower()
    if _ALLOWED_TYPE_RE.match(base):
        return base
    return "application/octet-stream"


_session = None
_session_lock = threading.Lock()


def _get_session():
    global _session
    with _session_lock:
        if _session is None:
            # No env proxies, no cookies, fail fast: a user is waiting.
            _session = net.make_interactive_session(pool=config.MAX_STREAMS * 2)
            # An empty allowlist rejects every domain, so the proxy stays
            # stateless and can't be made to replay a cookie to a CDN.
            _session.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
        return _session


def open_stream(url, range_header=None, if_none_match=None):
    """Fetch `url`, following allowlisted redirects manually.

    Returns (response, headers_to_send, status). The caller must close the
    response, and must release the stream slot via `release()`.
    """
    check_url(url)

    if not _streams.acquire(blocking=False):
        raise ProxyError("too_many_streams",
                         "Too many media streams in flight; retry shortly.", 503)

    try:
        headers = {}
        if range_header:
            # Forwarding Range is what makes <video> seeking work, and iOS
            # Safari will not play a video at all without a 206.
            headers["Range"] = range_header
        if if_none_match:
            headers["If-None-Match"] = if_none_match

        current = url
        response = None
        for _ in range(MAX_REDIRECTS + 1):
            response = _get_session().get(
                current, headers=headers, stream=True,
                allow_redirects=False, timeout=(5, 20))
            if response.status_code not in (301, 302, 303, 307, 308):
                break
            location = response.headers.get("location")
            response.close()
            if not location:
                raise ProxyError("bad_upstream", "Redirect without a location.", 502)
            current = requests.compat.urljoin(current, location)
            # Re-validate every hop: an allowlisted host must not be able to
            # bounce this proxy somewhere it was never allowed to reach.
            check_url(current)
        else:
            raise ProxyError("too_many_redirects", "Too many redirects.", 502)

        if response.status_code in (404, 410):
            response.close()
            raise ProxyError("gone_upstream", "That media is no longer available.", 410)
        if response.status_code == 304:
            out = {"Cache-Control": "private, max-age=3600"}
            etag = response.headers.get("etag")
            if etag:
                out["ETag"] = etag
            response.close()
            # There is no body, so iter_body will never run to release the slot.
            _streams.release()
            return None, out, 304
        if response.status_code >= 400:
            status = response.status_code
            response.close()
            raise ProxyError("bad_upstream", "Upstream returned %d." % status, 502)

        declared = response.headers.get("content-length")
        if declared and not range_header:
            try:
                if int(declared) > config.PROXY_MAX_MB * 1024 * 1024:
                    response.close()
                    raise ProxyError("too_large", "That file is too large to proxy.", 413)
            except ValueError:
                pass

        out = {
            "Content-Type": _content_type_for(current, response.headers.get("content-type")),
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
            # The body is media, so nothing in it should ever be able to load
            # anything else if a browser decides to render it as a document.
            "Content-Security-Policy": "default-src 'none'; sandbox",
        }
        for header in _PASS_THROUGH:
            value = response.headers.get(header)
            if value:
                out[header.title()] = value
        return response, out, response.status_code
    except ProxyError:
        _streams.release()
        raise
    except requests.exceptions.RequestException as e:
        _streams.release()
        logger.trace("Proxy fetch failed for %s: %s", url, e)
        raise ProxyError("bad_upstream", "Could not fetch that media.", 502)
    except BaseException:
        _streams.release()
        raise


def release():
    try:
        _streams.release()
    except ValueError:
        pass


def iter_body(response, max_bytes=None):
    """Stream the body, enforcing a hard cap in case Content-Length lied."""
    limit = max_bytes or (config.PROXY_MAX_MB * 1024 * 1024)
    sent = 0
    try:
        for chunk in response.iter_content(chunk_size=net.CHUNK_SIZE):
            if not chunk:
                continue
            sent += len(chunk)
            if sent > limit:
                logger.warning("Proxy stream exceeded %d bytes; truncating.", limit)
                return
            yield chunk
    finally:
        response.close()
        release()
