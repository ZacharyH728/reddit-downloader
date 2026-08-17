"""RedGifs API client.

Notes that are load-bearing and were verified against the live API (their docs
repo is gone, so don't "correct" these from memory):

* The temporary bearer token is bound to the requesting IP *and* User-Agent -
  the JWT carries `valid_addr` / `valid_agent` and a mismatched UA is answered
  with `401 WrongSender`. So: one session, one pinned UA, never overridden.
* `api.redgifs.com` sets `Access-Control-Allow-Origin: https://www.redgifs.com`,
  so a browser cannot call it directly. All RedGifs JSON must be fetched here,
  server-side. Media on `media.redgifs.com` is the opposite: it needs no headers
  at all and honors Range, so the browser can load it directly.
* Creator search lives at /v1/creators/search and its free-text parameter is
  `query=` (undocumented; the `redgifs` PyPI client has no equivalent).
* A creator's gifs live at /v2/users/{name}/search, ordered by `latest`, paged by
  offset (`cursor` is always null). Probes disagreed about what happens past the
  last page - one saw 200 with an empty list, one saw 400 - so both are treated
  as end-of-listing.
* A single profile is /v1/users/{name}. There is no /v2/users/{name}.
"""
import threading
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core import net
from core.config import BROWSER_USER_AGENT, REDGIFS_MIN_INTERVAL, logger

API_ROOT = "https://api.redgifs.com"
AUTH_URL = API_ROOT + "/v2/auth/temporary"
# The API rejects count > 100 with `400 Invalid page size`.
MAX_COUNT = 100
# Tokens carry a 24h expiry; refresh a little early rather than on every call,
# because /v2/auth/temporary is itself the documented rate-limit hotspot.
TOKEN_TTL = 23 * 3600


class RedGifsGone(Exception):
    """The requested gif has been deleted upstream (HTTP 410)."""


class RedGifsClient:
    def __init__(self, min_interval=None):
        self.min_interval = REDGIFS_MIN_INTERVAL if min_interval is None else min_interval

        # API session: every request is serialized through _lock, so sharing it
        # across threads is safe by construction.
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"User-Agent": BROWSER_USER_AGENT})
        # The original client had no Retry adapter at all, so RedGifs got no 429
        # backoff whatsoever.
        retries = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS"],
        )
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("https://", adapter)

        # Media session: used for concurrent file downloads, so it must NOT be
        # the API session (requests.Session is not thread-safe).
        self.media_session = net.make_session()
        self.media_session.headers.update({"User-Agent": BROWSER_USER_AGENT})

        self.token = None
        self._token_ts = 0.0
        self._last_call = 0.0
        self._lock = threading.RLock()

    # --- internals -------------------------------------------------------

    def _throttle(self):
        """Pace API calls without sleeping when the last one was already ago.

        This replaces the old unconditional `time.sleep(1)` after every RedGifs
        download, which slept even when nothing needed pacing.
        """
        if self.min_interval <= 0:
            return
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _authenticate(self):
        """Fetch a new temporary token. Caller must hold _lock."""
        try:
            self._throttle()
            response = self.session.get(AUTH_URL, timeout=20)
            response.raise_for_status()
            self.token = response.json().get("token")
            self._token_ts = time.time()
            logger.trace("Successfully acquired new RedGifs token.")
        except (requests.exceptions.RequestException, ValueError) as e:
            logger.error("Failed to authenticate with RedGifs: %s", e)
            self.token = None
            self._token_ts = 0.0

    def _ensure_token(self):
        if not self.token or (time.time() - self._token_ts) > TOKEN_TTL:
            self._authenticate()
        return self.token

    def _get(self, path, params=None):
        """Authenticated GET returning parsed JSON.

        Raises requests.exceptions.HTTPError for non-2xx (so callers can inspect
        the status), or returns None when no token could be obtained.
        """
        with self._lock:
            if not self._ensure_token():
                return None

            url = API_ROOT + path
            headers = {"Authorization": "Bearer " + self.token}
            self._throttle()
            response = self.session.get(url, headers=headers, params=params, timeout=20)

            # 401 covers both an expired token and `WrongSender` (our egress IP
            # changed). Both are fixed by asking for a fresh one, once.
            if response.status_code == 401:
                logger.debug("RedGifs token rejected, refreshing...")
                self._authenticate()
                if not self.token:
                    return None
                headers["Authorization"] = "Bearer " + self.token
                self._throttle()
                response = self.session.get(url, headers=headers, params=params, timeout=20)

            response.raise_for_status()
            return response.json()

    # --- public API ------------------------------------------------------

    def get_media_info(self, video_id):
        """Metadata for one gif. Raises RedGifsGone if it was deleted upstream."""
        try:
            return self._get("/v2/gifs/%s" % video_id)
        except requests.exceptions.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                raise RedGifsGone(video_id)
            logger.error("Error fetching RedGifs metadata for %s: %s", video_id, e)
            return None
        except (requests.exceptions.RequestException, ValueError) as e:
            logger.error("Unexpected error fetching RedGifs metadata for %s: %s", video_id, e)
            return None

    def search_creators(self, query, page=1, count=40):
        """Free-text creator search -> {items, page, pages, total}."""
        params = {
            "query": query,
            "page": max(1, int(page)),
            "count": min(int(count), MAX_COUNT),
            "order": "best_match",
        }
        try:
            data = self._get("/v1/creators/search", params=params)
        except requests.exceptions.HTTPError as e:
            if getattr(e.response, "status_code", None) == 400:
                # Probes disagreed on the accepted `order` values; relevance
                # ordering isn't worth failing the search over.
                params.pop("order", None)
                try:
                    data = self._get("/v1/creators/search", params=params)
                except (requests.exceptions.RequestException, ValueError) as e2:
                    logger.error("RedGifs creator search failed for %r: %s", query, e2)
                    return None
            else:
                logger.error("RedGifs creator search failed for %r: %s", query, e)
                return None
        except (requests.exceptions.RequestException, ValueError) as e:
            logger.error("RedGifs creator search failed for %r: %s", query, e)
            return None

        if not isinstance(data, dict):
            return None
        return {
            "items": data.get("items") or [],
            "page": data.get("page") or 1,
            "pages": data.get("pages") or 1,
            "total": data.get("total") or 0,
        }

    def get_user(self, username):
        """One creator profile, or None if they don't exist. Note: v1, not v2."""
        try:
            return self._get("/v1/users/%s" % username)
        except requests.exceptions.HTTPError as e:
            if getattr(e.response, "status_code", None) in (404, 410):
                return None
            logger.error("RedGifs profile lookup failed for %s: %s", username, e)
            return None
        except (requests.exceptions.RequestException, ValueError) as e:
            logger.error("RedGifs profile lookup failed for %s: %s", username, e)
            return None

    def list_creator_gifs(self, username, page=1, count=40, order="latest"):
        """One page of a creator's gifs.

        Returns {gifs, profile, page, pages, total, end} or None on error.
        `end` is True when there is nothing after this page.
        """
        page = max(1, int(page))
        params = {
            "order": order,
            "count": min(int(count), MAX_COUNT),
            "page": page,
            "type": "g",
        }
        try:
            data = self._get("/v2/users/%s/search" % username, params=params)
        except requests.exceptions.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status == 400:
                # Paged past the end.
                return {"gifs": [], "profile": None, "page": page,
                        "pages": page, "total": None, "end": True}
            if status in (404, 410):
                return None
            logger.error("RedGifs listing failed for %s page %d: %s", username, page, e)
            return None
        except (requests.exceptions.RequestException, ValueError) as e:
            logger.error("RedGifs listing failed for %s page %d: %s", username, page, e)
            return None

        if not isinstance(data, dict):
            return None
        gifs = data.get("gifs") or []
        pages = data.get("pages") or page
        users = data.get("users") or []
        profile = None
        for user in users:
            if str(user.get("username", "")).lower() == username.lower():
                profile = user
                break
        return {
            "gifs": gifs,
            "profile": profile or (users[0] if users else None),
            "page": data.get("page") or page,
            "pages": pages,
            "total": data.get("total"),
            "end": not gifs or page >= pages,
        }


def gif_media_url(gif):
    """Best downloadable URL for a gif object, preferring HD."""
    urls = (gif or {}).get("urls") or {}
    return urls.get("hd") or urls.get("sd")


def gif_thumb_url(gif):
    """Poster/thumbnail for the preview grid.

    `urls.gif` and `urls.vthumbnail` do not exist on the v2 API despite showing
    up in some third-party type definitions - don't add them back.
    """
    urls = (gif or {}).get("urls") or {}
    return urls.get("thumbnail") or urls.get("poster")


def gif_preview_url(gif):
    """URL to play in the lightbox: the muted variant when there is one."""
    urls = (gif or {}).get("urls") or {}
    return urls.get("silent") or urls.get("sd") or urls.get("hd")
