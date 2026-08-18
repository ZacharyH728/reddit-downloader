"""Input validation for values that reach the filesystem.

A creator name arrives as an HTTP path segment and is used to build
`<root>/<platform>/<creator>/`, so it is a path-traversal vector. Two independent
layers guard it: a strict allowlist regex, then a realpath containment check
(which also catches an escape via a symlink someone left in the media tree).
"""
import os
import re

PLATFORMS = ("reddit", "redgifs", "twitter")

# Reddit usernames: 3-20 chars of [A-Za-z0-9_-] in practice; 2 allows for the
# handful of legacy short accounts.
# X handles are 1-15 chars of [A-Za-z0-9_] - no dots or dashes, and shorter than
# the others, so this must not be relaxed into the redgifs pattern.
_PATTERNS = {
    "reddit": re.compile(r"\A[A-Za-z0-9_-]{2,20}\Z"),
    "redgifs": re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z"),
    "twitter": re.compile(r"\A[A-Za-z0-9_]{1,15}\Z"),
}


class ValidationError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def validate_platform(raw):
    value = (raw or "").strip().lower()
    if value not in PLATFORMS:
        raise ValidationError("invalid_platform",
                              "Unknown platform %r; expected one of %s"
                              % (raw, ", ".join(PLATFORMS)))
    return value


def validate_creator(platform, raw):
    """Return the canonical (lowercased) creator name, or raise ValidationError.

    The value is NOT decoded here. The web framework already URL-decoded the path
    segment once; decoding a second time is exactly how `%252e%252e` bypasses a
    check like this one.
    """
    platform = validate_platform(platform)
    value = (raw or "").strip()
    # "@handle" is how an X user writes their own name, so accept it and store the
    # bare form. Stripping here (not in the UI) keeps the canonical name identical
    # everywhere it matters: the route, the directory, and the manifest.
    if platform == "twitter" and value.startswith("@"):
        value = value[1:]
    if not value:
        raise ValidationError("invalid_creator", "Creator name is empty.")
    if not _PATTERNS[platform].match(value):
        raise ValidationError(
            "invalid_creator",
            "%r is not a valid %s username." % (raw, platform))
    lowered = value.lower()
    # Redundant given the regex (neither pattern can match a bare dot sequence),
    # kept because this is the check that must never silently regress.
    if lowered in (".", "..") or ".." in lowered or "/" in lowered or "\\" in lowered:
        raise ValidationError("invalid_creator", "Creator name contains a path segment.")
    return lowered


_ITEM_ID_PATTERNS = {
    "reddit": re.compile(r"\A[A-Za-z0-9]{2,16}\Z"),
    "redgifs": re.compile(r"\A[A-Za-z0-9]{3,64}\Z"),
    # Tweet IDs are numeric snowflakes - 19 digits today, so allow a little room
    # without accepting the unbounded input a `\d+` would.
    "twitter": re.compile(r"\A[0-9]{5,25}\Z"),
}


def validate_item_id(platform, raw):
    platform = validate_platform(platform)
    value = (raw or "").strip()
    if not _ITEM_ID_PATTERNS[platform].match(value):
        raise ValidationError("invalid_item_id", "%r is not a valid %s item id."
                              % (raw, platform))
    return value


def safe_child_dir(root, *parts):
    """Join `parts` under `root`, refusing anything that escapes it.

    Uses realpath rather than string prefixes so a symlink inside the tree can't
    be used to redirect writes outside it.
    """
    root_real = os.path.realpath(root)
    candidate = os.path.realpath(os.path.join(root_real, *parts))
    if candidate != root_real and not candidate.startswith(root_real + os.sep):
        raise ValidationError("path_escape",
                              "Refusing to write outside the download directory.")
    return candidate
