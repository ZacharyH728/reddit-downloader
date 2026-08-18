"""Reddit access: auth, creator search/listing, and the per-post dispatch logic.

The four media branches that used to live inline in main()'s loop are lifted into
`describe_submission()`, a pure function that turns a PRAW Submission into a plain
dict. The saved-posts sync and the creator-download jobs then share one code path,
which is what keeps their filenames (and the `_1`/`_2` gallery convention the
sibling gallery app parses) identical.

Two PRAW hazards this module works around:

* Optional fields are read out of `post.__dict__`, never with getattr(). PRAW
  objects are lazy: touching an attribute that isn't in the response fires an
  HTTP request. Doing that inside a loop over 100 listing items is an instant
  rate-limit.
* `preview` / `media_metadata` URLs arrive HTML-escaped, because PRAW does not
  set `raw_json=1`. They must be unescaped, and their query strings must be left
  exactly as-is: preview.redd.it URLs are HMAC-signed and editing `width=` or
  dropping `s=` turns them into 403s.
"""
import datetime
import html
import os
import re
import threading
from urllib.parse import urlsplit

import praw
import prawcore

from core import config, net
from core.config import logger
from core.manifest import name_taken, resolve_filename, sanitize_title
from core.redgifs import RedGifsGone, gif_media_url, gif_preview_url, gif_thumb_url

# Extensions the original accepted for direct image links. Anything else is skipped.
IMAGE_EXTS = ['jpg', 'jpeg', 'png', 'gif']
# Path segments that are route prefixes, never gif ids.
_REDGIFS_PATH_NOISE = frozenset(
    ["watch", "ifr", "i", "v", "gifs", "detail", "users", "gallery", "embed"])
# Shortest plausible gif id. RedGifs ids are long concatenated words.
_REDGIFS_MIN_ID = 3
# Placeholder values Reddit uses in `thumbnail` when there is no real thumbnail.
_THUMB_PLACEHOLDERS = frozenset(["self", "default", "nsfw", "spoiler", "image", ""])
# Only worth spending an API call on an exact-name lookup if it could be a name.
_PLAUSIBLE_USERNAME = re.compile(r"\A[A-Za-z0-9_-]{2,20}\Z")
# Target width for grid thumbnails, picked from Reddit's pre-scaled preview ladder.
_THUMB_TARGET_WIDTH = 640

_local = threading.local()


def _raw(obj, key, default=None):
    """Read an attribute without triggering PRAW's lazy fetch."""
    return obj.__dict__.get(key, default)


def _clean_url(url):
    """Unescape an API-provided URL. Never rewrite its query string."""
    if not url:
        return None
    return html.unescape(url)


def redgifs_id_from_url(url):
    """Pull a gif id out of any of RedGifs' URL shapes, or return None.

    They use /watch/<id>, /ifr/<id>, /i/<id> and bare /<id>. Matching with an
    optional prefix group is the obvious approach and is wrong: on /i/<id> the
    prefix group doesn't match, so the capture happily returns "i" as the id.
    Taking the last meaningful path segment can't make that mistake, and copes
    with prefixes we haven't seen.
    """
    if not url:
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host != "redgifs.com" and not host.endswith(".redgifs.com"):
        return None
    for segment in reversed([s for s in parts.path.split("/") if s]):
        candidate = segment.split(".")[0]
        if candidate.lower() in _REDGIFS_PATH_NOISE:
            continue
        if len(candidate) >= _REDGIFS_MIN_ID and candidate.isalnum():
            return candidate
    return None


# --- authentication ---------------------------------------------------------

def make_reddit(session):
    return praw.Reddit(
        client_id=config.CLIENT_ID,
        client_secret=config.CLIENT_SECRET,
        user_agent=config.USER_AGENT,
        username=config.USERNAME,
        password=config.PASSWORD,
        requestor_kwargs={'session': session},
        # Otherwise praw prints an "outdated version" notice to stdout on first
        # use, interleaved with the application's own logs.
        check_for_updates=False,
    )


def get_reddit(interactive=False):
    """One praw.Reddit (and one requests.Session) per thread.

    prawcore's token refresh isn't locked, so sharing a single Reddit instance
    between the sync thread and the web workers produces rare, confusing 401s.
    """
    attr = "reddit_interactive" if interactive else "reddit"
    existing = getattr(_local, attr, None)
    if existing is None:
        session = net.make_interactive_session() if interactive else net.make_session()
        existing = make_reddit(session)
        setattr(_local, attr, existing)
    return existing


# --- creator search and listing --------------------------------------------

def _redditor_result(obj):
    """Normalize one `users/search` hit.

    The endpoint can return either a Redditor (t2) or a user-profile Subreddit
    (t5, whose display_name is `u_<name>`); PRAW's own type annotation and
    docstring disagree about which, so handle both.
    """
    display_name = _raw(obj, "display_name")
    if display_name and display_name.lower().startswith("u_"):
        name = display_name[2:]
        subscribers = _raw(obj, "subscribers")
        avatar = _clean_url(_raw(obj, "icon_img") or _raw(obj, "community_icon"))
        nsfw = bool(_raw(obj, "over18") or _raw(obj, "over_18"))
        title = _raw(obj, "title") or name
    else:
        name = _raw(obj, "name")
        if not name:
            return None
        subscribers = _raw(obj, "link_karma")
        avatar = _clean_url(_raw(obj, "icon_img"))
        nsfw = False
        title = name

    if not name:
        return None
    return {
        "platform": "reddit",
        "name": name,
        "display": title or name,
        "avatar": avatar,
        "count": subscribers,
        "nsfw": nsfw,
        "url": "https://www.reddit.com/user/%s" % name,
    }


def search_redditors(reddit, query, limit=25):
    """Search users by name, with an exact-name fallback.

    `users/search` is a fuzzy index and frequently misses an exact handle, which
    is the single most common thing someone types into a search box.
    """
    results = []
    seen = set()
    try:
        for obj in reddit.redditors.search(query, limit=limit):
            item = _redditor_result(obj)
            if item and item["name"].lower() not in seen:
                seen.add(item["name"].lower())
                results.append(item)
    except prawcore.exceptions.PrawcoreException as e:
        logger.error("Reddit user search failed for %r: %s", query, e)
        if not results:
            raise

    if query.lower() not in seen and _PLAUSIBLE_USERNAME.match(query or ""):
        exact = get_redditor(reddit, query)
        if exact:
            results.insert(0, exact)
    return results


def get_redditor(reddit, username):
    """Profile for one redditor, or None if missing/suspended.

    NotFound is raised on first *attribute access*, not by reddit.redditor(),
    because the object is lazy.
    """
    try:
        redditor = reddit.redditor(username)
        # Force the fetch, then read everything out of the response.
        if getattr(redditor, "is_suspended", False):
            return {
                "platform": "reddit",
                "name": _raw(redditor, "name") or username,
                "display": _raw(redditor, "name") or username,
                "avatar": None,
                "count": None,
                "nsfw": False,
                "suspended": True,
                "url": "https://www.reddit.com/user/%s" % username,
            }
        return {
            "platform": "reddit",
            "name": _raw(redditor, "name") or username,
            "display": _raw(redditor, "name") or username,
            "avatar": _clean_url(_raw(redditor, "icon_img")),
            "count": _raw(redditor, "link_karma"),
            "created_utc": _raw(redditor, "created_utc"),
            "nsfw": False,
            "suspended": False,
            "url": "https://www.reddit.com/user/%s" % username,
        }
    except (prawcore.exceptions.NotFound, prawcore.exceptions.Forbidden):
        return None
    except AttributeError:
        # Suspended/shadowbanned accounts expose almost nothing.
        return None
    except prawcore.exceptions.PrawcoreException as e:
        logger.error("Reddit profile lookup failed for %s: %s", username, e)
        return None


def list_redditor_posts(reddit, username, limit=30, after=None, sort="new"):
    """One page of a redditor's submissions -> (submissions, next_cursor).

    Paged manually rather than with limit=None so the UI can load incrementally.
    The cursor is the last item's fullname, which needs no extra request. Reddit
    caps this listing at roughly 1000 items regardless of paging.
    """
    listing = reddit.redditor(username).submissions
    method = {
        "new": listing.new,
        "top": listing.top,
        "hot": listing.hot,
    }.get(sort, listing.new)

    # `params` must be omitted entirely on the first page, not passed as None.
    # PRAW's _safely_add_arguments does `deepcopy(arguments[key]).update(...)`
    # when the key is present, so an explicit None raises AttributeError.
    kwargs = {"limit": limit}
    if after:
        kwargs["params"] = {"after": after}
    generator = method(**kwargs)
    items = list(generator)
    # A short page means the listing is exhausted.
    cursor = items[-1].fullname if len(items) >= limit and items else None
    return items, cursor


def resolve_submissions(reddit, post_ids):
    """Re-fetch submissions by ID, batched (100 per request).

    The web layer accepts item IDs only, never URLs - taking a client-supplied
    download URL would make the download endpoint an arbitrary-fetch primitive.
    """
    fullnames = ["t3_" + pid for pid in post_ids if pid]
    if not fullnames:
        return []
    found = list(reddit.info(fullnames=fullnames))
    # reddit.info() silently omits anything it can't match; preserve the caller's
    # order so progress reporting lines up with what the user selected.
    by_id = {getattr(s, "id", None): s for s in found}
    return [by_id[pid] for pid in post_ids if pid in by_id]


# --- describing a post ------------------------------------------------------

def _preview_from_images(post):
    """Pick a grid thumbnail out of Reddit's pre-scaled preview ladder."""
    preview = _raw(post, "preview") or {}
    images = preview.get("images") or []
    if not images:
        return None
    first = images[0]
    candidates = first.get("resolutions") or []
    chosen = None
    for res in candidates:
        if res.get("width", 0) >= _THUMB_TARGET_WIDTH:
            chosen = res
            break
    if chosen is None and candidates:
        chosen = candidates[-1]
    if chosen is None:
        chosen = first.get("source") or {}
    return _clean_url(chosen.get("url"))


def _gallery_thumb(post, media_id):
    """Thumbnail for a gallery item from its media_metadata preview ladder."""
    meta = (_raw(post, "media_metadata") or {}).get(media_id) or {}
    ladder = meta.get("p") or []
    for entry in ladder:
        if entry.get("x", 0) >= _THUMB_TARGET_WIDTH:
            return _clean_url(entry.get("u"))
    if ladder:
        return _clean_url(ladder[-1].get("u"))
    return _clean_url((meta.get("s") or {}).get("u"))


def _fallback_thumb(post):
    thumb = _raw(post, "thumbnail")
    if thumb and thumb not in _THUMB_PLACEHOLDERS and thumb.startswith("http"):
        return _clean_url(thumb)
    return None


def describe_submission(post):
    """Turn a Submission into a plain dict, or None if we can't download it.

    Pure: no network, no filesystem. The branch conditions are carried over from
    the original dispatch chain unchanged.
    """
    post_id = _raw(post, "id")
    title = _raw(post, "title")
    url = _raw(post, "url")
    if not post_id or title is None or not url:
        return None

    base = {
        "source": "reddit",
        "id": post_id,
        "title": title,
        "created_utc": _raw(post, "created_utc"),
        "nsfw": bool(_raw(post, "over_18")),
        "permalink": "https://www.reddit.com" + (_raw(post, "permalink") or ""),
        "thumb": _preview_from_images(post) or _fallback_thumb(post),
        "count": 1,
        "duration": None,
        "width": None,
        "height": None,
        "avg_color": None,
    }

    # --- Galleries ---
    if _raw(post, "is_gallery"):
        gallery_data = _raw(post, "gallery_data") or {}
        media_metadata = _raw(post, "media_metadata") or {}
        items = []
        for entry in gallery_data.get("items") or []:
            media_id = entry.get("media_id")
            # A removed gallery image leaves a dangling media_id. Skipping it
            # keeps the rest of the gallery downloadable; the original raised
            # KeyError here and abandoned the whole post, permanently.
            if not media_id or media_id not in media_metadata:
                continue
            mime = (media_metadata[media_id] or {}).get("m") or ""
            ext = mime.split("/")[-1]
            if not ext:
                continue
            items.append({
                "url": "https://i.redd.it/%s.%s" % (media_id, ext),
                "ext": ext,
                "thumb": _gallery_thumb(post, media_id),
            })
        if not items:
            return None
        base.update({
            "kind": "gallery",
            "items": items,
            "count": len(items),
            "preview": items[0]["thumb"] or items[0]["url"],
            "preview_type": "image",
        })
        if not base["thumb"]:
            base["thumb"] = items[0]["thumb"] or items[0]["url"]
        return base

    # --- i.redd.it and i.imgur.com ---
    if "i.redd.it" in url or "i.imgur.com" in url:
        ext = url.split('.')[-1].lower()
        if ext not in IMAGE_EXTS:
            return None
        base.update({
            "kind": "image",
            "ext": ext,
            "url": url,
            "preview": url,
            "preview_type": "image",
        })
        if not base["thumb"]:
            base["thumb"] = url
        return base

    # --- RedGifs ---
    if "redgifs.com" in url:
        gif_id = redgifs_id_from_url(url)
        if not gif_id:
            return None
        base.update({
            "kind": "redgifs",
            "ext": "mp4",
            "redgifs_id": gif_id,
            "url": url,
            # The RedGifs CDN URL isn't in the Reddit response; the lightbox
            # resolves it lazily via /api/redgifs/gif/<id>.
            "preview": None,
            "preview_type": "redgifs",
        })
        return base

    # --- v.redd.it (native Reddit-hosted video) ---
    if "v.redd.it" in url:
        media = _raw(post, "media") or {}
        reddit_video = media.get("reddit_video") or {}
        fallback = reddit_video.get("fallback_url")
        if not fallback:
            return None
        base.update({
            "kind": "video",
            "ext": "mp4",
            "video_url": fallback,
            "url": url,
            "duration": reddit_video.get("duration"),
            "width": reddit_video.get("width"),
            "height": reddit_video.get("height"),
            # fallback_url is the video-only DASH track, so this preview is
            # silent. The download muxes the audio track back in.
            "preview": fallback,
            "preview_type": "video",
        })
        return base

    return None


def describe_gif(gif):
    """Turn a RedGifs API gif object into the same shape as describe_submission."""
    gif_id = (gif or {}).get("id")
    if not gif_id:
        return None
    media_url = gif_media_url(gif)
    if not media_url:
        return None
    is_image = gif.get("type") == 2
    ext = "jpg" if is_image else "mp4"
    return {
        "source": "redgifs",
        "id": gif_id,
        "kind": "image" if is_image else "video",
        "title": gif_id,
        "created_utc": gif.get("createDate"),
        "nsfw": True,
        "permalink": "https://www.redgifs.com/watch/%s" % gif_id,
        "ext": ext,
        "url": media_url,
        "video_url": None if is_image else media_url,
        "thumb": gif_thumb_url(gif),
        "preview": media_url if is_image else gif_preview_url(gif),
        "preview_type": "image" if is_image else "video",
        "count": 1,
        "duration": gif.get("duration"),
        "width": gif.get("width"),
        "height": gif.get("height"),
        "avg_color": gif.get("avgColor"),
        "has_audio": gif.get("hasAudio"),
    }


def describe_tweet(tweet):
    """Turn a core.twitter tweet dict into the same shape as describe_submission.

    A tweet carries up to four media files, so a multi-image tweet becomes a
    `gallery` - which is what makes it inherit the existing `_1`/`_2` filename
    convention the sibling gallery app already parses. A single-file tweet stays
    a plain image/video so it doesn't get a pointless `_1` suffix.
    """
    tweet_id = str((tweet or {}).get("id") or "")
    files = (tweet or {}).get("files") or []
    if not tweet_id or not files:
        return None

    # X serves animated GIFs as silent MP4s. They are videos as far as
    # downloading is concerned, and the lightbox notes the missing audio.
    def is_video(f):
        return f.get("type") in ("video", "animated_gif")

    first = files[0]
    base = {
        "source": "twitter",
        "id": tweet_id,
        # Tweets have no title. The text is used for the filename stem when it
        # says anything; plan_filenames falls back to a dated ID otherwise.
        "title": tweet.get("text") or "",
        "created_utc": tweet.get("created_utc"),
        "nsfw": bool(tweet.get("sensitive")),
        "permalink": "https://x.com/%s/status/%s" % (
            tweet.get("handle") or "i/web", tweet_id),
        "count": len(files),
        "duration": first.get("duration"),
        "width": first.get("width"),
        "height": first.get("height"),
        "avg_color": None,
        "has_audio": first.get("type") == "video",
    }

    if len(files) > 1:
        base.update({
            "kind": "gallery",
            "items": [{
                "url": f["url"],
                "ext": f["ext"],
                # pbs.twimg.com resizes by query parameter, so a grid thumbnail
                # costs nothing extra to produce.
                "thumb": _twimg_sized(f["url"], "small"),
            } for f in files],
            "preview": _twimg_sized(first["url"], "medium"),
            "preview_type": "image",
            "thumb": _twimg_sized(first["url"], "small"),
        })
        return base

    if is_video(first):
        base.update({
            "kind": "video",
            "ext": first.get("ext") or "mp4",
            "url": first["url"],
            "video_url": first["url"],
            # video.twimg.com MP4s are progressive and honor Range, so the
            # lightbox plays them directly - no lazy resolve step like RedGifs.
            "preview": first["url"],
            "preview_type": "video",
            "thumb": None,
        })
        return base

    base.update({
        "kind": "image",
        "ext": first.get("ext") or "jpg",
        "url": first["url"],
        "preview": _twimg_sized(first["url"], "medium"),
        "preview_type": "image",
        "thumb": _twimg_sized(first["url"], "small"),
    })
    return base


def _twimg_sized(url, name):
    """Ask pbs.twimg.com for a scaled variant of an image URL.

    Their URLs carry the size in a `name=` parameter (`...?format=jpg&name=orig`).
    Anything that isn't a pbs.twimg.com media URL - a video, notably - is returned
    untouched, because rewriting a video URL's query breaks it.
    """
    if not url or "pbs.twimg.com" not in url or "name=" not in url:
        return url
    return re.sub(r"([?&]name=)[^&]*", r"\g<1>" + name, url)


# --- filenames --------------------------------------------------------------

def _dated_stem(created, item_id):
    """`YYYYMMDD_<id>`, or just `<id>` when the timestamp is missing or bogus."""
    if not created:
        return item_id
    try:
        stamp = datetime.datetime.fromtimestamp(
            float(created), datetime.timezone.utc).strftime("%Y%m%d")
    except (ValueError, OSError, OverflowError, TypeError):
        return item_id
    return "%s_%s" % (stamp, item_id)


def plan_filenames(desc, owned_files, max_len=None):
    """Decide which filenames `desc` will occupy. Does not touch the disk."""
    post_id = desc["id"]

    if desc["source"] == "redgifs":
        # RedGifs items have no title. A date prefix sorts chronologically in any
        # file browser, and the alphanumeric ID can never end in `_<digits>`, so
        # this cannot accidentally look like a `_1`/`_2` gallery to the viewer.
        return ["%s.%s" % (_dated_stem(desc.get("created_utc"), post_id), desc["ext"])]

    if desc["source"] == "twitter":
        # Tweet text is frequently empty, an emoji, or a bare link, so it can't
        # be the primary name. A date prefix sorts chronologically in any file
        # browser and the numeric ID keeps two same-text tweets apart; the text,
        # when there is any, is appended purely so the folder is readable.
        # The `t` is not decoration. A tweet ID is all digits, so a bare
        # `<date>_<id>.jpg` ends in `_<digits>` - exactly the shape the sibling
        # gallery app reads as "page <n> of a gallery". The prefix breaks that.
        stem = _dated_stem(desc.get("created_utc"), "t" + post_id)
        text = sanitize_title(desc["title"], max_len=max_len)
        if text:
            # Budget the text against what the prefix already spent, so the whole
            # name still fits the byte limit plan_filenames is given.
            room = (max_len - len(stem) - 1) if max_len else None
            if room is None or room > 16:
                trimmed = sanitize_title(desc["title"], max_len=room) if room else text
                if trimmed:
                    stem = "%s_%s" % (stem, trimmed)
        if desc["kind"] == "gallery":
            return ["%s_%d.%s" % (stem, i + 1, item["ext"])
                    for i, item in enumerate(desc["items"])]
        return ["%s.%s" % (stem, desc["ext"])]

    base = sanitize_title(desc["title"], max_len=max_len) or post_id

    if desc["kind"] == "gallery":
        items = desc["items"]
        # If the base name is already taken by a different post, suffix the whole
        # gallery with the post ID - kept BEFORE the _<n> index so the viewer's
        # gallery grouping (…_1, …_2) still matches.
        first_ext = items[0]["ext"]
        if name_taken(owned_files, "%s_1.%s" % (base, first_ext)):
            base = "%s_%s" % (base, post_id)
        return ["%s_%d.%s" % (base, i + 1, item["ext"]) for i, item in enumerate(items)]

    return [resolve_filename(base, desc["ext"], post_id, owned_files)]


# --- downloading ------------------------------------------------------------

def download_planned(desc, filenames, dest_dir, session, redgifs=None, should_cancel=None):
    """Download everything `desc` refers to into `dest_dir`.

    Returns {"status": ..., "error": ...} where status is one of
    "downloaded" / "skipped" / "failed" / "gone" / "cancelled".
    """
    kind = desc["kind"]
    statuses = []

    def path_of(name):
        return os.path.join(dest_dir, name)

    try:
        if kind == "gallery":
            for item, name in zip(desc["items"], filenames):
                statuses.append(net.download_file(
                    item["url"], path_of(name), session=session,
                    should_cancel=should_cancel, expect_ext=item["ext"]))

        elif kind == "redgifs":
            name = filenames[0]
            target = path_of(name)
            # Claim an already-downloaded RedGif without ever touching the API.
            if os.path.exists(target):
                statuses.append(net.SKIPPED)
            else:
                try:
                    meta = redgifs.get_media_info(desc["redgifs_id"])
                except RedGifsGone:
                    return {"status": net.GONE, "error": "deleted upstream"}
                if not meta:
                    return {"status": net.FAILED, "error": "no RedGifs metadata"}
                hd_url = gif_media_url(meta.get("gif") or {})
                if not hd_url:
                    return {"status": net.FAILED, "error": "no HD URL for RedGif"}
                statuses.append(net.download_file(
                    hd_url, target, session=redgifs.media_session, check_size=True,
                    should_cancel=should_cancel, expect_ext="mp4"))

        elif kind == "video" and desc.get("video_url"):
            name = filenames[0]
            target = path_of(name)
            if os.path.exists(target):
                statuses.append(net.SKIPPED)
            elif desc["source"] == "reddit":
                # v.redd.it only: video and audio are separate DASH tracks that
                # have to be muxed. Tested on `source`, not on "not redgifs" -
                # every other platform serves a single progressive MP4, and
                # sending one through the muxer fetches a bogus audio URL.
                statuses.append(net.download_reddit_video(
                    desc["video_url"], target, session,
                    should_cancel=should_cancel, label=name))
            else:
                statuses.append(net.download_file(
                    desc["video_url"], target, session=session,
                    should_cancel=should_cancel, expect_ext=desc["ext"]))

        elif kind == "image":
            name = filenames[0]
            statuses.append(net.download_file(
                desc["url"], path_of(name), session=session,
                should_cancel=should_cancel, expect_ext=desc["ext"]))

        else:
            return {"status": net.FAILED, "error": "no handler for kind %r" % kind}

    except OSError:
        # Out of space / permissions: the caller aborts the job rather than
        # grinding through hundreds of identical failures.
        raise

    if net.CANCELLED in statuses:
        return {"status": net.CANCELLED, "error": None}
    if net.FAILED in statuses:
        return {"status": net.FAILED, "error": "one or more files failed"}
    if net.GONE in statuses:
        return {"status": net.GONE, "error": "deleted upstream"}
    if net.DOWNLOADED in statuses:
        return {"status": net.DOWNLOADED, "error": None}
    return {"status": net.SKIPPED, "error": None}
