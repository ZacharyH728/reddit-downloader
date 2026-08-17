"""HTTP sessions and the download primitives.

Extracted from reddit_downloader.py with three deliberate changes:

1. `download_file` takes a full destination *path* instead of a filename that it
   joins onto the DOWNLOAD_LOCATION global. That global was the only thing
   preventing per-creator output directories.
2. `download_file` returns a truthful status string. It used to return True when
   it SKIPPED an existing file and False after a SUCCESSFUL download (as well as
   on failure), which was harmless only because every call site ignored it.
3. Downloads stream into a sibling `.part` file and are verified before being
   moved into place. Previously a stream that died mid-write left a truncated
   file at the final path, which the "do I already have this?" check then treated
   as complete forever.
"""
import os
import re
import shutil
import subprocess
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core import config
from core.config import logger

# Statuses returned by download_file / download_reddit_video.
DOWNLOADED = "downloaded"
SKIPPED = "skipped"
FAILED = "failed"
GONE = "gone"
CANCELLED = "cancelled"

CHUNK_SIZE = 65536
PART_SUFFIX = ".part"

_MEDIA_EXTS = frozenset([
    "jpg", "jpeg", "png", "gif", "webp", "bmp",
    "mp4", "m4v", "webm", "mov", "mkv", "m4a", "mp3",
])
# Content types that are never valid media: a CDN error page saved as .jpg would
# otherwise be cached as "already downloaded" permanently.
_BAD_TYPE_RE = re.compile(r"^(text/|application/(json|xml|xhtml))", re.I)


def make_session(pool=10):
    """The background session: patient retries, matching the original behavior."""
    session = requests.Session()
    # Good practice: ignore env proxies to prevent accidental interference
    session.trust_env = False

    retries = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS", "POST"]
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=pool, pool_maxsize=pool)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def make_interactive_session(pool=10):
    """The session for requests made while a user waits.

    The background policy honors Retry-After, so a Reddit 429 carrying
    `Retry-After: 600` would block a web worker thread for ten minutes. Requests
    on this session fail fast instead, so the UI can show a real error.
    """
    session = requests.Session()
    session.trust_env = False
    retries = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS", "POST"],
        respect_retry_after_header=False,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=pool, pool_maxsize=pool)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _never_cancel():
    return False


def _status_of(exc):
    """Map a requests exception onto GONE (permanent) or FAILED (retryable)."""
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if code in (404, 410):
        return GONE
    return FAILED


def _verify(part_path, response, min_bytes, ext):
    """Return None if the downloaded body looks like real, complete media."""
    size = os.path.getsize(part_path)
    if size < min_bytes:
        return "body too small (%d bytes)" % size

    # A truncated stream is the failure this exists to catch. Content-Length is
    # only trustworthy when the body wasn't transparently decompressed.
    declared = response.headers.get("content-length")
    if declared and not response.headers.get("content-encoding"):
        try:
            expected = int(declared)
        except ValueError:
            expected = None
        if expected is not None and expected != size:
            return "truncated: got %d bytes, expected %d" % (size, expected)

    content_type = response.headers.get("content-type", "")
    if ext in _MEDIA_EXTS and _BAD_TYPE_RE.match(content_type):
        return "server returned %s for a .%s file" % (content_type.split(";")[0], ext)
    return None


def download_file(url, filepath, session=None, check_size=False, should_cancel=None,
                  min_bytes=1024, timeout=(10, 60), expect_ext=None):
    """Download `url` to `filepath`, atomically.

    Returns one of: "downloaded", "skipped", "failed", "gone", "cancelled".
    Raises OSError for local filesystem problems (out of space, permissions) so
    the caller can abort the whole job instead of grinding through failures.
    """
    should_cancel = should_cancel or _never_cancel
    requester = session if session else requests
    filename = os.path.basename(filepath)

    logger.trace("Checking file: %s", filename)

    if os.path.exists(filepath):
        if not check_size:
            logger.trace("Skipped: %s already exists.", filename)
            return SKIPPED

        try:
            head_response = requester.head(url, allow_redirects=True, timeout=15)
            remote_size = int(head_response.headers.get('content-length', 0))
            local_size = os.path.getsize(filepath)

            if remote_size > 0 and remote_size == local_size:
                logger.trace("Skipped: %s already exists and size matches.", filename)
                return SKIPPED

            logger.info("Redownloading %s: Local size %d vs Remote size %d",
                        filename, local_size, remote_size)
        except requests.exceptions.RequestException as e:
            logger.error("Error checking size for %s: %s", filename, e)
            return _status_of(e)

    part_path = filepath + PART_SUFFIX
    try:
        try:
            response = requester.get(url, stream=True, timeout=timeout)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.error("Error downloading %s: %s", url, e)
            return _status_of(e)

        with response:
            with open(part_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if should_cancel():
                        logger.debug("Cancelled mid-download: %s", filename)
                        return CANCELLED
                    if chunk:
                        f.write(chunk)

            ext = (expect_ext or filepath.rsplit(".", 1)[-1]).lower().lstrip(".")
            problem = _verify(part_path, response, min_bytes, ext)

        if problem:
            logger.error("Discarding %s: %s", filename, problem)
            return FAILED

        os.replace(part_path, filepath)
        logger.trace("Downloaded: %s", filename)
        return DOWNLOADED
    except requests.exceptions.RequestException as e:
        # A connection dropped mid-stream surfaces here rather than on .get().
        logger.error("Error downloading %s: %s", url, e)
        return _status_of(e)
    finally:
        # Never leave a partial file behind, at the final path or beside it.
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except OSError:
                pass


def url_exists(url, session):
    """HEAD-checks a URL without logging on a plain 404 (used for the optional
    audio track, which most v.redd.it videos simply don't have)."""
    try:
        response = session.head(url, allow_redirects=True, timeout=15)
        return response.status_code == 200
    except requests.exceptions.RequestException:
        return False


def audio_url_for(video_url):
    """Reddit serves v.redd.it audio as a separate DASH track alongside video."""
    return re.sub(r'DASH_\d+\.mp4.*$', 'DASH_audio.mp4', video_url)


def download_reddit_video(video_url, filepath, session, should_cancel=None, label=None):
    """Download a v.redd.it (native Reddit-hosted) video.

    Reddit serves video and audio as separate DASH tracks and the video track is
    silent, so this fetches the matching audio track when one exists and muxes
    the two with ffmpeg, falling back to video-only if there's no audio track or
    ffmpeg isn't available.
    """
    should_cancel = should_cancel or _never_cancel
    label = label or os.path.basename(filepath)

    video_tmp = filepath + ".video.tmp"
    audio_tmp = filepath + ".audio.tmp"

    try:
        result = download_file(video_url, video_tmp, session=session,
                               should_cancel=should_cancel, expect_ext="mp4")
        if result != DOWNLOADED:
            if result == FAILED:
                logger.error("Failed to download v.redd.it video: %s", video_url)
            return result

        audio_url = audio_url_for(video_url)
        has_audio = config.FFMPEG_AVAILABLE and url_exists(audio_url, session)
        if has_audio:
            has_audio = download_file(audio_url, audio_tmp, session=session,
                                      should_cancel=should_cancel,
                                      expect_ext="m4a") == DOWNLOADED

        if should_cancel():
            return CANCELLED

        if has_audio:
            try:
                completed = subprocess.run(
                    ['ffmpeg', '-y', '-i', video_tmp, '-i', audio_tmp,
                     '-c', 'copy', '-map', '0:v:0', '-map', '1:a:0', filepath],
                    capture_output=True, timeout=600,
                )
                if completed.returncode != 0 or not os.path.exists(filepath):
                    raise RuntimeError(completed.stderr.decode(errors='ignore')[-300:])
            except (subprocess.SubprocessError, RuntimeError, OSError) as e:
                logger.warning("ffmpeg mux failed for %s, saving video-only: %s", label, e)
                if os.path.exists(video_tmp):
                    os.replace(video_tmp, filepath)
        else:
            os.replace(video_tmp, filepath)

        if not os.path.exists(filepath):
            return FAILED

        logger.trace("Downloaded: %s", label)
        return DOWNLOADED
    except OSError:
        raise
    except Exception as e:
        logger.error("Error processing v.redd.it video %s: %s", video_url, e)
        return FAILED
    finally:
        for tmp in (video_tmp, audio_tmp):
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


def sweep_part_files(root, older_than=3600):
    """Remove `.part` orphans left behind by a SIGKILL (docker kill, OOM)."""
    if not os.path.isdir(root):
        return 0
    cutoff = time.time() - older_than
    removed = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not name.endswith(PART_SUFFIX):
                continue
            path = os.path.join(dirpath, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
    if removed:
        logger.info("Cleaned up %d orphaned .part file(s) under %s", removed, root)
    return removed


def free_disk_mb(path):
    """Free space on the filesystem holding `path` (walking up to an existing dir)."""
    probe = os.path.abspath(path)
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return shutil.disk_usage(probe).free // (1024 * 1024)
    except OSError:
        return None
