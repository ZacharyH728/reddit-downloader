"""X (Twitter) access, backed by gallery-dl's extractor.

Why not the official API: as of 2026 X has no free read tier. New developers are
on pay-per-use metering per post read, so walking a creator's whole media history
- which is exactly what a "download everything" job does - would be billed per
item. gallery-dl's extractor talks to the same GraphQL endpoints the website uses,
authenticated with the operator's own `auth_token` cookie, and absorbs the churn
in X's rotating GraphQL query IDs on our behalf.

gallery-dl is used for METADATA ONLY. It is never allowed to write a file: every
download still goes through core.net, so the manifest, the `_1`/`_2` gallery
naming, cancellation and the `.part` verification all behave exactly as they do
for the other two platforms.

Three things here are load-bearing and will look like they can be simplified:

* `gallery_dl.config` is process-global module state, not per-extractor. Two
  concurrent extractions would otherwise read each other's cursor and cookies, so
  every entry point holds `_lock` for the whole extraction, exactly like the
  serialized RedGifs client.
* A tweet's files arrive as several `Message.Url` items preceded by one
  `Message.Directory`. The Directory message is the only reliable tweet boundary
  in the stream, so grouping keys off it rather than off `tweet_id` changing.
* Browsing and jobs page differently on purpose. gallery-dl's cursor advances one
  API page at a time, so a caller that stops mid-page cannot resume exactly.
  `list_media_page` accepts that (it feeds a preview grid) while `iter_all_media`
  avoids it entirely by consuming one uninterrupted generator.
"""
import threading
import time

from core import config
from core.config import logger

# Imported lazily and tolerantly: X support is optional, and a missing or
# incompatible gallery-dl must degrade to "platform unavailable" rather than
# taking the whole app down at import time.
try:
    import gallery_dl
    from gallery_dl import config as gdl_config
    from gallery_dl import exception as gdl_exception
    from gallery_dl import extractor as gdl_extractor
    from gallery_dl.extractor.message import Message
    GALLERY_DL_IMPORT_ERROR = None
except Exception as e:  # noqa: BLE001 - any import failure means "unavailable"
    gallery_dl = None
    gdl_config = None
    gdl_exception = None
    gdl_extractor = None
    Message = None
    GALLERY_DL_IMPORT_ERROR = e

ROOT = "https://x.com"
# Photos only ever come back as jpg/png; `orig` is the unresized original.
IMAGE_SIZE = "orig"
# Tweets per GraphQL request when we aren't matching a caller's page size.
# gallery-dl's own default; larger values are not obviously accepted.
API_PAGE_SIZE = 50

_lock = threading.RLock()
_configured = False
_last_call = 0.0


class TwitterUnavailable(Exception):
    """X cannot be reached at all: no gallery-dl, or no auth token."""


class TwitterAuthError(Exception):
    """The auth_token cookie is missing, expired, or rejected."""


class TwitterNotFound(Exception):
    """No such handle or tweet."""


class TwitterUpstreamError(Exception):
    """X failed for some reason that refreshing the cookie will not fix."""


def available():
    """True when X support is usable. Cheap enough to call per request."""
    return gallery_dl is not None and bool(config.TWITTER_AUTH_TOKEN)


def unavailable_reason():
    if gallery_dl is None:
        return ("gallery-dl is not installed, so X/Twitter is unavailable. "
                "Install it with: pip install gallery-dl")
    if not config.TWITTER_AUTH_TOKEN:
        return ("TWITTER_AUTH_TOKEN is unset, so X/Twitter is unavailable. "
                "Copy the `auth_token` cookie from a logged-in x.com session.")
    return None


def _require():
    if not available():
        raise TwitterUnavailable(unavailable_reason())


def _throttle():
    """Pace our own calls. Caller must hold _lock."""
    global _last_call
    interval = config.TWITTER_MIN_INTERVAL
    if interval <= 0:
        return
    wait = interval - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def _configure():
    """Apply our settings to gallery-dl's global config. Caller must hold _lock."""
    global _configured
    if _configured:
        return
    # Note we never call gallery_dl.config.load(): that is what would pull in
    # ~/.config/gallery-dl/config.json. Only the keys set below are in effect, so
    # the operator's own gallery-dl setup cannot silently change our behavior.
    gdl_config.set(("extractor", "twitter"), "cookies",
                   {"auth_token": config.TWITTER_AUTH_TOKEN})
    gdl_config.set(("extractor", "twitter"), "retweets", config.TWITTER_RETWEETS)
    gdl_config.set(("extractor", "twitter"), "size", IMAGE_SIZE)
    # We resolve video URLs ourselves and hand them to core.net; letting
    # gallery-dl shell out to yt-dlp would bypass the manifest entirely.
    gdl_config.set(("extractor", "twitter"), "videos", True)
    gdl_config.set(("extractor", "twitter"), "previews", False)
    # Quoted/replied tweets belong to whoever wrote them, not to this creator.
    gdl_config.set(("extractor", "twitter"), "quoted", False)
    gdl_config.set(("extractor", "twitter"), "text-tweets", False)
    _configured = True


def reset_config():
    """Forget the applied config so the next call re-reads core.config. Tests only."""
    global _configured
    with _lock:
        _configured = False


def _build(url, cursor=None, page_size=None):
    """Construct a configured extractor for `url`. Caller must hold _lock."""
    _configure()
    # `cursor` is read out of the global config by the extractor's own
    # _init_cursor(), so it has to be set here rather than passed in.
    gdl_config.set(("extractor", "twitter"), "cursor", cursor or True)
    # Always set, never left over: gallery-dl's config is global, so a browse
    # with limit=30 would otherwise silently become the page size of the next
    # "download everything" job too.
    gdl_config.set(("extractor", "twitter"), "limit", page_size or API_PAGE_SIZE)
    extr = gdl_extractor.find(url)
    if extr is None:
        raise TwitterUnavailable("No gallery-dl extractor matched %s" % url)
    extr.initialize()
    return extr


# X builds a per-session "transaction id" from a JavaScript bundle on
# abs.twimg.com before it will answer a GraphQL call. When the session cookie is
# bad, that chain fails first - and it fails as a 404 for an ondemand.s.*.js URL
# with an unresolved index in it, which looks nothing like an auth error. This is
# the single most likely way a wrong or expired token presents, so it is
# classified as one rather than surfaced as an opaque 500.
_TRANSACTION_ID_FAILURE = "ondemand.s."


def _is_auth_error(exc):
    """True when `exc` means "the session is bad", as opposed to "X is down"."""
    if gdl_exception is not None:
        if isinstance(exc, (gdl_exception.AuthorizationError,
                            gdl_exception.AuthenticationError)):
            return True
        if isinstance(exc, gdl_exception.HttpError):
            if getattr(exc, "status", 0) in (401, 403):
                return True
            if _TRANSACTION_ID_FAILURE in str(exc):
                return True
            return False
    text = ("%s %s" % (type(exc).__name__, exc)).lower()
    return "auth" in text or "login" in text or "401" in text


def _is_missing(exc):
    """True when the creator or tweet simply does not exist.

    Checked after _is_auth_error, and re-checks it here too: the transaction-id
    failure above is *also* a 404, and calling that "no such account" would send
    someone hunting for a typo when their cookie has expired.
    """
    if gdl_exception is None or _is_auth_error(exc):
        return False
    if isinstance(exc, gdl_exception.NotFoundError):
        return True
    return (isinstance(exc, gdl_exception.HttpError)
            and getattr(exc, "status", 0) == 404)


def _translate(exc, what):
    """Re-raise `exc` as the right one of our exceptions. Never returns."""
    if _is_auth_error(exc):
        raise TwitterAuthError(str(exc)) from exc
    if _is_missing(exc):
        raise TwitterNotFound(what) from exc
    logger.error("X request failed for %s: %s", what, exc)
    raise TwitterUpstreamError(str(exc)) from exc


def _tweet_from(meta, files):
    """One tweet's worth of gallery-dl metadata, flattened.

    `meta` is the Directory message's metadata; `files` are the per-file dicts
    from the Url messages that followed it.
    """
    author = meta.get("author") or meta.get("user") or {}
    date = meta.get("date")
    created = None
    if date is not None:
        try:
            created = date.timestamp()
        except (AttributeError, ValueError, OSError, OverflowError):
            created = None
    return {
        "id": str(meta.get("tweet_id") or ""),
        "handle": (author.get("name") or "").lower(),
        "nick": author.get("nick") or author.get("name") or "",
        "text": meta.get("content") or "",
        "created_utc": created,
        "sensitive": bool(meta.get("sensitive")),
        "files": files,
    }


def _iter_tweets(extr, should_stop=None, max_tweets=None):
    """Group an extractor's message stream into tweets.

    Yields one dict per tweet, in listing order. A tweet whose files were all
    filtered out (an unavailable photo, say) is skipped rather than yielded empty.
    """
    should_stop = should_stop or (lambda: False)
    current = None
    files = []
    produced = 0

    for message in extr:
        if should_stop():
            return

        kind = message[0]
        if kind == Message.Directory:
            if current is not None and files:
                yield _tweet_from(current, files)
                produced += 1
                if max_tweets and produced >= max_tweets:
                    return
            # Copied, not aliased: the extractor keeps mutating this same dict
            # (it pops keys and sets `num`) while emitting the Url messages that
            # follow, and we don't read it until the tweet is complete.
            current = dict(message[-1])
            files = []
        elif kind == Message.Url:
            if current is None:
                # A Url with no preceding Directory shouldn't happen; ignoring it
                # is safer than inventing a tweet with no metadata.
                continue
            url, meta = message[1], message[2]
            files.append({
                "url": url,
                "ext": (meta.get("extension") or "").lower() or "jpg",
                "type": meta.get("type") or "photo",
                "width": meta.get("width") or None,
                "height": meta.get("height") or None,
                "duration": meta.get("duration") or None,
                "num": meta.get("num") or (len(files) + 1),
            })

    if current is not None and files:
        yield _tweet_from(current, files)


# --- public API -------------------------------------------------------------

def get_user(handle):
    """Profile for one handle, or None if it doesn't exist.

    There is no fuzzy user search here: X's search endpoints need the same
    session and are not exposed by gallery-dl, so the UI resolves exact handles
    only. `TwitterInfoExtractor` is the cheapest thing that returns a user object.
    """
    _require()
    with _lock:
        try:
            _throttle()
            extr = _build("%s/%s/info" % (ROOT, handle))
            for message in extr:
                if message[0] == Message.Directory:
                    return _profile_from(message[-1], handle)
            # An extractor that yields nothing still populated the user object.
            user = getattr(extr, "_user_obj", None)
            if user:
                return _profile_from({"user": extr._transform_user(user)}, handle)
            return None
        except Exception as e:  # noqa: BLE001
            # "No such handle" is the one outcome this returns rather than
            # raises - it is an ordinary search result. Everything else is
            # raised, so a bad session doesn't read as "no such account" and
            # send the user hunting for a typo.
            if _is_missing(e):
                return None
            _translate(e, "@" + handle)


def _profile_from(meta, handle):
    user = meta.get("user") or meta.get("author") or {}
    name = (user.get("name") or handle or "").lower()
    if not name:
        return None
    return {
        "platform": "twitter",
        "name": name,
        "display": user.get("nick") or name,
        "avatar": user.get("profile_image") or None,
        # The media tab is what we download, so its count is the honest one.
        "count": user.get("media_count"),
        "followers": user.get("followers_count"),
        "verified": bool(user.get("verified")),
        "protected": bool(user.get("protected")),
        "nsfw": False,
        "suspended": False,
        "url": "%s/%s" % (ROOT, name),
    }


def list_media_page(handle, cursor=None, limit=30):
    """One page of a creator's media tab -> {tweets, next}.

    Best-effort by design: gallery-dl's cursor advances a whole API page at a
    time, so this asks X for a page of `limit` tweets and returns whatever that
    page yielded. Callers get a short page rather than an exact one. The grid
    dedupes by tweet id, so a boundary overlap is harmless; for exhaustive
    enumeration use iter_all_media instead.
    """
    _require()
    with _lock:
        try:
            _throttle()
            extr = _build("%s/%s/media" % (ROOT, handle),
                          cursor=cursor, page_size=limit)
            tweets = list(_iter_tweets(extr, max_tweets=limit))
            next_cursor = getattr(extr, "_cursor", None)
            # An empty page means the listing is exhausted, whatever the cursor
            # says - X keeps handing back a cursor past the end of a timeline.
            if not tweets:
                next_cursor = None
            return {"tweets": tweets, "next": next_cursor or None}
        except (TwitterAuthError, TwitterNotFound, TwitterUpstreamError):
            raise
        except Exception as e:  # noqa: BLE001
            _translate(e, "@" + handle)


def iter_all_media(handle, should_stop=None, max_items=None):
    """Yield every tweet on a creator's media tab, oldest page last.

    One uninterrupted generator, so no cursor is ever persisted and nothing is
    dropped at a page boundary. Bounded by TWITTER_MAX_ITEMS because X throttles
    deep paging hard and an unbounded walk of a large account gets the session
    rate-limited rather than finishing.
    """
    _require()
    limit = max_items or config.TWITTER_MAX_ITEMS
    with _lock:
        try:
            _throttle()
            extr = _build("%s/%s/media" % (ROOT, handle))
            count = 0
            for tweet in _iter_tweets(extr, should_stop=should_stop):
                yield tweet
                count += 1
                if limit and count >= limit:
                    logger.info(
                        "Stopping X enumeration for %s at %d items "
                        "(TWITTER_MAX_ITEMS).", handle, count)
                    return
        except (TwitterAuthError, TwitterNotFound, TwitterUpstreamError):
            raise
        except Exception as e:  # noqa: BLE001
            _translate(e, "@" + handle)


def get_tweet(tweet_id):
    """One tweet by ID, or None. Used to re-resolve a selected item server-side."""
    _require()
    with _lock:
        try:
            _throttle()
            extr = _build("%s/i/web/status/%s" % (ROOT, tweet_id))
            for tweet in _iter_tweets(extr, max_tweets=1):
                return tweet
            return None
        except Exception as e:  # noqa: BLE001
            # A deleted tweet is expected during a job (the caller records it as
            # gone); anything else is a real failure and must not be silently
            # counted as a deletion.
            if _is_missing(e):
                return None
            _translate(e, "tweet %s" % tweet_id)
