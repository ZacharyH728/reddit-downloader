"""Input validation for values that reach the filesystem.

A creator name arrives as an HTTP path segment and is used to build
`<root>/<platform>/<creator>/`, so it is a path-traversal vector. Two independent
layers guard it: a strict allowlist regex, then a realpath containment check
(which also catches an escape via a symlink someone left in the media tree).
"""
import os
import re

PLATFORMS = ("reddit", "redgifs")

# Reddit usernames: 3-20 chars of [A-Za-z0-9_-] in practice; 2 allows for the
# handful of legacy short accounts.
_PATTERNS = {
    "reddit": re.compile(r"\A[A-Za-z0-9_-]{2,20}\Z"),
    "redgifs": re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z"),
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
