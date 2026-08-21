"""Background download jobs.

One job runs at a time and the rest queue. That single decision buys a lot: there
is exactly one writer per creator directory and manifest (so no file locking
anywhere), the outbound request rate stays bounded, and progress is unambiguous.
Inside a job, DOWNLOAD_CONCURRENCY items download in parallel.

Two invariants that are easy to get wrong and hard to debug:

1. Filenames are assigned on the worker thread, never inside a parallel task. Two
   same-titled posts resolving names concurrently would both pick `title.jpg`.
2. Manifest writes happen only on the worker thread. Tasks hand results back
   through futures, so the manifest needs no lock at all.
"""
import collections
import os
import queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from core import config, creators, net, reddit_api
from core.config import logger
from core.manifest import Manifest
from core.validate import validate_creator, validate_item_id, validate_platform

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
DONE_WITH_ERRORS = "done_with_errors"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL = frozenset([DONE, DONE_WITH_ERRORS, FAILED, CANCELLED])
ACTIVE = frozenset([QUEUED, RUNNING])

# How many per-item error details to keep. Counters stay exact beyond this.
MAX_ERROR_DETAIL = 100


class JobError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class Job:
    def __init__(self, platform, creator, mode, item_ids=None, kind="all", only="all"):
        self.id = uuid.uuid4().hex[:12]
        self.platform = platform
        self.creator = creator
        self.mode = mode
        self.item_ids = list(item_ids or [])
        # Filters, mirroring the grid's own. Only meaningful for mode="all" —
        # a "selected" job already carries an explicit id list.
        self.kind = kind
        self.only = only
        self.state = QUEUED
        self.phase = "queued"
        self.total = len(self.item_ids) if mode == "selected" else None
        self.completed = 0
        self.downloaded = 0
        self.skipped = 0
        self.failed = 0
        self.gone = 0
        self.current = None
        self.dest = creators.creator_label(platform, creator)
        self.created_at = time.time()
        self.started_at = None
        self.finished_at = None
        self.error = None
        self.errors = []
        self.failed_ids = []
        self.cancel_requested = False

    def as_dict(self):
        return {
            "id": self.id,
            "platform": self.platform,
            "creator": self.creator,
            "mode": self.mode,
            "kind": self.kind,
            "only": self.only,
            "state": self.state,
            "phase": self.phase,
            "total": self.total,
            "completed": self.completed,
            "downloaded": self.downloaded,
            "skipped": self.skipped,
            "failed": self.failed,
            "gone": self.gone,
            "current": self.current,
            "dest": self.dest,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "errors": list(self.errors),
            "failed_ids": list(self.failed_ids),
            "active": self.state in ACTIVE,
        }


class JobManager:
    def __init__(self, concurrency=None, history=None):
        self.concurrency = max(1, concurrency or config.DOWNLOAD_CONCURRENCY)
        self.history_limit = history or config.JOB_HISTORY_LIMIT
        self._jobs = collections.OrderedDict()
        self._queue = queue.Queue()
        self._lock = threading.RLock()
        self._worker = None
        self._stop = threading.Event()

    # --- lifecycle ------------------------------------------------------

    def start(self):
        if self._worker is None:
            self._worker = threading.Thread(target=self._run, name="job-worker", daemon=True)
            self._worker.start()
        return self._worker

    def stop(self):
        self._stop.set()
        with self._lock:
            for job in self._jobs.values():
                if job.state in ACTIVE:
                    job.cancel_requested = True
        self._queue.put(None)

    # --- submission -----------------------------------------------------

    def submit(self, platform, creator, mode, item_ids=None, kind="all", only="all"):
        platform = validate_platform(platform)
        creator = validate_creator(platform, creator)
        if mode not in ("all", "selected"):
            raise JobError("invalid_mode", "mode must be 'all' or 'selected'.")

        kind = kind or "all"
        if kind != "all" and kind not in creators.KIND_FILTERS:
            raise JobError("invalid_kind", "kind must be 'all' or one of %s."
                           % ", ".join(sorted(creators.KIND_FILTERS)))
        only = only or "all"
        # list_items also accepts "have", which is meaningless for a download —
        # every item would be skipped as already present. Rejecting it beats
        # accepting a request that provably downloads nothing.
        if only not in ("all", "missing"):
            raise JobError("invalid_only", "only must be 'all' or 'missing'.")

        ids = []
        if mode == "selected":
            if not item_ids:
                raise JobError("no_items", "No items were selected.")
            if len(item_ids) > 5000:
                raise JobError("too_many_items", "Select at most 5000 items per job.", 413)
            seen = set()
            for raw in item_ids:
                item_id = validate_item_id(platform, raw)
                if item_id not in seen:
                    seen.add(item_id)
                    ids.append(item_id)

        with self._lock:
            existing = self._active_for(platform, creator)
            if existing:
                raise JobError(
                    "job_exists",
                    "A download for %s/%s is already %s." % (platform, creator, existing.state),
                    409)

        ok, free = creators.free_space_ok(platform, creator)
        if not ok:
            raise JobError(
                "insufficient_disk",
                "Only %d MB free; need at least %d MB." % (free, config.MIN_FREE_DISK_MB),
                507)

        job = Job(platform, creator, mode, ids, kind=kind, only=only)
        with self._lock:
            self._jobs[job.id] = job
            self._trim()
        self._queue.put(job.id)
        logger.info("Queued %s job for %s/%s (kind=%s, only=%s) (%s)",
                    mode, platform, creator, kind, only, job.id)
        return job.as_dict()

    def retry_failed(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobError("not_found", "No such job.", 404)
            if job.state in ACTIVE:
                raise JobError("still_running", "That job is still running.", 409)
            ids = list(job.failed_ids)
            platform, creator = job.platform, job.creator
        if not ids:
            raise JobError("nothing_to_retry", "That job has no failed items.")
        return self.submit(platform, creator, "selected", ids)

    def cancel(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobError("not_found", "No such job.", 404)
            if job.state in TERMINAL:
                return job.as_dict()
            job.cancel_requested = True
            if job.state == QUEUED:
                # Never started, so finish it here rather than waiting for the
                # worker to pick it up just to throw it away.
                job.state = CANCELLED
                job.phase = "cancelled"
                job.finished_at = time.time()
            else:
                job.phase = "cancelling"
            return job.as_dict()

    # --- reads ----------------------------------------------------------

    def get(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            return job.as_dict() if job else None

    def list(self, limit=20):
        with self._lock:
            jobs = [j.as_dict() for j in self._jobs.values()]
        jobs.sort(key=lambda j: (not j["active"], -(j["created_at"] or 0)))
        return jobs[:limit]

    def active_count(self):
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.state in ACTIVE)

    def _active_for(self, platform, creator):
        for job in self._jobs.values():
            if job.platform == platform and job.creator == creator and job.state in ACTIVE:
                return job
        return None

    def _trim(self):
        finished = [jid for jid, j in self._jobs.items() if j.state in TERMINAL]
        while len(finished) > self.history_limit:
            self._jobs.pop(finished.pop(0), None)

    # --- worker ---------------------------------------------------------

    def _run(self):
        while not self._stop.is_set():
            job_id = self._queue.get()
            if job_id is None:
                break
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None or job.state != QUEUED:
                    continue
                job.state = RUNNING
                job.phase = "starting"
                job.started_at = time.time()
            try:
                self._execute(job)
            except Exception as e:  # noqa: BLE001 - a job must never kill the worker
                logger.exception("Job %s crashed: %s", job.id, e)
                with self._lock:
                    job.state = FAILED
                    job.error = str(e)
            finally:
                with self._lock:
                    if job.state not in TERMINAL:
                        if job.cancel_requested:
                            job.state = CANCELLED
                        elif job.failed or job.gone:
                            job.state = DONE_WITH_ERRORS
                        else:
                            job.state = DONE
                    job.phase = job.state
                    job.current = None
                    job.finished_at = time.time()
                    self._trim()
                logger.info(
                    "Job %s finished: %s (%d downloaded, %d already had, %d failed, %d gone)",
                    job.id, job.state, job.downloaded, job.skipped, job.failed, job.gone)

    def _set(self, job, **kwargs):
        with self._lock:
            for key, value in kwargs.items():
                setattr(job, key, value)

    def _note_error(self, job, item_id, title, message):
        with self._lock:
            job.failed += 1
            if item_id:
                job.failed_ids.append(item_id)
            if len(job.errors) < MAX_ERROR_DETAIL:
                job.errors.append({"id": item_id, "title": title, "error": message})

    def _execute(self, job):
        directory = creators.creator_dir(job.platform, job.creator)
        os.makedirs(directory, exist_ok=True)
        net.sweep_part_files(directory, older_than=0)

        manifest = Manifest(directory)
        session = net.make_session(pool=self.concurrency * 2)
        redgifs = creators.get_redgifs()
        max_len = config.CREATOR_TITLE_MAX_LEN

        def cancelled():
            return job.cancel_requested or self._stop.is_set()

        if job.mode == "selected":
            self._set(job, phase="resolving")
            descs, missing = creators.resolve_selected(job.platform, job.creator, job.item_ids)
            for item_id in missing:
                with self._lock:
                    job.gone += 1
                    job.completed += 1
                manifest.mark_gone(item_id)
            self._set(job, phase="downloading", total=len(descs) + len(missing))
            self._download_batch(job, descs, manifest, directory, session, redgifs,
                                 max_len, cancelled)
        else:
            self._set(job, phase="enumerating")
            # A job started from a filtered grid takes only what the grid showed.
            # `kind` is decided from the listing alone, so creators applies it
            # while paging; `only` needs the manifest, which lives here.
            source = self._apply_only(job, manifest, creators.iter_all_items(
                job.platform, job.creator, should_stop=cancelled, kind=job.kind))
            if job.platform in ("redgifs", "twitter"):
                # Both stream: RedGifs because pre-walking a 5000-gif creator at
                # 1 req/s is minutes of dead time, X because it throttles deep
                # paging and reports no total at all (so the bar runs
                # indeterminate - see total_estimate).
                self._set(job, total=self._streaming_total(job))
                batch = []
                for desc in source:
                    batch.append(desc)
                    if len(batch) >= self.concurrency * 4:
                        self._set(job, phase="downloading")
                        self._download_batch(job, batch, manifest, directory, session,
                                             redgifs, max_len, cancelled)
                        batch = []
                        if cancelled():
                            break
                if batch and not cancelled():
                    self._set(job, phase="downloading")
                    self._download_batch(job, batch, manifest, directory, session,
                                         redgifs, max_len, cancelled)
            else:
                # Reddit's listing is cheap and capped, so walk it all up front.
                descs = []
                for desc in source:
                    descs.append(desc)
                    self._set(job, total=len(descs))
                self._set(job, phase="downloading", total=len(descs))
                self._download_batch(job, descs, manifest, directory, session, redgifs,
                                     max_len, cancelled)

        manifest.flush(force=True)

    @staticmethod
    def _apply_only(job, manifest, descs):
        """Drop already-downloaded items when the job was started with the
        "only missing" filter on.

        _download_batch would skip these anyway, so this changes no files — but
        it keeps them out of `total`, so the progress bar measures the work the
        user actually asked for instead of counting a few thousand instant skips.
        """
        if job.only != "missing":
            return descs

        def generate():
            for desc in descs:
                if not manifest.has_post(desc["id"]):
                    yield desc
        return generate()

    @staticmethod
    def _streaming_total(job):
        """Up-front total for the platforms that download while enumerating.

        total_estimate counts a creator's whole catalogue, so it is only right
        when the job is taking all of it. Under a filter it would overshoot and
        pin the bar short of 100% forever; an indeterminate bar is the honest
        answer, the same call twitter's estimate already makes.
        """
        if job.kind != "all" or job.only != "all":
            return None
        return creators.total_estimate(job.platform, job.creator)

    def _download_batch(self, job, descs, manifest, directory, session, redgifs,
                        max_len, cancelled):
        """Download a batch of items, `concurrency` at a time.

        Filenames are resolved here, serially, before anything is handed to the
        pool - see the invariants in the module docstring.
        """
        if not descs:
            return

        planned = []
        for desc in descs:
            if manifest.has_post(desc["id"]):
                with self._lock:
                    job.skipped += 1
                    job.completed += 1
                continue
            filenames = reddit_api.plan_filenames(desc, manifest.owned, max_len=max_len)
            for name in filenames:
                manifest.claim(name)
            planned.append((desc, filenames))

        if not planned:
            return

        executor = ThreadPoolExecutor(max_workers=self.concurrency)
        futures = {}
        try:
            for desc, filenames in planned:
                futures[executor.submit(
                    self._download_one, job, desc, filenames, directory, session,
                    redgifs, cancelled)] = (desc, filenames)

            # as_completed, not submission order: one slow item must not stall
            # progress reporting for everything queued behind it.
            for future in as_completed(list(futures)):
                desc, filenames = futures[future]
                try:
                    result = future.result()
                except OSError as e:
                    # Out of space or permissions: stop the whole job rather than
                    # failing every remaining item the same way.
                    self._set(job, error="Filesystem error: %s" % e, state=FAILED)
                    job.cancel_requested = True
                    logger.error("Job %s aborted: %s", job.id, e)
                    break
                except Exception as e:  # noqa: BLE001
                    self._note_error(job, desc["id"], desc.get("title"), str(e))
                    with self._lock:
                        job.completed += 1
                    continue

                status = result["status"]
                with self._lock:
                    job.completed += 1

                if status == net.DOWNLOADED:
                    with self._lock:
                        job.downloaded += 1
                    manifest.record(desc["id"], filenames)
                elif status == net.SKIPPED:
                    with self._lock:
                        job.skipped += 1
                    manifest.record(desc["id"], filenames)
                elif status == net.GONE:
                    with self._lock:
                        job.gone += 1
                    manifest.mark_gone(desc["id"])
                elif status == net.CANCELLED:
                    pass
                else:
                    self._note_error(job, desc["id"], desc.get("title"),
                                     result.get("error") or "download failed")

                manifest.flush(every=10)
        finally:
            if cancelled():
                executor.shutdown(wait=False, cancel_futures=True)
            else:
                executor.shutdown(wait=True)
            manifest.flush(force=True)

    def _download_one(self, job, desc, filenames, directory, session, redgifs, cancelled):
        if cancelled():
            return {"status": net.CANCELLED, "error": None}
        # Runs on a pool thread. `current` is approximate with concurrency > 1;
        # it exists to show the user that something is happening.
        if filenames:
            self._set(job, current=filenames[0])
        return reddit_api.download_planned(
            desc, filenames, directory, session, redgifs=redgifs,
            should_cancel=cancelled)


_manager = None
_manager_lock = threading.Lock()


def get_manager():
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = JobManager()
            _manager.start()
        return _manager
