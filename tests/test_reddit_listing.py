"""Tests for the Reddit listing call, against PRAW's real argument handling.

This path had no coverage and shipped broken: passing `params=None` for the first
page made every /api/creators/reddit/<name>/items request 500. The stubs used
elsewhere in the suite sit *above* this function, so nothing exercised it.

These tests use the actual praw code that raised, rather than a hand-written
imitation of it, so they fail the same way production did.
"""
import pytest
from praw.models.base import PRAWBase

from core import reddit_api


class FakeSubmission:
    def __init__(self, index):
        self.id = "post%d" % index
        self.fullname = "t3_post%d" % index


class FakeSubListing:
    """Stands in for redditor.submissions, running PRAW's real _prepare logic."""

    def __init__(self, recorder, count):
        self.recorder = recorder
        self.count = count

    def _listing(self, sort, **generator_kwargs):
        self.recorder.append({"sort": sort, "kwargs": dict(generator_kwargs)})
        # This is the exact call from praw's BaseListingMixin._prepare that blew
        # up in production when generator_kwargs contained params=None.
        PRAWBase._safely_add_arguments(
            arguments=generator_kwargs, key="params", sort=sort)
        return [FakeSubmission(i) for i in range(self.count)]

    def new(self, **kwargs):
        return self._listing("new", **kwargs)

    def top(self, **kwargs):
        return self._listing("top", **kwargs)

    def hot(self, **kwargs):
        return self._listing("hot", **kwargs)


class FakeRedditor:
    def __init__(self, submissions):
        self.submissions = submissions


class FakeReddit:
    def __init__(self, count=30):
        self.calls = []
        self._submissions = FakeSubListing(self.calls, count)

    def redditor(self, _name):
        return FakeRedditor(self._submissions)


def test_first_page_omits_params_entirely():
    """The regression: `params=None` is not the same as not passing params."""
    reddit = FakeReddit(count=5)
    items, cursor = reddit_api.list_redditor_posts(reddit, "someone", limit=30)

    assert len(items) == 5
    assert "params" not in reddit.calls[0]["kwargs"], \
        "params must be omitted on the first page, not passed as None"
    assert reddit.calls[0]["kwargs"]["limit"] == 30


def test_subsequent_page_passes_the_after_cursor():
    reddit = FakeReddit(count=5)
    reddit_api.list_redditor_posts(reddit, "someone", limit=30, after="t3_abc")
    assert reddit.calls[0]["kwargs"]["params"] == {"after": "t3_abc"}


def test_full_page_returns_a_cursor():
    reddit = FakeReddit(count=30)
    _items, cursor = reddit_api.list_redditor_posts(reddit, "someone", limit=30)
    assert cursor == "t3_post29"


def test_short_page_means_the_listing_is_exhausted():
    reddit = FakeReddit(count=7)
    _items, cursor = reddit_api.list_redditor_posts(reddit, "someone", limit=30)
    assert cursor is None


def test_empty_listing_returns_no_cursor():
    reddit = FakeReddit(count=0)
    items, cursor = reddit_api.list_redditor_posts(reddit, "someone", limit=30)
    assert items == [] and cursor is None


@pytest.mark.parametrize("sort,expected", [
    ("new", "new"), ("top", "top"), ("hot", "hot"),
    ("nonsense", "new"),  # unknown sorts fall back rather than crashing
])
def test_sort_selects_the_right_listing(sort, expected):
    reddit = FakeReddit(count=1)
    reddit_api.list_redditor_posts(reddit, "someone", limit=30, sort=sort)
    assert reddit.calls[0]["sort"] == expected


def test_paging_through_multiple_pages():
    """Walk the cursor the way iter_all_items and the UI both do."""
    reddit = FakeReddit(count=30)
    seen = []
    cursor = None
    for _ in range(3):
        items, cursor = reddit_api.list_redditor_posts(
            reddit, "someone", limit=30, after=cursor)
        seen.extend(items)
        if cursor is None:
            break
    assert len(seen) == 90
    assert "params" not in reddit.calls[0]["kwargs"]
    assert reddit.calls[1]["kwargs"]["params"] == {"after": "t3_post29"}
    assert reddit.calls[2]["kwargs"]["params"] == {"after": "t3_post29"}
