"""Backwards-compatible entrypoint for the saved-posts daemon.

The implementation now lives in core/ and the web UI entrypoint is app.py. This
shim keeps `python reddit_downloader.py` (and any existing Docker CMD or habit)
working exactly as before: the daemon, and nothing else.
"""
from core.sync import run_forever

if __name__ == "__main__":
    run_forever()
