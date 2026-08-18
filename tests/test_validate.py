"""Validation of anything that reaches the filesystem.

The creator name arrives as an HTTP path segment and becomes a directory name, so
these are the tests that stop a request from writing outside the download tree.
"""
import os

import pytest

from core.validate import (ValidationError, safe_child_dir, validate_creator,
                           validate_item_id, validate_platform)


@pytest.mark.parametrize("raw", ["reddit", "REDDIT", " Reddit ", "redgifs"])
def test_valid_platforms(raw):
    assert validate_platform(raw) in ("reddit", "redgifs")


@pytest.mark.parametrize("raw", ["", None, "facebook", "../reddit", "reddit/x", "redditx"])
def test_invalid_platforms(raw):
    with pytest.raises(ValidationError) as exc:
        validate_platform(raw)
    assert exc.value.code == "invalid_platform"


@pytest.mark.parametrize("raw,expected", [
    ("spez", "spez"),
    ("Spez", "spez"),
    ("SPEZ", "spez"),
    ("  spez  ", "spez"),
    ("some_user", "some_user"),
    ("some-user", "some-user"),
    ("a1", "a1"),
    ("x" * 20, "x" * 20),
])
def test_valid_reddit_creators(raw, expected):
    assert validate_creator("reddit", raw) == expected


@pytest.mark.parametrize("raw", [
    "..",
    ".",
    "../../etc/passwd",
    "..%2f..%2fetc",
    "../etc",
    "foo/bar",
    "foo\\bar",
    "/absolute",
    "~root",
    "foo\x00bar",
    "a",                    # too short
    "x" * 21,               # too long
    "",
    None,
    "with space",
    "unicodeé",
    "semi;colon",
    "dot.name",             # legal on redgifs, not on reddit
    "%2e%2e",
    "%2e%2e%2f",
    "....//",
    "foo\nbar",
])
def test_rejected_reddit_creators(raw):
    with pytest.raises(ValidationError) as exc:
        validate_creator("reddit", raw)
    assert exc.value.code == "invalid_creator"


@pytest.mark.parametrize("raw", ["..", ".", "../x", "a/b", ".hidden", "-lead", "_lead"])
def test_rejected_redgifs_creators(raw):
    with pytest.raises(ValidationError):
        validate_creator("redgifs", raw)


@pytest.mark.parametrize("raw,expected", [
    ("someuser", "someuser"),
    ("Some.User", "some.user"),
    ("user_1-2.3", "user_1-2.3"),
])
def test_valid_redgifs_creators(raw, expected):
    assert validate_creator("redgifs", raw) == expected


def test_pre_decoded_traversal_is_rejected():
    """Flask decodes the path segment once. If we decoded again, %252e%252e would
    turn into '..' after our check had already passed."""
    with pytest.raises(ValidationError):
        validate_creator("reddit", "%252e%252e%252f")
    with pytest.raises(ValidationError):
        validate_creator("reddit", "..%2F..")


@pytest.mark.parametrize("raw", ["abc123", "t3abc", "AB12"])
def test_valid_item_ids(raw):
    assert validate_item_id("reddit", raw) == raw


@pytest.mark.parametrize("raw", ["", "a", "../x", "abc-123", "abc_123", "x" * 20, "a b"])
def test_rejected_reddit_item_ids(raw):
    with pytest.raises(ValidationError):
        validate_item_id("reddit", raw)


# --- containment ------------------------------------------------------------

def test_safe_child_dir_builds_the_expected_path(tmp_path):
    result = safe_child_dir(str(tmp_path), "reddit", "spez")
    assert result == os.path.join(os.path.realpath(str(tmp_path)), "reddit", "spez")


@pytest.mark.parametrize("parts", [
    ("..",),
    ("..", ".."),
    ("reddit", "..", "..", "escaped"),
    (os.sep + "etc",),
])
def test_safe_child_dir_rejects_escapes(tmp_path, parts):
    with pytest.raises(ValidationError) as exc:
        safe_child_dir(str(tmp_path), *parts)
    assert exc.value.code == "path_escape"


def test_safe_child_dir_rejects_a_symlink_escape(tmp_path):
    """A string prefix check would pass this; realpath resolution catches it."""
    outside = tmp_path.parent / "outside_target"
    outside.mkdir(exist_ok=True)
    root = tmp_path / "root"
    root.mkdir()
    (root / "reddit").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValidationError) as exc:
        safe_child_dir(str(root), "reddit", "spez")
    assert exc.value.code == "path_escape"


def test_safe_child_dir_allows_the_root_itself(tmp_path):
    assert safe_child_dir(str(tmp_path)) == os.path.realpath(str(tmp_path))


def test_validated_name_can_never_contain_a_separator():
    """Belt and braces: whatever validate_creator returns must be a single path
    segment, because it is concatenated into a directory path."""
    for candidate in ["spez", "Some.User", "a-b_c"]:
        for platform in ("reddit", "redgifs"):
            try:
                value = validate_creator(platform, candidate)
            except ValidationError:
                continue
            assert os.sep not in value
            assert "/" not in value and "\\" not in value
            assert value not in (".", "..")
            assert os.path.basename(value) == value
