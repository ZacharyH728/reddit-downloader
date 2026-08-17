"""The saved-posts sync: the original hourly daemon, unchanged in behavior.

Downloads media from the authenticated user's saved posts into the flat
DOWNLOAD_LOCATION, using the same filenames it always has. The per-post dispatch
now goes through core.reddit_api so the creator-download jobs share it.
"""
import os
import threading
import time

import prawcore
import requests

from core import config, net, reddit_api
from core.config import logger
from core.manifest import Manifest
from core.redgifs import RedGifsClient

# Set by POST /api/sync/run to wake the sleeping daemon thread immediately.
WAKE = threading.Event()

# Last-cycle state, read by GET /api/sync.
STATE = {
    "enabled": config.SYNC_SAVED_ENABLED,
    "interval": config.TIME_BETWEEN_DOWNLOADS,
    "running": False,
    "last_started": None,
    "last_finished": None,
    "next_run": None,
    "last_error": None,
    "summary": None,
}
_state_lock = threading.Lock()


def _set_state(**kwargs):
    with _state_lock:
        STATE.update(kwargs)


def get_state():
    with _state_lock:
        return dict(STATE)


def sync_saved(dest_dir=None, reddit=None, redgifs=None, session=None):
    """Run one full pass over the saved posts. Returns a summary dict."""
    dest_dir = dest_dir or config.DOWNLOAD_LOCATION
    if not os.path.exists(dest_dir):
        os.makedirs(dest_dir)

    session = session or net.make_session()
    reddit = reddit or reddit_api.make_reddit(session)
    redgifs = redgifs or RedGifsClient()

    logger.info("Successfully authenticated with Reddit.")

    deleted_count = 0
    skipped_files_count = 0
    downloaded_files_count = 0
    failed_files_count = 0

    manifest = Manifest(dest_dir)

    # Materialize and process OLDEST-first: a pre-existing file is then claimed by
    # the older post that owns it, and any newer same-title save is the one that
    # gets a post-ID-suffixed name and an actual download (instead of being
    # skipped as a false duplicate).
    saved_posts = list(reddit.user.me().saved(limit=None))
    logger.info("Processing %d saved items (oldest-first).", len(saved_posts))

    for post in reversed(saved_posts):
        post_id = getattr(post, "id", None)

        # Fast path: this exact post was already downloaded in a previous run.
        # (Delete .download_manifest.json to force a full re-verify.)
        if post_id and manifest.has_post(post_id):
            skipped_files_count += 1
            continue

        # Known deleted upstream. Without this the daemon re-requested every dead
        # item every cycle, back to back, which for RedGifs is an unpaced burst
        # of 410s against a rate-limited API.
        if post_id and manifest.is_gone(post_id):
            deleted_count += 1
            continue

        desc = reddit_api.describe_submission(post)
        if desc is None:
            logger.debug("No handler for saved item %s: %s", post_id,
                         getattr(post, "url", "not a media submission"))
            continue

        try:
            # max_len=None keeps the original filename derivation exactly, so no
            # file already on disk is ever renamed.
            filenames = reddit_api.plan_filenames(desc, manifest.owned, max_len=None)
            for name in filenames:
                manifest.claim(name)

            missing = [n for n in filenames if not manifest.have_file(n)]
            if not missing:
                for name in filenames:
                    logger.trace("Have: %s", name)
                    skipped_files_count += 1
                manifest.record(post_id, filenames)
                manifest.flush()
                continue

            result = reddit_api.download_planned(
                desc, filenames, dest_dir, session, redgifs=redgifs)

            status = result["status"]
            if status == net.GONE:
                deleted_count += 1
                manifest.mark_gone(post_id)
                manifest.flush()
                continue
            if status == net.FAILED:
                failed_files_count += len(missing)
                logger.warning("Failed: %s (%s)", filenames[0], result.get("error"))
                # Not recorded, so a transient failure is retried next cycle.
                # Permanently dead URLs come back as GONE, not FAILED.
                continue

            downloaded_files_count += len(missing)
            skipped_files_count += len(filenames) - len(missing)
            manifest.record(post_id, filenames)
            manifest.flush()

        except requests.exceptions.HTTPError as e:
            if getattr(e.response, "status_code", None) == 410:
                deleted_count += 1
                if post_id:
                    manifest.mark_gone(post_id)
            else:
                logger.error("Error processing %s: %s", desc.get("url"), e)
            continue
        except OSError as e:
            logger.error("Filesystem error, aborting cycle: %s", e)
            break
        except Exception as e:
            logger.error("Error processing %s: %s", desc.get("url"), e)
            continue

    manifest.flush(force=True)

    logger.info(
        "Cycle summary: %d downloaded, %d already had, %d failed, %d deleted upstream.",
        downloaded_files_count, skipped_files_count, failed_files_count, deleted_count)

    return {
        "downloaded": downloaded_files_count,
        "skipped": skipped_files_count,
        "failed": failed_files_count,
        "gone": deleted_count,
        "total": len(saved_posts),
    }


def run_forever(stop_event=None, redgifs=None):
    """The original daemon loop, with an interruptible sleep."""
    stop_event = stop_event or threading.Event()
    session = net.make_session()
    reddit = reddit_api.make_reddit(session)

    while not stop_event.is_set():
        logger.info("-------------------------------------------")
        logger.info("Starting new download cycle...")
        _set_state(running=True, last_started=time.time(), last_error=None)
        try:
            summary = sync_saved(reddit=reddit, redgifs=redgifs, session=session)
            _set_state(summary=summary)
        except (prawcore.exceptions.RequestException,
                requests.exceptions.RequestException) as e:
            logger.error("Connection error occurred: %s", e)
            logger.info("Will retry in 60 seconds...")
            _set_state(running=False, last_finished=time.time(), last_error=str(e),
                       next_run=time.time() + 60)
            if stop_event.wait(timeout=60):
                break
            continue
        except Exception as e:
            logger.exception("An unexpected error occurred: %s", e)
            logger.info("Will retry after the delay.")
            _set_state(last_error=str(e))

        interval = config.TIME_BETWEEN_DOWNLOADS
        _set_state(running=False, last_finished=time.time(),
                   next_run=time.time() + interval)
        logger.info("Download cycle finished. Waiting %d seconds...", interval)
        logger.info("-------------------------------------------\n")

        # Wake early if the UI asks for a run, or if we're shutting down.
        deadline = time.monotonic() + interval
        while not stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if WAKE.wait(timeout=min(remaining, 5)):
                WAKE.clear()
                logger.info("Sync triggered manually.")
                break


def start_sync_thread(stop_event, redgifs=None):
    thread = threading.Thread(
        target=run_forever, args=(stop_event, redgifs),
        name="saved-sync", daemon=True)
    thread.start()
    return thread
