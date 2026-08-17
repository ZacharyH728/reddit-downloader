"""Environment configuration and logging setup.

Everything here was previously the top-of-file block in reddit_downloader.py.
The existing variables keep their exact names, defaults, and semantics; the new
ones are all defaulted so an existing deployment keeps working untouched.
"""
import logging
import os
import shutil
import sys

from dotenv import load_dotenv

load_dotenv()


def _bool(name, default):
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name, default):
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name, default):
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# --- Logging Setup ---
TRACE_LEVEL_NUM = 5
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")


def trace(self, message, *args, **kws):
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kws)


logging.Logger.trace = trace

# Get log level from environment, default to INFO
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
numeric_level = getattr(logging, LOG_LEVEL, logging.INFO)

if LOG_LEVEL == "TRACE":
    numeric_level = TRACE_LEVEL_NUM

logging.basicConfig(
    level=numeric_level,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

logger = logging.getLogger("reddit_downloader")

# --- Environment Variables ---
CLIENT_ID = os.getenv("REDDIT_CLIENT_ID")
CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET")
USER_AGENT = os.getenv("REDDIT_USER_AGENT")
USERNAME = os.getenv("REDDIT_USERNAME")
PASSWORD = os.getenv("REDDIT_PASSWORD")
DOWNLOAD_LOCATION = os.getenv("DOWNLOAD_LOCATION", "./downloads")
TIME_BETWEEN_DOWNLOADS = int(os.getenv("TIME_BETWEEN_DOWNLOADS", "3600"))  # in seconds

# --- Web UI ---
# There is no authentication. Serve this on a private network only.
WEB_ENABLED = _bool("WEB_ENABLED", True)
# 0.0.0.0 is required for `docker run -p` to reach the server at all.
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = _int("WEB_PORT", 8080)
# Each in-flight request (including a playing video) holds one waitress thread
# for its entire lifetime, so this needs headroom over MAX_STREAMS.
WEB_THREADS = _int("WEB_THREADS", 16)

# --- Saved-posts sync ---
SYNC_SAVED_ENABLED = _bool("SYNC_SAVED_ENABLED", True)

# --- Creator downloads ---
# Root for downloads/<platform>/<creator>/. Defaults to the same tree as saved
# posts so the sibling gallery app picks creator content up automatically.
CREATOR_ROOT = os.getenv("CREATOR_ROOT", DOWNLOAD_LOCATION)
DOWNLOAD_CONCURRENCY = _int("DOWNLOAD_CONCURRENCY", 3)
# Reddit titles reach 300 chars; ext4 caps a filename at 255 *bytes*.
CREATOR_TITLE_MAX_LEN = _int("CREATOR_TITLE_MAX_LEN", 120)
JOB_HISTORY_LIMIT = _int("JOB_HISTORY_LIMIT", 50)
MIN_FREE_DISK_MB = _int("MIN_FREE_DISK_MB", 2048)

# --- Media proxy ---
MAX_STREAMS = _int("MAX_STREAMS", 8)
PROXY_MAX_MB = _int("PROXY_MAX_MB", 512)
# Kill switch: force every media URL through /api/proxy instead of hotlinking.
MEDIA_PROXY_ALWAYS = _bool("MEDIA_PROXY_ALWAYS", False)

# --- RedGifs ---
# Replaces the old hardcoded `time.sleep(1)` after each RedGifs download.
REDGIFS_MIN_INTERVAL = _float("REDGIFS_MIN_INTERVAL", 1.0)

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None
if not FFMPEG_AVAILABLE:
    logger.warning("ffmpeg not found on PATH: v.redd.it videos will be saved without audio.")

# --- Download manifest ---
# Maps each downloaded Reddit post ID -> list of filenames it owns. Dedup is keyed
# on the unique post ID (not the title-derived filename), so two DIFFERENT posts
# that happen to share a title no longer collide: the second one is saved under a
# name suffixed with its post ID instead of being silently skipped. Posts are
# processed oldest-first, so a pre-existing file is claimed by the OLDER post and
# a newer same-title save is the one that gets the suffix + a real download.
MANIFEST_NAME = ".download_manifest.json"
MANIFEST_FILE = os.path.join(DOWNLOAD_LOCATION, MANIFEST_NAME)

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def startup_warnings():
    """Config problems worth telling the user about, surfaced in logs and /api/health."""
    warnings = []
    if not all([CLIENT_ID, CLIENT_SECRET, USER_AGENT, USERNAME, PASSWORD]):
        warnings.append({
            "code": "reddit_credentials_missing",
            "message": (
                "One or more of REDDIT_CLIENT_ID / _SECRET / _USER_AGENT / _USERNAME / "
                "_PASSWORD is unset. Reddit search and downloads will fail."
            ),
        })
    if not FFMPEG_AVAILABLE:
        warnings.append({
            "code": "ffmpeg_missing",
            "message": "ffmpeg not found on PATH: v.redd.it videos will be saved without audio.",
        })
    return warnings
