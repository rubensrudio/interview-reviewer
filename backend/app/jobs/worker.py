"""Background worker: runs due periodic functions and consumes the job queue (DA-4, CT-7).

Run with ``python -m app.jobs.worker``. Each job goes through three short transactions:

1. claim (``claim_next`` + commit), so the ``running`` status is visible and the row lock is
   released before the work starts;
2. the handler plus ``mark_done`` in one transaction, so the handler's writes and the ``done``
   status commit together;
3. on error, a fresh transaction that records the failure (``mark_failed``).

A handler that raises ``RetryableJobError`` is retried with backoff; any other exception
fails the job with the exception class name as ``error_code`` (never the message).

Stale-job reaper (LAC-38): at startup and then every ``reap_every_seconds``, jobs left
``running`` with ``locked_at`` older than ``stale_after`` (their worker died) go back to
``queued``, or to ``failed`` when they already used ``max_attempts``. Before recording the
outcome of a job, the worker checks that it still owns it (same ``attempts`` and still
``running``), so a late finish never overwrites a job that was reaped and claimed again.

Lock heartbeat (LAC-44): while a handler runs, a background thread renews the job's
``locked_at`` every ``heartbeat_every_seconds`` (well below ``stale_after``) in a database
session of its own, so the handler's transaction is never committed by it. The renewal is
fenced by ``attempts``: if it matches no row, this execution lost the job, the heartbeat
logs ``job.lock_lost`` and stops; the handler is not interrupted, and its result is discarded
by the ownership check (``job.ownership_lost``). The heartbeat is always stopped and joined
when the handler returns or raises. A dead worker sends no heartbeat, so the reaper still
recovers its jobs.
"""

import importlib
import signal
import threading
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from types import FrameType
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_sessionmaker
from app.jobs.queue import (
    ERROR_CODE_MAX_LENGTH,
    claim_next,
    mark_done,
    mark_failed,
    renew_lock,
)
from app.jobs.registry import (
    JobHandler,
    JobRegistry,
    PeriodicFn,
    PeriodicTask,
    RetryableJobError,
    default_registry,
    register,
    register_periodic,
)
from app.llm.model_version import ModelNotApproved, assert_model_release_allowed
from app.logging_setup import configure_logging
from app.models.job import Job, JobStatus
from app.observability import log_event, timed

__all__ = [
    "JobHandler",
    "PeriodicFn",
    "RetryableJobError",
    "Worker",
    "main",
    "reap_stale_jobs",
    "register",
    "register_periodic",
]

# Local constants: no Settings entry exists for them yet (config.py is outside TASK-009).
STALE_LOCK_TIMEOUT = timedelta(minutes=30)
REAPER_INTERVAL_SECONDS = 60
# LAC-44: renew the lock of a running job far more often than STALE_LOCK_TIMEOUT.
HEARTBEAT_INTERVAL_SECONDS = 60.0
HEARTBEAT_JOIN_TIMEOUT_SECONDS = 30.0
HEARTBEAT_THREAD_PREFIX = "job-heartbeat"
POLL_INTERVAL_SECONDS = 1.0
UNKNOWN_KIND_ERROR = "UnknownJobKind"
STALE_LOCK_ERROR = "StaleJobLock"
PRODUCTION = "production"
RELEASE_REFUSED_EXIT_CODE = 1

# Modules whose import registers domain handlers and periodic functions. Each owning task
# adds its module here; nothing domain-specific is registered by the worker itself.
HANDLER_MODULES: tuple[str, ...] = ()

SessionFactory = Callable[[], Session]


def _error_code(exc: BaseException) -> str:
    return type(exc).__name__[:ERROR_CODE_MAX_LENGTH]


def reap_stale_jobs(db: Session, stale_after: timedelta = STALE_LOCK_TIMEOUT) -> int:
    """Recover ``running`` jobs whose lock is older than ``stale_after``. The caller commits.

    Rows are locked in id order and locked rows are skipped, so concurrent reapers never
    block each other or a worker that is finishing a job.
    """
    statement = (
        select(Job)
        .where(
            Job.status == JobStatus.RUNNING,
            Job.locked_at < func.clock_timestamp() - stale_after,
        )
        .order_by(Job.id)
        .with_for_update(skip_locked=True)
    )
    jobs = list(db.execute(statement).scalars())
    for job in jobs:
        job.locked_at = None
        job.last_error_code = STALE_LOCK_ERROR
        if job.attempts >= job.max_attempts:
            job.status = JobStatus.FAILED
            event = "job.failed"
        else:
            job.status = JobStatus.QUEUED
            job.run_after = func.clock_timestamp()
            event = "job.reaped"
        log_event(
            event,
            job_id=str(job.id),
            kind=job.kind,
            attempts=job.attempts,
            error_code=STALE_LOCK_ERROR,
        )
    db.flush()
    return len(jobs)


def _lock_if_owned(db: Session, job_id: uuid.UUID, attempts: int) -> Job | None:
    """Lock the job row and return it only if this worker's claim is still current."""
    job = db.execute(select(Job).where(Job.id == job_id).with_for_update()).scalar_one_or_none()
    if job is None or job.status is not JobStatus.RUNNING or job.attempts != attempts:
        return None
    return job


class _LockHeartbeat:
    """Background thread that renews one job's lock until stopped or the lock is lost."""

    def __init__(
        self,
        session_factory: SessionFactory,
        job_id: uuid.UUID,
        kind: str,
        attempts: int,
        every_seconds: float,
    ) -> None:
        self._session_factory = session_factory
        self._job_id = job_id
        self._kind = kind
        self._attempts = attempts
        self._every_seconds = every_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"{HEARTBEAT_THREAD_PREFIX}-{job_id}", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(HEARTBEAT_JOIN_TIMEOUT_SECONDS)
        if self._thread.is_alive():
            log_event("job.heartbeat_stuck", job_id=str(self._job_id), kind=self._kind)

    def _run(self) -> None:
        while not self._stop.wait(self._every_seconds):
            try:
                renewed = self._renew()
            except Exception as exc:  # database hiccup: try again on the next beat
                log_event(
                    "job.heartbeat_failed",
                    job_id=str(self._job_id),
                    kind=self._kind,
                    error_code=_error_code(exc),
                )
                continue
            if not renewed:
                log_event(
                    "job.lock_lost",
                    job_id=str(self._job_id),
                    kind=self._kind,
                    attempts=self._attempts,
                )
                return

    def _renew(self) -> bool:
        with self._session_factory() as db:
            try:
                renewed = renew_lock(db, self._job_id, self._attempts)
                db.commit()
            except Exception:
                db.rollback()
                raise
        return renewed


class Worker:
    """One worker loop. Several workers (threads or processes) may run side by side."""

    def __init__(
        self,
        registry: JobRegistry = default_registry,
        session_factory: SessionFactory | None = None,
        stale_after: timedelta | None = None,
        reap_every_seconds: int | None = REAPER_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        heartbeat_every_seconds: float | None = None,
    ) -> None:
        # Defaults are read at construction so the module constants can be patched in tests.
        stale = STALE_LOCK_TIMEOUT if stale_after is None else stale_after
        heartbeat = (
            HEARTBEAT_INTERVAL_SECONDS
            if heartbeat_every_seconds is None
            else heartbeat_every_seconds
        )
        if heartbeat <= 0 or heartbeat >= stale.total_seconds():
            raise ValueError("heartbeat interval must be positive and shorter than stale_after")
        self._registry = registry
        self._session_factory = session_factory
        self._stale_after = stale
        self._heartbeat_every_seconds = heartbeat
        self._reap_every_seconds = reap_every_seconds
        self._clock = clock
        self._last_reap: float | None = None
        self._last_periodic_run: dict[str, float] = {}

    def _new_session(self) -> Session:
        factory = self._session_factory or get_sessionmaker()
        return factory()

    def run_once(self) -> bool:
        """Reap stale jobs and run periodics when due, then process at most one job.

        Returns True when a job was claimed.
        """
        self._reap_if_due()
        self._run_due_periodics()
        return self._process_next_job()

    def run_forever(
        self, stop: threading.Event, poll_interval: float = POLL_INTERVAL_SECONDS
    ) -> None:
        log_event("worker.started")
        while not stop.is_set():
            try:
                worked = self.run_once()
            except Exception as exc:  # database down, etc.: keep the loop alive
                log_event("worker.error", error_code=_error_code(exc))
                worked = False
            if not worked:
                stop.wait(poll_interval)
        log_event("worker.stopped")

    def _is_due(self, last_run: float | None, every_seconds: int) -> bool:
        return last_run is None or self._clock() - last_run >= every_seconds

    def _reap_if_due(self) -> None:
        if self._reap_every_seconds is None:
            return
        if not self._is_due(self._last_reap, self._reap_every_seconds):
            return
        self._last_reap = self._clock()
        with self._new_session() as db:
            try:
                reaped = reap_stale_jobs(db, self._stale_after)
                db.commit()
            except Exception:
                db.rollback()
                raise
        if reaped:
            log_event("worker.reaped", count=reaped)

    def _run_due_periodics(self) -> None:
        for task in self._registry.periodic_tasks():
            if self._is_due(self._last_periodic_run.get(task.name), task.every_seconds):
                self._last_periodic_run[task.name] = self._clock()
                self._run_periodic(task)

    def _run_periodic(self, task: PeriodicTask) -> None:
        with self._new_session() as db:
            try:
                with timed("periodic.run", name=task.name):
                    task.fn(db)
                db.commit()
            except Exception as exc:
                db.rollback()
                log_event("periodic.failed", name=task.name, error_code=_error_code(exc))

    def _process_next_job(self) -> bool:
        with self._new_session() as db:
            job = claim_next(db)
            if job is None:
                db.rollback()
                return False
            job_id, kind, attempts = job.id, job.kind, job.attempts
            payload = cast(dict[str, str], dict(job.payload))
            db.commit()

        handler = self._registry.handler_for(kind)
        if handler is None:
            self._record_failure(job_id, kind, attempts, UNKNOWN_KIND_ERROR, retry=False)
            return True

        log_event("job.started", job_id=str(job_id), kind=kind, attempts=attempts)
        with self._new_session() as db:
            try:
                self._run_handler_with_heartbeat(handler, db, payload, job_id, kind, attempts)
                owned = _lock_if_owned(db, job_id, attempts)
                if owned is None:
                    db.rollback()
                    log_event("job.ownership_lost", job_id=str(job_id), kind=kind)
                    return True
                mark_done(db, owned)
                db.commit()
                log_event("job.finished", job_id=str(job_id), kind=kind, attempts=attempts)
                return True
            except Exception as exc:
                db.rollback()
                error = exc

        retry = isinstance(error, RetryableJobError)
        self._record_failure(job_id, kind, attempts, _error_code(error), retry=retry)
        return True

    def _run_handler_with_heartbeat(
        self,
        handler: JobHandler,
        db: Session,
        payload: dict[str, str],
        job_id: uuid.UUID,
        kind: str,
        attempts: int,
    ) -> None:
        # Stopped before the ownership check, so a late beat never waits on (or races) the
        # row lock taken to record the outcome.
        heartbeat = _LockHeartbeat(
            self._new_session, job_id, kind, attempts, self._heartbeat_every_seconds
        )
        heartbeat.start()
        try:
            with timed("job.handler", job_id=str(job_id), kind=kind, attempts=attempts):
                handler(db, payload)
        finally:
            heartbeat.stop()

    def _record_failure(
        self, job_id: uuid.UUID, kind: str, attempts: int, error_code: str, retry: bool
    ) -> None:
        with self._new_session() as db:
            try:
                owned = _lock_if_owned(db, job_id, attempts)
                if owned is None:
                    db.rollback()
                    log_event("job.ownership_lost", job_id=str(job_id), kind=kind)
                    return
                mark_failed(db, owned, error_code, retry=retry)
                db.commit()
            except Exception:
                db.rollback()
                # The job stays running; the reaper recovers it after STALE_LOCK_TIMEOUT.
                raise


def _load_handler_modules(modules: tuple[str, ...] = HANDLER_MODULES) -> None:
    for module in modules:
        importlib.import_module(module)


def main() -> None:
    configure_logging()
    # MODEL-03: like create_app, refuse to start in production without an approved model
    # version, before any job is claimed. The gate itself logs the refusal reason.
    settings = get_settings()
    if settings.app_env == PRODUCTION:
        try:
            assert_model_release_allowed(settings)
        except ModelNotApproved:
            log_event("worker.release_refused")
            raise SystemExit(RELEASE_REFUSED_EXIT_CODE) from None
    _load_handler_modules()
    stop = threading.Event()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    Worker().run_forever(stop)


if __name__ == "__main__":
    main()
