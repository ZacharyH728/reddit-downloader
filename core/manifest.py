"""Filename derivation and the per-directory download manifest.

The manifest maps each downloaded post ID -> the list of filenames it owns. Dedup
is keyed on the unique post ID (not the title-derived filename), so two DIFFERENT
posts that happen to share a title don't collide: the second is saved under a name
suffixed with its post ID instead of being silently skipped.

One Manifest instance per directory. The flat saved-posts tree keeps its manifest
at the root; each creator directory gets its own, so a creator folder is
self-contained and portable.
"""
import json
import os
import re
import time
import unicodedata

from core.config import MANIFEST_NAME, logger

# The original illegal-character set. Do not extend this for the saved-posts path:
# every existing filename on disk was produced by exactly this substitution.
_ILLEGAL = re.compile(r'[\\/*?:"<>|]')
# Control characters and bidi overrides: invisible in a file browser, and the
# latter can make a filename display in a misleading order.
_CONTROL = re.compile("[%s]" % "".join([
    "\\x00-\\x1f\\x7f",      # C0 controls and DEL
    "\\u200e\\u200f",        # LRM, RLM
    "\\u202a-\\u202e",       # LRE, RLE, PDF, LRO, RLO
    "\\u2066-\\u2069",       # LRI, RLI, FSI, PDI
    "\\ufeff",               # BOM / zero-width no-break space
]))
_WHITESPACE = re.compile(r'\s+')
_RESERVED = frozenset(
    ["con", "prn", "aux", "nul"]
    + ["com%d" % i for i in range(1, 10)]
    + ["lpt%d" % i for i in range(1, 10)]
)


def sanitize_title(title, max_len=None):
    """Turn a post title into a filename stem.

    With `max_len=None` this is *exactly* the original transformation, which is
    what guarantees the saved-posts sync never renames a file that already exists
    on disk. Pass an integer (creator downloads only) to additionally normalize
    and truncate; a 300-character Reddit title otherwise exceeds the 255-byte
    filename limit on ext4 and raises OSError mid-job.
    """
    title = title or ""
    if max_len is None:
        return _ILLEGAL.sub("", title)

    stem = unicodedata.normalize("NFC", title)
    stem = _ILLEGAL.sub("", stem)
    # Collapse whitespace first, so a tab or newline becomes a space instead of
    # being deleted outright and welding two words together.
    stem = _WHITESPACE.sub(" ", stem)
    stem = _CONTROL.sub("", stem)
    stem = _WHITESPACE.sub(" ", stem).strip()
    stem = stem.strip(". -")

    # Truncate on a UTF-8 *byte* budget without splitting a codepoint.
    encoded = stem.encode("utf-8")
    if len(encoded) > max_len:
        stem = encoded[:max_len].decode("utf-8", "ignore")
        stem = stem.strip(". -")

    if stem.lower() in _RESERVED:
        stem = "_" + stem
    return stem


def resolve_filename(base, ext, post_id, owned_files):
    """Pick a filename for `base.ext`, appending `_<post_id>` if that name is
    already owned by a different post (title collision).

    The comparison is case-insensitive: APFS and SMB shares are case-insensitive,
    so treating `Foo.jpg` and `foo.jpg` as distinct would silently overwrite.
    """
    name = "%s.%s" % (base, ext)
    if name_taken(owned_files, name):
        name = "%s_%s.%s" % (base, post_id, ext)
    return name


def name_taken(owned_files, name):
    """Case-insensitive membership test against a set of claimed filenames."""
    if name in owned_files:
        return True
    lowered = name.lower()
    return any(existing.lower() == lowered for existing in owned_files)


class Manifest:
    """The `.download_manifest.json` for a single directory."""

    def __init__(self, directory):
        self.directory = directory
        self.path = os.path.join(directory, MANIFEST_NAME)
        self.posts = {}
        self.gone = {}
        self.owned = set()
        self._dirty = 0
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        if not isinstance(data, dict):
            return
        self.posts = data.get("posts") or {}
        self.gone = data.get("gone") or {}
        for files in self.posts.values():
            self.owned.update(files)

    # --- queries ---

    def has_post(self, post_id):
        return bool(post_id) and post_id in self.posts

    def is_gone(self, post_id):
        return bool(post_id) and post_id in self.gone

    def path_for(self, filename):
        return os.path.join(self.directory, filename)

    def have_file(self, filename):
        return os.path.exists(self.path_for(filename))

    def files_for(self, post_id):
        return list(self.posts.get(post_id, []))

    # --- mutations ---

    def claim(self, filename):
        """Reserve a filename so a later post in the same run can't reuse it."""
        self.owned.add(filename)

    def record(self, post_id, filenames):
        if not post_id or not filenames:
            return
        self.posts[post_id] = list(filenames)
        self.gone.pop(post_id, None)
        self._dirty += 1

    def mark_gone(self, post_id):
        """Remember that a post's media is deleted upstream.

        Without this, every cycle re-requests every dead item forever - which for
        RedGifs means an unpaced burst of 410s against a rate-limited API.
        """
        if not post_id:
            return
        self.gone[post_id] = int(time.time())
        self._dirty += 1

    def forget(self, post_id):
        if self.posts.pop(post_id, None) is not None:
            self._dirty += 1

    # --- persistence ---

    def flush(self, every=50, force=False):
        """Atomically persist so an interrupted run keeps its progress."""
        if not force and self._dirty < every:
            return False
        if not self._dirty and not force:
            return False
        payload = {"posts": self.posts}
        if self.gone:
            payload["gone"] = self.gone
        tmp = self.path + ".tmp"
        try:
            os.makedirs(self.directory, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, self.path)
            self._dirty = 0
            return True
        except OSError as e:
            logger.error("Failed to write download manifest: %s", e)
            return False

    def stats(self):
        files = 0
        total_bytes = 0
        for names in self.posts.values():
            for name in names:
                try:
                    total_bytes += os.path.getsize(self.path_for(name))
                    files += 1
                except OSError:
                    pass
        return {"items": len(self.posts), "files": files, "bytes": total_bytes}
