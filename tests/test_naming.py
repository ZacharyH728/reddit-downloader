"""Filename derivation tests.

The centerpiece is test_golden_matches_original_implementation: it replays the
pre-refactor naming code (copied verbatim from git history) against a corpus of
posts and asserts the new code produces the identical filenames. That is the
guarantee that no file already sitting in the user's library gets renamed.
"""
import re

import pytest

from core.manifest import name_taken, resolve_filename, sanitize_title
from core.reddit_api import describe_submission, plan_filenames


class FakePost:
    """Stands in for a PRAW Submission: attributes live in __dict__, like the
    real thing after objectification, so _raw() reads them without a fetch."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def image_post(post_id="abc123", title="A Title", ext="jpg"):
    return FakePost(id=post_id, title=title, url="https://i.redd.it/%s.%s" % (post_id, ext),
                    over_18=False, permalink="/r/x/comments/%s/" % post_id,
                    created_utc=1700000000.0)


def gallery_post(post_id="gal123", title="Gallery", exts=("jpg", "png")):
    media_metadata = {}
    items = []
    for i, ext in enumerate(exts):
        media_id = "m%d" % i
        media_metadata[media_id] = {"m": "image/%s" % ext}
        items.append({"media_id": media_id})
    return FakePost(id=post_id, title=title, url="https://reddit.com/gallery/%s" % post_id,
                    is_gallery=True, gallery_data={"items": items},
                    media_metadata=media_metadata, over_18=False,
                    permalink="/r/x/comments/%s/" % post_id, created_utc=1700000000.0)


def redgifs_post(post_id="rg123", title="Clip"):
    return FakePost(id=post_id, title=title, url="https://www.redgifs.com/watch/somegifid",
                    over_18=True, permalink="/r/x/comments/%s/" % post_id,
                    created_utc=1700000000.0)


def video_post(post_id="vid123", title="Video"):
    return FakePost(id=post_id, title=title, url="https://v.redd.it/%s" % post_id,
                    media={"reddit_video": {"fallback_url":
                           "https://v.redd.it/%s/DASH_1080.mp4?source=fallback" % post_id,
                           "duration": 30}},
                    over_18=False, permalink="/r/x/comments/%s/" % post_id,
                    created_utc=1700000000.0)


# --- the original implementation, copied from git history --------------------

def _original_resolve_filename(base, ext, post_id, owned_files):
    name = "{}.{}".format(base, ext)
    if name in owned_files:
        name = "{}_{}.{}".format(base, post_id, ext)
    return name


def _original_filenames(post, owned_files):
    """The naming half of the original main() loop, verbatim in behavior."""
    post_id = post.id
    title = post.title
    sanitized_title = re.sub(r'[\\/*?:"<>|]', "", title)

    if hasattr(post, "is_gallery") and post.is_gallery:
        gallery_items = post.gallery_data['items']
        gallery_base = sanitized_title
        first = gallery_items[0]
        first_type = post.media_metadata[first['media_id']]['m'].split('/')[-1]
        if "{}_1.{}".format(sanitized_title, first_type) in owned_files:
            gallery_base = "{}_{}".format(sanitized_title, post_id)
        names = []
        for i, item in enumerate(gallery_items):
            media_type = post.media_metadata[item['media_id']]['m'].split('/')[-1]
            names.append("{}_{}.{}".format(gallery_base, i + 1, media_type))
        return names

    if "i.redd.it" in post.url or "i.imgur.com" in post.url:
        file_extension = post.url.split('.')[-1]
        if file_extension not in ['jpg', 'jpeg', 'png', 'gif']:
            return []
        return [_original_resolve_filename(sanitized_title, file_extension, post_id, owned_files)]

    if "redgifs.com" in post.url:
        return [_original_resolve_filename(sanitized_title, "mp4", post_id, owned_files)]

    if "v.redd.it" in post.url:
        return [_original_resolve_filename(sanitized_title, "mp4", post_id, owned_files)]

    return []


# Titles chosen to exercise the illegal-character set, unicode, punctuation and
# the shapes that show up in real saved posts.
TITLE_CORPUS = [
    "A Title",
    "Path/With/Slashes",
    "Back\\slashes",
    'Quotes "inside" here',
    "Question? Colon: Star* Pipe| LT< GT>",
    "Trailing dots...",
    "   leading and trailing   ",
    "emoji 🎉 title 🚀",
    "日本語のタイトル",
    "Ünïcödé àccênts",
    "[OC] Something (2023) - Part 1",
    "a" * 300,
    "100% real, no fake",
    "under_scores_and-dashes",
    "Multiple    interior    spaces",
    "tab\tinside",
    "CON",
    ".hidden",
    "_1",
    "already_1.jpg",
]


@pytest.mark.parametrize("title", TITLE_CORPUS)
@pytest.mark.parametrize("owned", [
    frozenset(),
    frozenset(["A Title.jpg", "Gallery_1.jpg", "Clip.mp4", "Video.mp4"]),
])
def test_golden_matches_original_implementation(title, owned):
    """New naming must equal old naming for every post kind, with max_len=None."""
    for post in (image_post(title=title), gallery_post(title=title),
                 redgifs_post(title=title), video_post(title=title)):
        desc = describe_submission(post)
        assert desc is not None, "describe_submission dropped %r" % post.url
        new = plan_filenames(desc, set(owned), max_len=None)
        old = _original_filenames(post, set(owned))
        assert new == old, "%s / %r: %r != %r" % (desc["kind"], title, new, old)


def test_gallery_collision_keeps_index_last():
    """The viewer groups galleries on a trailing _<n>, so the post ID has to go
    before the index, not after it."""
    post = gallery_post(post_id="gal999", title="Dup", exts=("jpg", "jpg", "jpg"))
    names = plan_filenames(describe_submission(post), {"Dup_1.jpg"}, max_len=None)
    assert names == ["Dup_gal999_1.jpg", "Dup_gal999_2.jpg", "Dup_gal999_3.jpg"]
    assert re.match(r"^(.+)_(\d+)(\.[^.]+)$", names[0])


def test_gallery_skips_removed_image():
    """A dangling media_id used to raise KeyError and abandon the whole post."""
    post = gallery_post(post_id="gal1", title="Partly", exts=("jpg", "png"))
    post.gallery_data["items"].append({"media_id": "missing"})
    desc = describe_submission(post)
    assert desc["count"] == 2
    assert plan_filenames(desc, set(), max_len=None) == ["Partly_1.jpg", "Partly_2.png"]


def test_unsupported_kinds_are_dropped():
    assert describe_submission(FakePost(id="x", title="t", url="https://example.com/page")) is None
    assert describe_submission(FakePost(id="x", title="t", url="https://i.redd.it/x.tiff")) is None
    # A v.redd.it post with no reddit_video metadata has nothing to download.
    assert describe_submission(FakePost(id="x", title="t", url="https://v.redd.it/x")) is None
    # Saved comments have no title/url at all.
    assert describe_submission(FakePost(id="x")) is None


# --- sanitize_title ---------------------------------------------------------

def test_max_len_none_is_the_original_transformation():
    for title in TITLE_CORPUS:
        assert sanitize_title(title) == re.sub(r'[\\/*?:"<>|]', "", title)


@pytest.mark.parametrize("title,expected", [
    ("Trailing dots...", "Trailing dots"),
    ("   padded   ", "padded"),
    (".hidden", "hidden"),
    ("..", ""),
    (".", ""),
    ("", ""),
    ("///", ""),
    ("CON", "_CON"),
    ("nul", "_nul"),
    ("Multiple    spaces", "Multiple spaces"),
    ("tab\tinside", "tab inside"),
])
def test_sanitize_title_edge_cases(title, expected):
    assert sanitize_title(title, max_len=120) == expected


def test_sanitize_title_strips_control_and_bidi():
    assert sanitize_title("a\x01b‮c‏d", max_len=120) == "abcd"
    assert "\x00" not in sanitize_title("a\x00b", max_len=120)


def test_sanitize_title_truncates_on_byte_budget():
    stem = sanitize_title("x" * 400, max_len=120)
    assert len(stem.encode("utf-8")) == 120

    # A 3-byte-per-codepoint title must not be cut mid-codepoint.
    cjk = sanitize_title("日" * 200, max_len=120)
    assert len(cjk.encode("utf-8")) <= 120
    assert cjk == "日" * 40  # decodes cleanly

    # 4-byte emoji, where the budget does not divide evenly.
    emoji = sanitize_title("🎉" * 100, max_len=120)
    assert len(emoji.encode("utf-8")) <= 120
    assert emoji.encode("utf-8").decode("utf-8") == emoji


def test_truncation_cannot_leave_a_trailing_dot():
    assert not sanitize_title("y" * 119 + "..." + "z" * 50, max_len=120).endswith(".")


def test_redgifs_creator_naming_is_date_prefixed():
    desc = {"source": "redgifs", "id": "amusedcrimsonhorse", "kind": "video",
            "ext": "mp4", "created_utc": 1705276800}
    assert plan_filenames(desc, set()) == ["20240115_amusedcrimsonhorse.mp4"]


def test_redgifs_creator_naming_without_a_date():
    desc = {"source": "redgifs", "id": "somegif", "kind": "video",
            "ext": "mp4", "created_utc": None}
    assert plan_filenames(desc, set()) == ["somegif.mp4"]


def test_redgifs_name_cannot_look_like_a_gallery():
    """The viewer groups `foo_1.mp4` + `foo_2.mp4` into a gallery; a date_id name
    must never match that pattern."""
    desc = {"source": "redgifs", "id": "wearyscarletmoth", "kind": "video",
            "ext": "mp4", "created_utc": 1705276800}
    name = plan_filenames(desc, set())[0]
    assert not re.match(r"^(.+)_(\d+)(\.[^.]+)$", name)


def test_long_reddit_title_stays_within_the_filesystem_limit():
    post = image_post(title="ünïcödé " * 60)
    names = plan_filenames(describe_submission(post), set(), max_len=120)
    assert len(names[0].encode("utf-8")) <= 255


def test_empty_title_falls_back_to_the_post_id():
    post = image_post(post_id="fallback1", title="///")
    assert plan_filenames(describe_submission(post), set(), max_len=120) == ["fallback1.jpg"]


# --- collision handling -----------------------------------------------------

def test_resolve_filename_suffixes_on_collision():
    assert resolve_filename("Foo", "jpg", "abc", set()) == "Foo.jpg"
    assert resolve_filename("Foo", "jpg", "abc", {"Foo.jpg"}) == "Foo_abc.jpg"
    assert resolve_filename("Foo", "jpg", "abc", {"Bar.jpg"}) == "Foo.jpg"


def test_collisions_are_case_insensitive():
    """APFS and SMB are case-insensitive, so Foo.jpg and foo.jpg are one file."""
    assert resolve_filename("foo", "jpg", "abc", {"FOO.jpg"}) == "foo_abc.jpg"
    assert resolve_filename("Foo", "jpg", "abc", {"foo.jpg"}) == "Foo_abc.jpg"
    assert name_taken({"FOO.JPG"}, "foo.jpg")
    assert not name_taken({"bar.jpg"}, "foo.jpg")


def test_different_extensions_do_not_collide():
    assert resolve_filename("Foo", "mp4", "abc", {"Foo.jpg"}) == "Foo.mp4"
