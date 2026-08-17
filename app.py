"""Entrypoint: the web UI plus the hourly saved-posts sync, in one process.

Set WEB_ENABLED=false to get the original daemon behavior and nothing else, or
SYNC_SAVED_ENABLED=false to get only the UI.
"""
import signal
import sys
import threading

import waitress

from core import config, jobs, net, sync
from core.config import logger
from web.server import create_app


def _log_startup_banner():
    for warning in config.startup_warnings():
        logger.warning(warning["message"])


def main():
    stop = threading.Event()

    def shutdown(signum, _frame):
        logger.info("Received signal %s, shutting down...", signum)
        stop.set()
        manager.stop()
        # waitress has no clean programmatic stop from a signal handler; the
        # in-flight job has been asked to cancel and manifests are flushed, so
        # exiting here is safe.
        sys.exit(0)

    _log_startup_banner()

    # Clear .part orphans from a previous hard kill before anything else runs.
    net.sweep_part_files(config.DOWNLOAD_LOCATION)
    if config.CREATOR_ROOT != config.DOWNLOAD_LOCATION:
        net.sweep_part_files(config.CREATOR_ROOT)

    manager = jobs.get_manager()
    logger.info("Job worker started (%d parallel downloads per job).", manager.concurrency)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    if config.SYNC_SAVED_ENABLED:
        sync.start_sync_thread(stop)
        logger.info("Saved-posts sync thread started (every %d seconds).",
                    config.TIME_BETWEEN_DOWNLOADS)
    else:
        logger.info("Saved-posts sync is disabled (SYNC_SAVED_ENABLED=false).")

    if not config.WEB_ENABLED:
        logger.info("Web UI is disabled (WEB_ENABLED=false); running as a daemon only.")
        stop.wait()
        return

    app = create_app(manager)
    logger.info("Web UI listening on http://%s:%d", config.WEB_HOST, config.WEB_PORT)
    waitress.serve(app, host=config.WEB_HOST, port=config.WEB_PORT,
                   threads=config.WEB_THREADS, ident="reddit-downloader")


if __name__ == "__main__":
    main()
