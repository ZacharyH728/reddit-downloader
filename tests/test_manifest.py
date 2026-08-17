"""Manifest persistence tests."""
import json

from core.config import MANIFEST_NAME
from core.manifest import Manifest


def read_raw(tmp_path):
    with open(str(tmp_path / MANIFEST_NAME)) as f:
        return json.load(f)


def test_schema_is_unchanged_for_a_posts_only_manifest(tmp_path):
    """Existing readers (and the previous version of this program) expect exactly
    {"posts": {...}} - the `gone` key must not appear unless it has content."""
    m = Manifest(str(tmp_path))
    m.record("abc", ["A.jpg"])
    m.flush(force=True)
    assert read_raw(tmp_path) == {"posts": {"abc": ["A.jpg"]}}


def test_round_trip_reloads_posts_and_owned_files(tmp_path):
    m = Manifest(str(tmp_path))
    m.record("abc", ["A.jpg"])
    m.record("def", ["B_1.jpg", "B_2.jpg"])
    m.flush(force=True)

    reloaded = Manifest(str(tmp_path))
    assert reloaded.has_post("abc")
    assert reloaded.has_post("def")
    assert not reloaded.has_post("nope")
    assert reloaded.owned == {"A.jpg", "B_1.jpg", "B_2.jpg"}
    assert reloaded.files_for("def") == ["B_1.jpg", "B_2.jpg"]


def test_reads_a_legacy_manifest_written_by_the_old_version(tmp_path):
    (tmp_path / MANIFEST_NAME).write_text(json.dumps({"posts": {"old1": ["Old.jpg"]}}))
    m = Manifest(str(tmp_path))
    assert m.has_post("old1")
    assert m.owned == {"Old.jpg"}
    assert m.gone == {}


def test_gone_is_persisted_and_reloaded(tmp_path):
    m = Manifest(str(tmp_path))
    m.mark_gone("dead1")
    m.flush(force=True)
    raw = read_raw(tmp_path)
    assert "dead1" in raw["gone"]

    reloaded = Manifest(str(tmp_path))
    assert reloaded.is_gone("dead1")
    assert not reloaded.is_gone("alive")


def test_recording_a_post_clears_its_gone_marker(tmp_path):
    """A gif can come back, or the 410 can have been a blip."""
    m = Manifest(str(tmp_path))
    m.mark_gone("x")
    m.record("x", ["X.mp4"])
    m.flush(force=True)
    assert read_raw(tmp_path).get("gone", {}) == {}
    assert not Manifest(str(tmp_path)).is_gone("x")


def test_corrupt_manifest_does_not_crash(tmp_path):
    (tmp_path / MANIFEST_NAME).write_text("{not json at all")
    m = Manifest(str(tmp_path))
    assert m.posts == {}
    # And it can still be written over.
    m.record("a", ["A.jpg"])
    assert m.flush(force=True)
    assert read_raw(tmp_path) == {"posts": {"a": ["A.jpg"]}}


def test_non_dict_manifest_does_not_crash(tmp_path):
    (tmp_path / MANIFEST_NAME).write_text("[1, 2, 3]")
    assert Manifest(str(tmp_path)).posts == {}


def test_flush_batches_until_the_threshold(tmp_path):
    m = Manifest(str(tmp_path))
    m.record("a", ["A.jpg"])
    assert m.flush(every=50) is False
    assert not (tmp_path / MANIFEST_NAME).exists()

    for i in range(60):
        m.record("p%d" % i, ["P%d.jpg" % i])
    assert m.flush(every=50) is True
    assert (tmp_path / MANIFEST_NAME).exists()


def test_flush_leaves_no_tmp_file(tmp_path):
    m = Manifest(str(tmp_path))
    m.record("a", ["A.jpg"])
    m.flush(force=True)
    assert [p.name for p in tmp_path.iterdir()] == [MANIFEST_NAME]


def test_claim_reserves_a_name_within_a_run(tmp_path):
    m = Manifest(str(tmp_path))
    m.claim("Taken.jpg")
    assert "Taken.jpg" in m.owned


def test_have_file_checks_the_filesystem(tmp_path):
    m = Manifest(str(tmp_path))
    assert not m.have_file("nope.jpg")
    (tmp_path / "yes.jpg").write_bytes(b"x")
    assert m.have_file("yes.jpg")


def test_stats_counts_only_files_that_exist(tmp_path):
    (tmp_path / "there.jpg").write_bytes(b"12345")
    m = Manifest(str(tmp_path))
    m.record("a", ["there.jpg"])
    m.record("b", ["deleted-by-hand.jpg"])
    stats = m.stats()
    assert stats == {"items": 2, "files": 1, "bytes": 5}


def test_manifest_creates_its_directory_on_flush(tmp_path):
    nested = tmp_path / "reddit" / "somebody"
    m = Manifest(str(nested))
    m.record("a", ["A.jpg"])
    assert m.flush(force=True)
    assert (nested / MANIFEST_NAME).exists()
