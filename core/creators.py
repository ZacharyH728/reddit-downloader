"""Platform-agnostic creator search, listing, and local "do I have it?" state.

This is the layer the web API talks to. It hides the two very different paging
models behind one shape: Reddit pages by an opaque `after` cursor (the last
item's fullname) and caps out around 1000 items; RedGifs pages by offset and
reports a real total up front.
"""
import os
import threading

import prawcore

from core import config, net, reddit_api, twitter
from core.config import logger
from core.manifest import Manifest
from core.redgifs import RedGifsClient, RedGifsGone
from core.twitter import (TwitterAuthError, TwitterNotFound, TwitterUnavailable,
                          TwitterUpstreamError)
from core.validate import (PLATFORMS, ValidationError, safe_child_dir, validate_creator,
                           validate_platform)

# Reddit will not serve more than roughly this many items from a user listing,
# no matter how the request is paged.
REDDIT_LISTING_CAP = 1000

_redgifs = None
_redgifs_lock = threading.Lock()


def get_redgifs():
    """One shared client: the rate limit and the IP/UA-bound token are global."""
    global _redgifs
    with _redgifs_lock:
        if _redgifs is None:
            _redgifs = RedGifsClient()
        return _redgifs


def creator_dir(platform, name):
    """Absolute path to <CREATOR_ROOT>/<platform>/<creator>/, validated."""
    platform = validate_platform(platform)
    name = validate_creator(platform, name)
    return safe_child_dir(config.CREATOR_ROOT, platform, name)


def creator_label(platform, name):
    """Path shown in the UI: relative when that's shorter, absolute otherwise."""
    try:
        absolute = creator_dir(platform, name)
    except ValidationError:
        return os.path.join(platform, name)
    try:
        relative = os.path.relpath(absolute, os.getcwd())
    except ValueError:
        return absolute
    # A relative path that climbs out of the cwd is less readable than the real one.
    return absolute if relative.startswith("..") else relative


# --- the flat saved-posts manifest, cached ---------------------------------

_saved_cache = {"stamp": None, "posts": frozenset()}
_saved_lock = threading.Lock()


def saved_post_ids():
    """IDs already downloaded by the hourly saved-posts sync.

    Used only to show an "in saved" hint in the grid, so the user doesn't keep a
    second copy of something they already have. Re-read when the file changes.
    """
    path = os.path.join(config.DOWNLOAD_LOCATION, config.MANIFEST_NAME)
    try:
        stat = os.stat(path)
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return frozenset()
    with _saved_lock:
        if _saved_cache["stamp"] == stamp:
            return _saved_cache["posts"]
    posts = frozenset(Manifest(config.DOWNLOAD_LOCATION).posts.keys())
    with _saved_lock:
        _saved_cache["stamp"] = stamp
        _saved_cache["posts"] = posts
    return posts


# --- search ---------------------------------------------------------------

def search(query, platform="both", limit=25):
    """Search creators. One platform failing must not fail the whole response."""
    query = (query or "").strip()
    out = {}

    if platform in ("both", "reddit"):
        section = {"results": [], "error": None}
        try:
            reddit = reddit_api.get_reddit(interactive=True)
            section["results"] = reddit_api.search_redditors(reddit, query, limit=limit)
        except prawcore.exceptions.TooManyRequests as e:
            section["error"] = "Reddit is rate limiting us; try again shortly."
            section["retry_after"] = getattr(e, "retry_after", None)
        except Exception as e:  # noqa: BLE001 - one source must not break the other
            logger.error("Reddit search failed for %r: %s", query, e)
            section["error"] = "Reddit search is unavailable."
        out["reddit"] = section

    if platform in ("both", "redgifs"):
        section = {"results": [], "error": None}
        try:
            data = get_redgifs().search_creators(query, page=1, count=limit)
            if data is None:
                section["error"] = "RedGifs search is unavailable."
            else:
                section["results"] = [_redgifs_result(item) for item in data["items"]]
                section["page"] = data["page"]
                section["pages"] = data["pages"]
                section["total"] = data["total"]
                if query.lower() not in {r["name"].lower() for r in section["results"]}:
                    # Fuzzy search often misses an exact handle, which is the
                    # most likely thing someone typed.
                    try:
                        exact = get_creator_profile("redgifs", query, quiet=True)
                    except ValidationError:
                        exact = None
                    if exact:
                        section["results"].insert(0, exact)
        except Exception as e:  # noqa: BLE001
            logger.error("RedGifs search failed for %r: %s", query, e)
            section["error"] = "RedGifs search is unavailable."
        out["redgifs"] = section

    if platform in ("both", "twitter"):
        # Exact handles only. X's user-search endpoints need the same logged-in
        # session and gallery-dl does not expose them, so there is no fuzzy
        # index to query - `query` is either a handle or it is nothing.
        section = {"results": [], "error": None, "exact_only": True}
        if not twitter.available():
            section["error"] = twitter.unavailable_reason()
        else:
            try:
                profile = get_creator_profile("twitter", query, quiet=True)
                if profile:
                    section["results"] = [profile]
                else:
                    section["error"] = "No X account with that exact handle."
            except TwitterAuthError:
                section["error"] = ("X rejected the saved session; refresh "
                                    "TWITTER_AUTH_TOKEN.")
            except ValidationError:
                section["error"] = "That is not a valid X handle."
            except TwitterUpstreamError:
                section["error"] = "X is unavailable."
            except Exception as e:  # noqa: BLE001
                logger.error("X lookup failed for %r: %s", query, e)
                section["error"] = "X is unavailable."
        out["twitter"] = section

    # Annotate anything already present on disk.
    for section in out.values():
        for result in section["results"]:
            try:
                directory = creator_dir(result["platform"], result["name"])
            except ValidationError:
                result["have_files"] = 0
                continue
            result["have_files"] = Manifest(directory).stats()["files"] if \
                os.path.isdir(directory) else 0
    return out


def _redgifs_result(item):
    return {
        "platform": "redgifs",
        "name": item.get("username") or "",
        "display": item.get("name") or item.get("username") or "",
        "avatar": item.get("profileImageUrl"),
        "count": item.get("publishedGifs") or item.get("gifs"),
        "verified": bool(item.get("verified")),
        "nsfw": True,
        "suspended": (item.get("status") or "active") != "active",
        "url": item.get("url") or ("https://www.redgifs.com/users/%s"
                                   % (item.get("username") or "")),
    }


def get_creator_profile(platform, name, quiet=False):
    """One creator's profile, or None if they don't exist."""
    platform = validate_platform(platform)
    name = validate_creator(platform, name)
    if platform == "reddit":
        try:
            return reddit_api.get_redditor(reddit_api.get_reddit(interactive=True), name)
        except Exception as e:  # noqa: BLE001
            if not quiet:
                logger.error("Reddit profile lookup failed for %s: %s", name, e)
            return None
    if platform == "twitter":
        return twitter.get_user(name)
    user = get_redgifs().get_user(name)
    return _redgifs_result(user) if user else None


# --- listing --------------------------------------------------------------

# Which item kinds each UI type filter accepts. Anything not listed here
# (including "all") means "no filtering at all".
#
# The grid and a "download everything" job must agree on what a filter selects,
# or the button downloads a different set than the one on screen — so both go
# through kind_matches instead of each rolling its own mapping.
KIND_FILTERS = {
    "video": frozenset(("video", "redgifs")),
    "image": frozenset(("image",)),
    "gallery": frozenset(("gallery",)),
}


def kind_matches(kind, item_kind):
    wanted = KIND_FILTERS.get(kind or "all")
    return wanted is None or item_kind in wanted


def _annotate(desc, manifest, saved_ids, max_len):
    """Attach local state to a described item, without downloading anything."""
    filenames = reddit_api.plan_filenames(desc, manifest.owned, max_len=max_len)
    if manifest.has_post(desc["id"]):
        have, reason = True, "manifest"
    elif filenames and all(manifest.have_file(n) for n in filenames):
        have, reason = True, "file"
    else:
        have, reason = False, None
    return {
        "id": desc["id"],
        # The lightbox needs to tell an X video (silent GIF, nothing missing)
        # apart from a v.redd.it one (audio muxed in only at download time).
        "source": desc.get("source"),
        "has_audio": desc.get("has_audio"),
        "kind": desc["kind"],
        "title": desc["title"],
        "created_utc": desc.get("created_utc"),
        "nsfw": bool(desc.get("nsfw")),
        "permalink": desc.get("permalink"),
        "thumb": desc.get("thumb"),
        "preview": desc.get("preview"),
        "preview_type": desc.get("preview_type"),
        "count": desc.get("count") or 1,
        "duration": desc.get("duration"),
        "width": desc.get("width"),
        "height": desc.get("height"),
        "avg_color": desc.get("avg_color"),
        "redgifs_id": desc.get("redgifs_id"),
        "gallery": [i.get("thumb") or i.get("url") for i in desc.get("items", [])],
        "have": have,
        "have_reason": reason,
        "in_saved": desc["id"] in saved_ids,
        "filenames": filenames,
        "gone": manifest.is_gone(desc["id"]),
    }


def list_items(platform, name, cursor=None, limit=30, sort="new", kind="all", only="all"):
    """One page of a creator's content, annotated with local state."""
    platform = validate_platform(platform)
    name = validate_creator(platform, name)
    directory = creator_dir(platform, name)
    manifest = Manifest(directory)
    saved_ids = saved_post_ids()
    max_len = config.CREATOR_TITLE_MAX_LEN

    result = {
        "platform": platform,
        "creator": name,
        "dest": creator_label(platform, name),
        "items": [],
        "next": None,
        "total": None,
        "page": None,
        "pages": None,
        "truncated_reason": None,
    }

    if platform == "reddit":
        reddit = reddit_api.get_reddit(interactive=True)
        posts, next_cursor = reddit_api.list_redditor_posts(
            reddit, name, limit=limit, after=cursor, sort=sort)
        descs = [d for d in (reddit_api.describe_submission(p) for p in posts) if d]
        result["next"] = next_cursor
    elif platform == "twitter":
        if not twitter.available():
            raise TwitterUnavailable(twitter.unavailable_reason())
        try:
            data = twitter.list_media_page(name, cursor=cursor, limit=limit)
        except TwitterNotFound:
            raise CreatorUnavailable(platform, name) from None
        descs = [d for d in (reddit_api.describe_tweet(t) for t in data["tweets"]) if d]
        result["next"] = data["next"]
        # X reports no timeline total, so the grid shows a bare loaded count
        # rather than a fraction. `total` stays None on purpose.
        result["truncated_reason"] = "x_no_total"
    else:
        page = 1
        if cursor:
            try:
                page = max(1, int(cursor))
            except ValueError:
                page = 1
        data = get_redgifs().list_creator_gifs(name, page=page, count=limit)
        if data is None:
            raise CreatorUnavailable(platform, name)
        descs = [d for d in (reddit_api.describe_gif(g) for g in data["gifs"]) if d]
        result["total"] = data["total"]
        result["page"] = data["page"]
        result["pages"] = data["pages"]
        result["next"] = None if data["end"] else str(data["page"] + 1)

    items = [_annotate(d, manifest, saved_ids, max_len) for d in descs]

    items = [i for i in items if kind_matches(kind, i["kind"])]
    if only == "missing":
        items = [i for i in items if not i["have"]]
    elif only == "have":
        items = [i for i in items if i["have"]]

    result["items"] = items
    return result


class CreatorUnavailable(Exception):
    def __init__(self, platform, name):
        super().__init__("%s creator %r is unavailable" % (platform, name))
        self.platform = platform
        self.name = name


def get_creator(platform, name):
    """Profile plus local download stats, for the creator screen header."""
    platform = validate_platform(platform)
    name = validate_creator(platform, name)
    profile = get_creator_profile(platform, name)
    directory = creator_dir(platform, name)
    have = Manifest(directory).stats() if os.path.isdir(directory) else \
        {"items": 0, "files": 0, "bytes": 0}
    return {
        "platform": platform,
        "creator": name,
        "profile": profile,
        "have": have,
        "dest": creator_label(platform, name),
        "listing_cap": {
            "reddit": REDDIT_LISTING_CAP,
            "twitter": config.TWITTER_MAX_ITEMS,
        }.get(platform),
    }


def local_library():
    """Creators already downloaded, read straight from disk.

    Lets the user get back to a creator without hitting either API - useful when
    a source is down, or the creator has since been suspended.
    """
    out = []
    for platform in PLATFORMS:
        base = os.path.join(config.CREATOR_ROOT, platform)
        if not os.path.isdir(base):
            continue
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            continue
        for entry in entries:
            directory = os.path.join(base, entry)
            if not os.path.isdir(directory):
                continue
            try:
                validate_creator(platform, entry)
            except ValidationError:
                continue
            manifest = Manifest(directory)
            stats = manifest.stats()
            if not stats["items"] and not stats["files"]:
                continue
            try:
                updated = os.path.getmtime(manifest.path)
            except OSError:
                updated = None
            out.append({
                "platform": platform,
                "creator": entry.lower(),
                "items": stats["items"],
                "files": stats["files"],
                "bytes": stats["bytes"],
                "updated_at": updated,
            })
    out.sort(key=lambda c: c["updated_at"] or 0, reverse=True)
    return out


# --- resolving items for download ------------------------------------------

def resolve_selected(platform, name, item_ids):
    """Re-resolve chosen items server-side, from IDs only.

    The API deliberately never accepts a media URL from the client; doing so
    would turn the download endpoint into an arbitrary-fetch-and-write.
    """
    platform = validate_platform(platform)
    if platform == "reddit":
        reddit = reddit_api.get_reddit()
        posts = reddit_api.resolve_submissions(reddit, item_ids)
        found = {}
        for post in posts:
            desc = reddit_api.describe_submission(post)
            if desc:
                found[desc["id"]] = desc
        return [found[i] for i in item_ids if i in found], \
               [i for i in item_ids if i not in found]

    if platform == "twitter":
        descs = []
        missing = []
        for item_id in item_ids:
            tweet = twitter.get_tweet(item_id)
            desc = reddit_api.describe_tweet(tweet) if tweet else None
            if desc:
                descs.append(desc)
            else:
                missing.append(item_id)
        return descs, missing

    client = get_redgifs()
    descs = []
    missing = []
    for item_id in item_ids:
        try:
            meta = client.get_media_info(item_id)
        except RedGifsGone:
            missing.append(item_id)
            continue
        gif = (meta or {}).get("gif")
        desc = reddit_api.describe_gif(gif) if gif else None
        if desc:
            descs.append(desc)
        else:
            missing.append(item_id)
    return descs, missing


def iter_all_items(platform, name, page_size=100, should_stop=None, kind="all"):
    """Yield every downloadable item for a creator, page by page.

    Reddit is enumerated eagerly by the caller (it's cheap and unthrottled, so a
    job can report an honest total up front); RedGifs is interleaved with
    downloading, because pre-walking a 5000-gif creator at 1 request/second is
    minutes of dead time before anything happens.

    `kind` applies the same type filter the grid uses, so a "download everything"
    job started from a filtered view takes only what was on screen. Filtering
    happens here rather than in each platform branch below so a new platform
    can't quietly skip it. Note it does not reduce the *paging* — the listing is
    walked in full either way (and Reddit's cap counts unfiltered posts, which is
    correct: the cap is a property of the listing, not of the selection).
    """
    for desc in _iter_all_descs(platform, name, page_size, should_stop):
        if kind_matches(kind, desc["kind"]):
            yield desc


def _iter_all_descs(platform, name, page_size, should_stop):
    should_stop = should_stop or (lambda: False)
    platform = validate_platform(platform)
    name = validate_creator(platform, name)

    if platform == "reddit":
        reddit = reddit_api.get_reddit()
        cursor = None
        seen = 0
        while not should_stop():
            posts, cursor = reddit_api.list_redditor_posts(
                reddit, name, limit=page_size, after=cursor)
            if not posts:
                return
            for post in posts:
                desc = reddit_api.describe_submission(post)
                if desc:
                    yield desc
            seen += len(posts)
            if cursor is None or seen >= REDDIT_LISTING_CAP:
                return
        return

    if platform == "twitter":
        if not twitter.available():
            raise TwitterUnavailable(twitter.unavailable_reason())
        # One uninterrupted generator, deliberately: gallery-dl's cursor moves a
        # whole API page at a time, so resuming a half-consumed page would drop
        # items. See the note in core/twitter.py.
        for tweet in twitter.iter_all_media(name, should_stop=should_stop):
            desc = reddit_api.describe_tweet(tweet)
            if desc:
                yield desc
        return

    client = get_redgifs()
    page = 1
    while not should_stop():
        data = client.list_creator_gifs(name, page=page, count=page_size)
        if data is None:
            raise CreatorUnavailable(platform, name)
        for gif in data["gifs"]:
            desc = reddit_api.describe_gif(gif)
            if desc:
                yield desc
        if data["end"]:
            return
        page += 1


def total_estimate(platform, name):
    """Best-effort item count, for a job's progress bar. None when unknown."""
    platform = validate_platform(platform)
    name = validate_creator(platform, name)
    if platform == "redgifs":
        data = get_redgifs().list_creator_gifs(name, page=1, count=1)
        return data["total"] if data else None
    if platform == "twitter":
        # `media_count` counts media *files*, not tweets, and we enumerate by
        # tweet - so it would over-report and leave the progress bar stuck short
        # of 100%. An honest unknown is better than a wrong number.
        return None
    return None


def resolve_redgifs_media(gif_id):
    """Fresh CDN URLs for one gif, for the lightbox."""
    meta = get_redgifs().get_media_info(gif_id)
    gif = (meta or {}).get("gif")
    if not gif:
        return None
    urls = gif.get("urls") or {}
    return {
        "id": gif_id,
        "hd": urls.get("hd"),
        "sd": urls.get("sd"),
        "silent": urls.get("silent"),
        "poster": urls.get("poster"),
        "thumbnail": urls.get("thumbnail"),
        "duration": gif.get("duration"),
        "has_audio": gif.get("hasAudio"),
        "width": gif.get("width"),
        "height": gif.get("height"),
    }


def free_space_ok(platform, name):
    """True when there's enough headroom to start a job."""
    directory = creator_dir(platform, name)
    free = net.free_disk_mb(directory)
    if free is None:
        return True, None
    return free >= config.MIN_FREE_DISK_MB, free
