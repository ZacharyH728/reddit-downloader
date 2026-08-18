"""X/Twitter: message-stream grouping, describe_tweet, and filename planning.

These tests never touch the network. gallery-dl's contract with us is a stream of
(Message.Directory, "", tweet_meta) and (Message.Url, url, file_meta) tuples, so
the fixtures below synthesize exactly that stream. If a gallery-dl upgrade changes
the shape, test_grouping_* is what should fail.
"""
import datetime

import pytest

from core import twitter
from core.reddit_api import describe_tweet, plan_filenames

# Mirrors gallery_dl.extractor.message.Message so the tests don't require
# gallery-dl to be installed to run.
DIRECTORY = 2
URL = 3

TWEET_ID = 1789012345678901234
# 2024-01-02 03:04:05 UTC
WHEN = datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
AUTHOR = {"name": "SomeCreator", "nick": "Some Creator", "media_count": 42,
          "followers_count": 1000, "verified": True, "protected": False,
          "profile_image": "https://pbs.twimg.com/profile_images/1/a.jpg"}


def photo(index, media_id="AAA"):
    return {
        "url": "https://pbs.twimg.com/media/%s?format=jpg&name=orig" % media_id,
        "extension": "jpg", "type": "photo", "width": 1200, "height": 800,
        "num": index,
    }


def video(index=1):
    return {
        "url": "https://video.twimg.com/ext_tw_video/1/pu/vid/1280x720/x.mp4",
        "extension": "mp4", "type": "video", "width": 1280, "height": 720,
        "duration": 30.5, "num": index,
    }


def stream(*tweets):
    """Build a gallery-dl style message stream from (meta, files) pairs."""
    out = []
    for meta, files in tweets:
        out.append((DIRECTORY, "", meta))
        for f in files:
            out.append((URL, f["url"], f))
    return out


def tweet_meta(tweet_id=TWEET_ID, content="Hello world", sensitive=False, date=WHEN):
    return {"tweet_id": tweet_id, "content": content, "sensitive": sensitive,
            "date": date, "author": AUTHOR, "user": AUTHOR}


@pytest.fixture(autouse=True)
def _message_constants(monkeypatch):
    """Let _iter_tweets run without gallery-dl installed."""
    class FakeMessage:
        Directory = DIRECTORY
        Url = URL
    monkeypatch.setattr(twitter, "Message", FakeMessage)


# --- grouping ---------------------------------------------------------------

def test_grouping_collects_one_tweet_per_directory_message():
    messages = stream(
        (tweet_meta(1, "first"), [photo(1, "A")]),
        (tweet_meta(2, "second"), [photo(1, "B"), photo(2, "C")]),
    )
    tweets = list(twitter._iter_tweets(iter(messages)))
    assert [t["id"] for t in tweets] == ["1", "2"]
    assert [len(t["files"]) for t in tweets] == [1, 2]
    assert tweets[0]["text"] == "first"


def test_grouping_yields_the_final_tweet():
    """The last tweet has no following Directory to flush it."""
    tweets = list(twitter._iter_tweets(iter(stream((tweet_meta(), [photo(1)])))))
    assert len(tweets) == 1


def test_grouping_skips_a_tweet_whose_files_were_all_filtered_out():
    messages = stream((tweet_meta(1), []), (tweet_meta(2), [photo(1)]))
    assert [t["id"] for t in twitter._iter_tweets(iter(messages))] == ["2"]


def test_grouping_is_not_confused_by_the_extractor_mutating_its_metadata():
    """gallery-dl reuses and mutates the tweet dict while emitting Url messages.

    _iter_tweets copies it; without that copy the popped keys would be missing by
    the time the tweet is assembled.
    """
    meta = tweet_meta(1, "original")

    def messages():
        # The mutation has to happen mid-iteration, exactly where the real
        # extractor does it: after the Directory message, before the tweet is
        # assembled. Mutating up front would be absorbed by the generator being
        # lazy and would pass even without the copy.
        yield DIRECTORY, "", meta
        meta["content"] = "mutated"
        meta.pop("author", None)
        yield URL, photo(1)["url"], photo(1)

    tweets = list(twitter._iter_tweets(messages()))
    assert tweets[0]["text"] == "original"
    assert tweets[0]["handle"] == "somecreator"


def test_grouping_respects_max_tweets():
    messages = stream(*[(tweet_meta(i), [photo(1)]) for i in range(1, 6)])
    assert len(list(twitter._iter_tweets(iter(messages), max_tweets=2))) == 2


def test_grouping_stops_when_asked_to():
    messages = stream(*[(tweet_meta(i), [photo(1)]) for i in range(1, 6)])
    stop = {"now": False}
    out = []
    for tweet in twitter._iter_tweets(iter(messages), should_stop=lambda: stop["now"]):
        out.append(tweet)
        stop["now"] = True
    assert len(out) == 1


def test_grouping_ignores_a_url_with_no_preceding_directory():
    messages = [(URL, photo(1)["url"], photo(1))]
    assert list(twitter._iter_tweets(iter(messages))) == []


# --- describe_tweet ---------------------------------------------------------

def one(meta=None, files=None):
    messages = stream((meta or tweet_meta(), files or [photo(1)]))
    return list(twitter._iter_tweets(iter(messages)))[0]


def test_a_single_photo_is_an_image_not_a_one_item_gallery():
    """A `_1` suffix on a lone file would be noise, and would read as a gallery."""
    desc = describe_tweet(one())
    assert desc["kind"] == "image"
    assert desc["count"] == 1
    assert plan_filenames(desc, set(), max_len=120) == \
        ["20240102_t%d_Hello world.jpg" % TWEET_ID]


def test_a_multi_photo_tweet_becomes_a_gallery():
    desc = describe_tweet(one(files=[photo(1, "A"), photo(2, "B"), photo(3, "C")]))
    assert desc["kind"] == "gallery"
    assert desc["count"] == 3
    names = plan_filenames(desc, set(), max_len=120)
    # The _1/_2/_3 convention the sibling gallery app parses.
    assert [n.rsplit("_", 1)[-1] for n in names] == ["1.jpg", "2.jpg", "3.jpg"]


def test_a_video_tweet_is_a_video_with_a_directly_playable_preview():
    desc = describe_tweet(one(files=[video()]))
    assert desc["kind"] == "video"
    assert desc["preview_type"] == "video"
    # No lazy resolve step: the CDN URL is already usable, unlike RedGifs.
    assert desc["preview"] == desc["video_url"]
    assert desc["duration"] == 30.5


def test_an_animated_gif_is_a_video_marked_as_having_no_audio():
    gif = dict(video(), type="animated_gif")
    desc = describe_tweet(one(files=[gif]))
    assert desc["kind"] == "video"
    assert desc["has_audio"] is False


def test_sensitive_tweets_are_marked_nsfw():
    assert describe_tweet(one(tweet_meta(sensitive=True)))["nsfw"] is True
    assert describe_tweet(one(tweet_meta(sensitive=False)))["nsfw"] is False


def test_permalink_points_at_the_real_tweet():
    assert describe_tweet(one())["permalink"] == \
        "https://x.com/somecreator/status/%d" % TWEET_ID


def test_a_tweet_with_no_files_is_not_describable():
    assert describe_tweet({"id": "1", "files": []}) is None
    assert describe_tweet(None) is None


def test_thumbnails_ask_twimg_for_a_smaller_variant():
    desc = describe_tweet(one())
    assert desc["thumb"].endswith("name=small")
    assert desc["preview"].endswith("name=medium")


def test_video_urls_are_never_rewritten():
    """Only pbs.twimg.com resizes by query parameter; editing a video URL breaks it."""
    desc = describe_tweet(one(files=[video()]))
    assert desc["preview"] == video()["url"]


# --- filenames --------------------------------------------------------------

def test_the_tweet_id_is_prefixed_so_it_cannot_look_like_a_gallery_index():
    """`<date>_<numeric id>.jpg` would end in `_<digits>`, which the sibling
    gallery app reads as page <n> of a gallery."""
    desc = describe_tweet(one(tweet_meta(content="")))
    name = plan_filenames(desc, set(), max_len=120)[0]
    assert name == "20240102_t%d.jpg" % TWEET_ID
    assert not name.rsplit("_", 1)[-1][0].isdigit()


def test_an_empty_tweet_still_gets_a_stable_name():
    for text in ("", "   ", None):
        desc = describe_tweet(one(tweet_meta(content=text)))
        assert plan_filenames(desc, set(), max_len=120) == \
            ["20240102_t%d.jpg" % TWEET_ID]


def test_a_tweet_with_no_date_falls_back_to_the_bare_id():
    desc = describe_tweet(one(tweet_meta(content="", date=None)))
    assert plan_filenames(desc, set(), max_len=120) == ["t%d.jpg" % TWEET_ID]


def test_long_tweet_text_is_truncated_within_the_byte_budget():
    desc = describe_tweet(one(tweet_meta(content="x" * 500)))
    name = plan_filenames(desc, set(), max_len=120)[0]
    assert len(name.encode("utf-8")) <= 120 + len(".jpg")


def test_two_tweets_with_identical_text_do_not_collide():
    first = describe_tweet(one(tweet_meta(tweet_id=111, content="same")))
    second = describe_tweet(one(tweet_meta(tweet_id=222, content="same")))
    owned = set(plan_filenames(first, set(), max_len=120))
    assert plan_filenames(second, owned, max_len=120)[0] not in owned


# --- availability -----------------------------------------------------------

def test_unavailable_without_a_token(monkeypatch):
    monkeypatch.setattr(twitter.config, "TWITTER_AUTH_TOKEN", None)
    assert twitter.available() is False
    assert "TWITTER_AUTH_TOKEN" in twitter.unavailable_reason()
    with pytest.raises(twitter.TwitterUnavailable):
        twitter._require()


def test_available_with_a_token(monkeypatch):
    monkeypatch.setattr(twitter.config, "TWITTER_AUTH_TOKEN", "deadbeef")
    if twitter.gallery_dl is None:
        pytest.skip("gallery-dl not installed")
    assert twitter.available() is True
    assert twitter.unavailable_reason() is None


@pytest.mark.parametrize("exc,expected", [
    (RuntimeError("401 Unauthorized"), True),
    (RuntimeError("AuthRequired: cookies needed"), True),
    (RuntimeError("Please login to access this"), True),
    (RuntimeError("connection reset"), False),
    (RuntimeError("404 Not Found"), False),
])
def test_auth_errors_are_recognised_without_gallery_dl(exc, expected, monkeypatch):
    """The string fallback, used only when gallery-dl isn't importable."""
    monkeypatch.setattr(twitter, "gdl_exception", None)
    assert twitter._is_auth_error(exc) is expected


def _http_error(status, url="https://x.com/i/api/graphql/x"):
    """Build a real gallery_dl HttpError, so the isinstance checks are exercised."""
    gdl_exception = pytest.importorskip("gallery_dl.exception")

    class FakeResponse:
        def __init__(self):
            self.status_code = status
            self.reason = "Error"
            self.url = url
    return gdl_exception.HttpError(response=FakeResponse())


def test_a_401_is_an_auth_error():
    assert twitter._is_auth_error(_http_error(401)) is True
    assert twitter._is_auth_error(_http_error(403)) is True


def test_the_transaction_id_404_is_classified_as_an_auth_error():
    """The real symptom of a bad cookie.

    X builds a per-session transaction id from a JS bundle before it will answer
    a GraphQL call, and with a rejected session that chain 404s on an
    `ondemand.s.*.js` URL. Read literally that is "not found", which would tell
    the user their handle was wrong instead of that their cookie expired.
    """
    exc = _http_error(
        404, "https://abs.twimg.com/responsive-web/client-web/ondemand.s.Nonea.js")
    assert twitter._is_auth_error(exc) is True
    assert twitter._is_missing(exc) is False
    with pytest.raises(twitter.TwitterAuthError):
        twitter._translate(exc, "@someone")


def test_an_ordinary_404_is_a_missing_creator():
    exc = _http_error(404, "https://x.com/i/api/graphql/UserByScreenName")
    assert twitter._is_auth_error(exc) is False
    assert twitter._is_missing(exc) is True
    with pytest.raises(twitter.TwitterNotFound):
        twitter._translate(exc, "@someone")


def test_a_server_error_is_neither_auth_nor_missing():
    exc = _http_error(503)
    assert twitter._is_auth_error(exc) is False
    assert twitter._is_missing(exc) is False
    with pytest.raises(twitter.TwitterUpstreamError):
        twitter._translate(exc, "@someone")


def test_gallery_dl_auth_exceptions_are_recognised():
    gdl_exception = pytest.importorskip("gallery_dl.exception")
    for cls in (gdl_exception.AuthRequired, gdl_exception.AuthenticationError,
                gdl_exception.AuthorizationError):
        assert twitter._is_auth_error(cls("nope")) is True


def test_profile_normalisation():
    profile = twitter._profile_from({"user": AUTHOR}, "SomeCreator")
    assert profile["platform"] == "twitter"
    assert profile["name"] == "somecreator"
    assert profile["display"] == "Some Creator"
    # media_count, not statuses_count: the media tab is what we download.
    assert profile["count"] == 42
    assert profile["url"] == "https://x.com/somecreator"
