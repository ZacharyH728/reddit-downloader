"""RedGifs URL -> gif id extraction.

The original regex made the path prefix optional:

    redgifs\\.com/(?:watch/|ifr/)?([a-zA-Z0-9]+)

On a /i/<id> URL the prefix group matches nothing, so the capture group returns
"i" - the prefix itself - as the gif id. That produced a lightbox error for any
Reddit post linking to a RedGifs image, and silently failed those downloads in
the saved-posts sync too.
"""
import pytest

from core.reddit_api import describe_submission, redgifs_id_from_url
from tests.test_naming import FakePost

GOOD_ID = "disfiguredfirebrickshrew"

SHAPES = [
    "https://redgifs.com/watch/%s",
    "https://www.redgifs.com/watch/%s",
    "https://redgifs.com/ifr/%s",
    "https://www.redgifs.com/ifr/%s",
    "https://redgifs.com/i/%s",
    "https://www.redgifs.com/i/%s",
    "https://redgifs.com/v/%s",
    "https://redgifs.com/%s",
    "https://www.redgifs.com/watch/%s?foo=bar",
    "https://www.redgifs.com/watch/%s#frag",
    "https://www.redgifs.com/watch/%s/",
    "https://v3.redgifs.com/watch/%s",
    "https://REDGIFS.COM/WATCH/%s",
]


@pytest.mark.parametrize("template", SHAPES)
def test_every_url_shape_yields_the_real_id(template):
    assert redgifs_id_from_url(template % GOOD_ID).lower() == GOOD_ID


def test_the_regression_prefix_is_never_returned_as_an_id():
    """The exact failure: /i/<id> used to yield "i"."""
    for prefix in ("i", "v", "watch", "ifr", "gifs", "embed"):
        url = "https://www.redgifs.com/%s/%s" % (prefix, GOOD_ID)
        assert redgifs_id_from_url(url) == GOOD_ID, url


def test_media_urls_drop_the_extension():
    assert redgifs_id_from_url(
        "https://media.redgifs.com/%s.mp4" % GOOD_ID) == GOOD_ID


def test_non_alphanumeric_segments_are_rejected():
    """CDN variants like <id>-mobile.jpg are not gif ids and must not be guessed at."""
    assert redgifs_id_from_url("https://thumbs4.redgifs.com/abc-mobile.jpg") is None
    assert redgifs_id_from_url(
        "https://thumbs4.redgifs.com/%s-mobile.jpg" % GOOD_ID) is None


@pytest.mark.parametrize("url", [
    "https://www.redgifs.com/",
    "https://www.redgifs.com",
    "https://www.redgifs.com/i/",
    "https://www.redgifs.com/watch/",
    "",
    None,
])
def test_urls_without_an_id_return_none(url):
    assert redgifs_id_from_url(url) is None


@pytest.mark.parametrize("url", [
    "https://notredgifs.com/watch/%s" % GOOD_ID,
    "https://evil-redgifs.com/watch/%s" % GOOD_ID,
    "https://redgifs.com.evil.com/watch/%s" % GOOD_ID,
    "https://i.redd.it/%s.jpg" % GOOD_ID,
])
def test_foreign_hosts_are_rejected(url):
    assert redgifs_id_from_url(url) is None


def test_describe_submission_uses_the_real_id():
    post = FakePost(id="abc123", title="A clip",
                    url="https://www.redgifs.com/i/%s" % GOOD_ID,
                    over_18=True, permalink="/r/x/comments/abc123/",
                    created_utc=1700000000.0)
    desc = describe_submission(post)
    assert desc["kind"] == "redgifs"
    assert desc["redgifs_id"] == GOOD_ID


def test_describe_submission_drops_a_redgifs_url_with_no_id():
    post = FakePost(id="abc123", title="A clip", url="https://www.redgifs.com/",
                    over_18=True, permalink="/r/x/comments/abc123/",
                    created_utc=1700000000.0)
    assert describe_submission(post) is None


def test_extracted_ids_pass_api_validation():
    """The id goes straight into /api/redgifs/gif/<id>, which validates it."""
    from core.validate import validate_item_id
    for template in SHAPES:
        gif_id = redgifs_id_from_url(template % GOOD_ID)
        assert validate_item_id("redgifs", gif_id) == gif_id
