"""Job manager tests.

Both APIs are stubbed at the `creators` seam, but everything below it is real:
real HTTP downloads off a local server, real filename planning, real manifests.
"""
import os
import time

import pytest

from core import config, creators, jobs
from core.jobs import JobError, JobManager
from core.manifest import Manifest
from tests.test_download import GOOD_BODY, Handler, QuietServer

import threading


@pytest.fixture(scope="module")
def server():
    httpd = QuietServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:%d" % httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def make_desc(item_id, url, title=None):
    return {
        "source": "reddit",
        "id": item_id,
        "kind": "image",
        "title": title or ("Post %s" % item_id),
        "ext": "jpg",
        "url": url,
        "created_utc": 1700000000.0,
        "nsfw": False,
        "count": 1,
    }


@pytest.fixture
def env(tmp_path, monkeypatch, server):
    """Point the creator root at tmp_path and stub out both platform APIs."""
    monkeypatch.setattr(config, "CREATOR_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "DOWNLOAD_LOCATION", str(tmp_path))
    monkeypatch.setattr(config, "MIN_FREE_DISK_MB", 0)

    # The real background session retries 5xx five times with backoff (~31s),
    # which is right for an unattended daemon and pointless in a unit test.
    import requests.adapters
    from core import net as net_module

    def fast_session(pool=10):
        session = requests.Session()
        session.trust_env = False
        session.mount("http://", requests.adapters.HTTPAdapter(max_retries=0))
        return session

    monkeypatch.setattr(net_module, "make_session", fast_session)

    state = {"descs": [], "missing": [], "total": None, "enumerated": 0}

    def fake_resolve(platform, name, item_ids):
        by_id = {d["id"]: d for d in state["descs"]}
        found = [by_id[i] for i in item_ids if i in by_id]
        missing = [i for i in item_ids if i not in by_id]
        return found, missing

    def fake_iter(platform, name, page_size=100, should_stop=None, kind="all"):
        should_stop = should_stop or (lambda: False)
        for desc in state["descs"]:
            if should_stop():
                return
            # Mirrors the real iter_all_items, which filters by kind while
            # paging — so a test can assert what a filtered job enumerated.
            if not creators.kind_matches(kind, desc["kind"]):
                continue
            state["enumerated"] += 1
            yield desc

    monkeypatch.setattr(creators, "resolve_selected", fake_resolve)
    monkeypatch.setattr(creators, "iter_all_items", fake_iter)
    monkeypatch.setattr(creators, "total_estimate", lambda p, n: state["total"])
    return state


@pytest.fixture
def manager():
    m = JobManager(concurrency=3, history=5)
    m.start()
    yield m
    m.stop()


def wait_for(manager, job_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = manager.get(job_id)
        if job and job["state"] in jobs.TERMINAL:
            return job
        time.sleep(0.02)
    raise AssertionError("job %s did not finish: %r" % (job_id, manager.get(job_id)))


# --- happy paths ------------------------------------------------------------

def test_selected_download_writes_files_and_manifest(env, manager, server, tmp_path):
    env["descs"] = [make_desc("a1", server + "/good.jpg"),
                    make_desc("a2", server + "/good.jpg"),
                    make_desc("a3", server + "/good.jpg")]
    submitted = manager.submit("reddit", "someone", "selected", ["a1", "a2", "a3"])
    job = wait_for(manager, submitted["id"])

    assert job["state"] == jobs.DONE
    assert (job["downloaded"], job["skipped"], job["failed"], job["gone"]) == (3, 0, 0, 0)

    directory = tmp_path / "reddit" / "someone"
    files = sorted(p.name for p in directory.iterdir())
    assert files == [".download_manifest.json", "Post a1.jpg", "Post a2.jpg", "Post a3.jpg"]
    assert (directory / "Post a1.jpg").stat().st_size == len(GOOD_BODY)

    manifest = Manifest(str(directory))
    assert manifest.has_post("a1") and manifest.has_post("a3")


def test_rerunning_the_same_job_skips_everything(env, manager, server, tmp_path):
    env["descs"] = [make_desc("b1", server + "/good.jpg")]
    wait_for(manager, manager.submit("reddit", "someone", "selected", ["b1"])["id"])

    job = wait_for(manager, manager.submit("reddit", "someone", "selected", ["b1"])["id"])
    assert (job["downloaded"], job["skipped"]) == (0, 1)


def test_deleting_a_file_makes_only_that_one_redownload(env, manager, server, tmp_path):
    env["descs"] = [make_desc("c1", server + "/good.jpg"),
                    make_desc("c2", server + "/good.jpg")]
    wait_for(manager, manager.submit("reddit", "someone", "selected", ["c1", "c2"])["id"])

    directory = tmp_path / "reddit" / "someone"
    (directory / "Post c1.jpg").unlink()
    # Drop c1 from the manifest the way a re-verify would.
    manifest = Manifest(str(directory))
    manifest.forget("c1")
    manifest.flush(force=True)

    job = wait_for(manager, manager.submit("reddit", "someone", "selected", ["c1", "c2"])["id"])
    assert (job["downloaded"], job["skipped"]) == (1, 1)
    assert (directory / "Post c1.jpg").exists()


def test_download_all_enumerates_everything(env, manager, server, tmp_path):
    env["descs"] = [make_desc("d%d" % i, server + "/good.jpg") for i in range(7)]
    job = wait_for(manager, manager.submit("reddit", "someone", "all")["id"])
    assert job["state"] == jobs.DONE
    assert job["downloaded"] == 7
    assert job["total"] == 7
    assert len(list((tmp_path / "reddit" / "someone").glob("*.jpg"))) == 7


def test_same_title_posts_get_distinct_filenames(env, manager, server, tmp_path):
    """Two items with one title must not race onto the same name."""
    env["descs"] = [make_desc("e1", server + "/good.jpg", title="Same Title"),
                    make_desc("e2", server + "/good.jpg", title="Same Title"),
                    make_desc("e3", server + "/good.jpg", title="Same Title")]
    job = wait_for(manager, manager.submit("reddit", "someone", "all")["id"])
    assert job["downloaded"] == 3

    names = sorted(p.name for p in (tmp_path / "reddit" / "someone").glob("*.jpg"))
    assert names == ["Same Title.jpg", "Same Title_e2.jpg", "Same Title_e3.jpg"]
    assert len(set(names)) == 3


# --- filters on a "download everything" job ---------------------------------
#
# The button takes what the grid is showing, so a job carries the same kind/only
# filters the listing was using. Getting this wrong is quiet and expensive: the
# user filters to images and the button fetches a creator's entire video back
# catalogue.

def make_video_desc(item_id, url, title=None):
    """A kind="video" desc that still downloads via the plain file path.

    `source` is deliberately not "reddit", so this skips the v.redd.it DASH
    muxer, and the body is the same jpg fixture as everything else: these tests
    are about which items get *selected*, and a real mp4 fixture would only make
    a filter regression fail with a confusing download error instead of a clean
    count mismatch.
    """
    desc = make_desc(item_id, url, title=title)
    desc.update({"source": "twitter", "kind": "video", "video_url": url})
    return desc


def test_download_all_honors_the_kind_filter(env, manager, server, tmp_path):
    env["descs"] = [make_desc("y1", server + "/good.jpg"),
                    make_video_desc("y2", server + "/good.jpg"),
                    make_desc("y3", server + "/good.jpg"),
                    make_video_desc("y4", server + "/good.jpg")]
    job = wait_for(manager, manager.submit(
        "reddit", "someone", "all", kind="image")["id"])

    assert job["state"] == jobs.DONE
    assert job["kind"] == "image"
    # total counts the filtered set, so the progress bar measures real work.
    assert (job["downloaded"], job["total"]) == (2, 2)

    manifest = Manifest(str(tmp_path / "reddit" / "someone"))
    assert manifest.has_post("y1") and manifest.has_post("y3")
    # Filtered-out items must be left completely alone - not downloaded, and not
    # recorded either, or a later unfiltered run would skip them as "have".
    assert not manifest.has_post("y2")
    assert not manifest.has_post("y4")


def test_download_all_kind_video_leaves_images(env, manager, server):
    env["descs"] = [make_desc("z1", server + "/good.jpg"),
                    make_video_desc("z2", server + "/good.jpg")]
    job = wait_for(manager, manager.submit(
        "reddit", "someone", "all", kind="video")["id"])
    assert (job["downloaded"], job["total"]) == (1, 1)


def test_video_filter_covers_the_redgifs_kind():
    """The grid's "Videos" toggle shows both kinds, so a job must take both."""
    assert creators.kind_matches("video", "video")
    assert creators.kind_matches("video", "redgifs")
    assert not creators.kind_matches("image", "redgifs")
    assert creators.kind_matches("all", "gallery")
    # An unrecognised filter must not silently select nothing.
    assert creators.kind_matches("bogus", "image")


def test_download_all_only_missing_ignores_what_we_have(env, manager, server):
    env["descs"] = [make_desc("w%d" % i, server + "/good.jpg") for i in range(4)]
    assert wait_for(manager, manager.submit(
        "reddit", "someone", "all")["id"])["downloaded"] == 4

    env["descs"].append(make_desc("w9", server + "/good.jpg"))
    job = wait_for(manager, manager.submit(
        "reddit", "someone", "all", only="missing")["id"])
    assert job["only"] == "missing"
    # The four we already have are filtered out before `total`, rather than
    # counted as four instant skips.
    assert (job["downloaded"], job["skipped"], job["total"]) == (1, 0, 1)

    # Unfiltered, the same listing reports them as skipped instead - same files
    # on disk either way, different accounting.
    job = wait_for(manager, manager.submit("reddit", "someone", "all")["id"])
    assert (job["downloaded"], job["skipped"], job["total"]) == (0, 5, 5)


def test_download_rejects_unknown_filters(env, manager):
    with pytest.raises(JobError) as exc:
        manager.submit("reddit", "someone", "all", kind="sideways")
    assert exc.value.code == "invalid_kind"

    with pytest.raises(JobError) as exc:
        manager.submit("reddit", "someone", "all", only="sideways")
    assert exc.value.code == "invalid_only"

    # "have" is a real /items filter, but a download job that only takes what is
    # already on disk provably transfers nothing, so it is refused rather than
    # accepted and silently turned into a no-op.
    with pytest.raises(JobError) as exc:
        manager.submit("reddit", "someone", "all", only="have")
    assert exc.value.code == "invalid_only"


def test_unfiltered_job_reports_all_as_its_scope(env, manager, server):
    """The drawer reads these to label a job, so the default must stay "all"."""
    env["descs"] = [make_desc("v1", server + "/good.jpg")]
    job = wait_for(manager, manager.submit("reddit", "someone", "all")["id"])
    assert (job["kind"], job["only"]) == ("all", "all")


# --- failure handling -------------------------------------------------------

def test_failures_are_counted_and_retryable(env, manager, server, tmp_path):
    env["descs"] = [make_desc("f1", server + "/good.jpg"),
                    make_desc("f2", server + "/boom.jpg"),
                    make_desc("f3", server + "/truncated.jpg")]
    job = wait_for(manager, manager.submit("reddit", "someone", "all")["id"])

    assert job["state"] == jobs.DONE_WITH_ERRORS
    assert job["downloaded"] == 1
    assert job["failed"] == 2
    assert sorted(job["failed_ids"]) == ["f2", "f3"]
    assert len(job["errors"]) == 2

    # A failed item must NOT be recorded, or it would never be retried.
    manifest = Manifest(str(tmp_path / "reddit" / "someone"))
    assert manifest.has_post("f1")
    assert not manifest.has_post("f2")
    # And no truncated file was published.
    assert not (tmp_path / "reddit" / "someone" / "Post f3.jpg").exists()


def test_gone_items_are_recorded_so_they_are_not_retried(env, manager, server, tmp_path):
    env["descs"] = [make_desc("g1", server + "/gone.jpg")]
    job = wait_for(manager, manager.submit("reddit", "someone", "all")["id"])
    assert job["gone"] == 1
    assert job["state"] == jobs.DONE_WITH_ERRORS
    assert Manifest(str(tmp_path / "reddit" / "someone")).is_gone("g1")


def test_unresolvable_selected_ids_count_as_gone(env, manager, server):
    env["descs"] = [make_desc("h1", server + "/good.jpg")]
    job = wait_for(manager, manager.submit(
        "reddit", "someone", "selected", ["h1", "deleted1"])["id"])
    assert job["downloaded"] == 1
    assert job["gone"] == 1


def test_retry_failed_starts_a_job_with_only_the_failures(env, manager, server, tmp_path):
    env["descs"] = [make_desc("i1", server + "/good.jpg"),
                    make_desc("i2", server + "/boom.jpg")]
    first = wait_for(manager, manager.submit("reddit", "someone", "all")["id"])
    assert first["failed_ids"] == ["i2"]

    # Point the failing id at a working URL, as a transient failure would clear.
    env["descs"] = [make_desc("i1", server + "/good.jpg"),
                    make_desc("i2", server + "/good.jpg")]
    retry = manager.retry_failed(first["id"])
    assert retry["mode"] == "selected"
    done = wait_for(manager, retry["id"])
    assert done["downloaded"] == 1
    assert done["state"] == jobs.DONE


def test_retry_failed_rejects_a_clean_job(env, manager, server):
    env["descs"] = [make_desc("j1", server + "/good.jpg")]
    job = wait_for(manager, manager.submit("reddit", "someone", "all")["id"])
    with pytest.raises(JobError) as exc:
        manager.retry_failed(job["id"])
    assert exc.value.code == "nothing_to_retry"


# --- submission validation --------------------------------------------------

def test_duplicate_creator_job_is_rejected(env, manager, server):
    env["descs"] = [make_desc("k%d" % i, server + "/slow.jpg") for i in range(20)]
    first = manager.submit("reddit", "someone", "all")
    try:
        with pytest.raises(JobError) as exc:
            manager.submit("reddit", "someone", "all")
        assert exc.value.code == "job_exists"
        assert exc.value.status == 409
    finally:
        manager.cancel(first["id"])


def test_a_different_creator_may_queue_concurrently(env, manager, server):
    env["descs"] = [make_desc("l%d" % i, server + "/slow.jpg") for i in range(20)]
    first = manager.submit("reddit", "someone", "all")
    second = manager.submit("reddit", "another", "all")
    try:
        assert second["state"] == jobs.QUEUED
    finally:
        manager.cancel(first["id"])
        manager.cancel(second["id"])


def test_selected_requires_ids(env, manager):
    with pytest.raises(JobError) as exc:
        manager.submit("reddit", "someone", "selected", [])
    assert exc.value.code == "no_items"


def test_invalid_mode_is_rejected(env, manager):
    with pytest.raises(JobError) as exc:
        manager.submit("reddit", "someone", "sideways")
    assert exc.value.code == "invalid_mode"


def test_invalid_creator_is_rejected_before_any_work(env, manager):
    from core.validate import ValidationError
    with pytest.raises(ValidationError):
        manager.submit("reddit", "../escape", "all")


def test_duplicate_ids_are_deduped(env, manager, server, tmp_path):
    env["descs"] = [make_desc("m1", server + "/good.jpg")]
    job = wait_for(manager, manager.submit(
        "reddit", "someone", "selected", ["m1", "m1", "m1"])["id"])
    assert job["total"] == 1
    assert job["downloaded"] == 1


# --- cancellation -----------------------------------------------------------

def test_cancel_while_queued_finishes_immediately(env, manager, server):
    env["descs"] = [make_desc("n%d" % i, server + "/slow.jpg") for i in range(30)]
    first = manager.submit("reddit", "someone", "all")
    queued = manager.submit("reddit", "another", "all")
    try:
        cancelled = manager.cancel(queued["id"])
        assert cancelled["state"] == jobs.CANCELLED
    finally:
        manager.cancel(first["id"])


def test_cancel_stops_a_running_job_and_leaves_no_partials(env, manager, server, tmp_path):
    env["descs"] = [make_desc("o%d" % i, server + "/slow.jpg") for i in range(40)]
    submitted = manager.submit("reddit", "someone", "all")

    # Wait until it is actually downloading, then cancel.
    deadline = time.time() + 10
    while time.time() < deadline:
        if (manager.get(submitted["id"]) or {}).get("phase") == "downloading":
            break
        time.sleep(0.02)
    manager.cancel(submitted["id"])
    job = wait_for(manager, submitted["id"], timeout=30)

    assert job["state"] == jobs.CANCELLED
    directory = tmp_path / "reddit" / "someone"
    if directory.exists():
        leftovers = [p.name for p in directory.iterdir() if p.name.endswith(".part")]
        assert leftovers == [], "cancellation left partial files: %r" % leftovers


def test_cancel_is_idempotent_and_404s_unknown_jobs(env, manager):
    with pytest.raises(JobError) as exc:
        manager.cancel("nosuchjob")
    assert exc.value.status == 404


# --- bookkeeping ------------------------------------------------------------

def test_history_is_bounded(env, manager, server):
    env["descs"] = [make_desc("p1", server + "/good.jpg")]
    ids = []
    for i in range(8):
        submitted = manager.submit("reddit", "creator%d" % i, "selected", ["p1"])
        ids.append(submitted["id"])
        wait_for(manager, submitted["id"])
    # history=5 for this manager, so the oldest finished jobs are dropped.
    assert len(manager.list(limit=100)) <= 6
    assert manager.get(ids[-1]) is not None


def test_list_puts_active_jobs_first(env, manager, server):
    env["descs"] = [make_desc("q%d" % i, server + "/slow.jpg") for i in range(30)]
    done = manager.submit("reddit", "finished", "selected", ["q0"])
    wait_for(manager, done["id"])
    running = manager.submit("reddit", "busy", "all")
    try:
        listed = manager.list(limit=10)
        assert listed[0]["id"] == running["id"]
        assert listed[0]["active"] is True
    finally:
        manager.cancel(running["id"])


def test_job_dict_shape_is_stable(env, manager, server):
    env["descs"] = [make_desc("r1", server + "/good.jpg")]
    job = wait_for(manager, manager.submit("reddit", "someone", "selected", ["r1"])["id"])
    expected = {
        "id", "platform", "creator", "mode", "state", "phase", "total", "completed",
        "downloaded", "skipped", "failed", "gone", "current", "dest", "created_at",
        "started_at", "finished_at", "error", "errors", "failed_ids", "active",
    }
    assert set(job) == expected


def test_creator_directory_is_created_under_the_platform(env, manager, server, tmp_path):
    # RedGifs IDs are long words, not short slugs.
    env["descs"] = [make_desc("amusedcrimsonhorse", server + "/good.jpg")]
    wait_for(manager, manager.submit(
        "redgifs", "somebody", "selected", ["amusedcrimsonhorse"])["id"])
    assert (tmp_path / "redgifs" / "somebody").is_dir()
    assert not (tmp_path / "somebody").exists()


def test_twitter_downloads_stream_instead_of_pre_enumerating(env, manager, server):
    """X reports no total, so the job must interleave enumeration with
    downloading rather than walking the whole timeline up front - the same
    treatment RedGifs gets, and for the same reason (throttling)."""
    env["descs"] = [make_desc("100%d" % i, server + "/good.jpg") for i in range(8)]
    env["total"] = None
    job = wait_for(manager, manager.submit("twitter", "someone", "all")["id"])
    assert job["state"] == jobs.DONE
    assert job["downloaded"] == 8
    # Nothing invented a total the platform never supplied.
    assert job["total"] is None


def test_twitter_creator_directory_is_created_under_the_platform(
        env, manager, server, tmp_path):
    env["descs"] = [make_desc("1789012345678901234", server + "/good.jpg")]
    wait_for(manager, manager.submit(
        "twitter", "SomeOne", "selected", ["1789012345678901234"])["id"])
    assert (tmp_path / "twitter" / "someone").is_dir()


@pytest.mark.parametrize("item_id", [
    "not-a-tweet-id",     # tweet IDs are numeric only
    "abc123",             # a Reddit-shaped ID
    "12",                 # too short
    "1" * 30,             # too long
])
def test_non_tweet_item_ids_are_rejected(manager, item_id):
    """The ID patterns are per-platform, and this one reaches the filesystem."""
    from core.validate import ValidationError
    with pytest.raises(ValidationError):
        manager.submit("twitter", "someone", "selected", [item_id])


def test_creator_name_is_lowercased_on_disk(env, manager, server, tmp_path):
    env["descs"] = [make_desc("t1", server + "/good.jpg")]
    job = wait_for(manager, manager.submit("reddit", "MixedCase", "selected", ["t1"])["id"])
    assert job["creator"] == "mixedcase"
    assert (tmp_path / "reddit" / "mixedcase").is_dir()


def test_startup_sweep_clears_stale_parts(env, manager, server, tmp_path):
    directory = tmp_path / "reddit" / "someone"
    directory.mkdir(parents=True)
    stale = directory / "leftover.jpg.part"
    stale.write_bytes(b"x")

    env["descs"] = [make_desc("u1", server + "/good.jpg")]
    wait_for(manager, manager.submit("reddit", "someone", "selected", ["u1"])["id"])
    assert not stale.exists(), "a job start should clear its own .part orphans"


def test_insufficient_disk_is_reported(env, manager, monkeypatch):
    monkeypatch.setattr(config, "MIN_FREE_DISK_MB", 10 ** 9)
    with pytest.raises(JobError) as exc:
        manager.submit("reddit", "someone", "all")
    assert exc.value.code == "insufficient_disk"
    assert exc.value.status == 507


def test_os_error_aborts_the_whole_job(env, manager, server, monkeypatch, tmp_path):
    """Out of space should fail the job, not fail 2000 items one at a time."""
    from core import reddit_api

    calls = {"n": 0}
    real = reddit_api.download_planned

    def exploding(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(28, "No space left on device")
        return real(*args, **kwargs)

    monkeypatch.setattr(reddit_api, "download_planned", exploding)
    env["descs"] = [make_desc("v%d" % i, server + "/good.jpg") for i in range(10)]
    job = wait_for(manager, manager.submit("reddit", "someone", "all")["id"])

    assert job["state"] == jobs.FAILED
    assert "No space left" in (job["error"] or "")
    assert calls["n"] < 10, "job kept going after a filesystem error"
