"""Job handler and periodic function registry (CT-7).

Each domain module registers its own handlers at import time (``register(kind, handler)``);
the worker looks them up by ``Job.kind``. Handlers receive the job payload, which holds
identifiers only, and must not commit: the worker commits the handler's writes together with
the ``done`` status.
"""

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.jobs.queue import KIND_MAX_LENGTH

JobHandler = Callable[[Session, dict[str, str]], None]
PeriodicFn = Callable[[Session], None]

PERIODIC_NAME_MAX_LENGTH = 64


class RetryableJobError(Exception):
    """Raised by a handler for a transient failure; the job is retried with backoff."""


@dataclass(frozen=True)
class PeriodicTask:
    name: str
    every_seconds: int
    fn: PeriodicFn


def _validate_name(label: str, value: str, max_length: int) -> None:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ValueError(f"{label} must be a string with 1 to {max_length} characters")


class JobRegistry:
    """Maps job kinds to handlers and holds the periodic functions."""

    def __init__(self) -> None:
        self._handlers: dict[str, JobHandler] = {}
        self._periodic: dict[str, PeriodicTask] = {}

    def register(self, kind: str, handler: JobHandler) -> None:
        _validate_name("job kind", kind, KIND_MAX_LENGTH)
        if not callable(handler):
            raise TypeError("job handler must be callable")
        current = self._handlers.get(kind)
        if current is not None and current is not handler:
            raise ValueError(f"a handler is already registered for job kind {kind!r}")
        self._handlers[kind] = handler

    def register_periodic(self, name: str, every_seconds: int, fn: PeriodicFn) -> None:
        _validate_name("periodic name", name, PERIODIC_NAME_MAX_LENGTH)
        if isinstance(every_seconds, bool) or not isinstance(every_seconds, int):
            raise TypeError("every_seconds must be an integer")
        if every_seconds < 0:
            raise ValueError("every_seconds must not be negative")
        if not callable(fn):
            raise TypeError("periodic function must be callable")
        current = self._periodic.get(name)
        if current is not None and current.fn is not fn:
            raise ValueError(f"a periodic function is already registered as {name!r}")
        self._periodic[name] = PeriodicTask(name=name, every_seconds=every_seconds, fn=fn)

    def unregister(self, kind: str) -> None:
        self._handlers.pop(kind, None)

    def unregister_periodic(self, name: str) -> None:
        self._periodic.pop(name, None)

    def handler_for(self, kind: str) -> JobHandler | None:
        return self._handlers.get(kind)

    def periodic_tasks(self) -> list[PeriodicTask]:
        return list(self._periodic.values())


default_registry = JobRegistry()


def register(kind: str, handler: JobHandler) -> None:
    """Register the handler for ``kind`` in the default registry used by the worker."""
    default_registry.register(kind, handler)


def register_periodic(name: str, every_seconds: int, fn: PeriodicFn) -> None:
    """Register ``fn`` to run every ``every_seconds`` in the default registry."""
    default_registry.register_periodic(name, every_seconds, fn)


def _register_domain_handlers() -> None:
    """Register the domain job handlers in the default registry used by the worker."""
    # Imported here: the domain modules import the job queue, which this module also uses.
    from app.evaluation.pipeline import EVALUATE_JOB_KIND, run_evaluation
    from app.interviews.expiration import (
        EXPIRE_PERIODIC_NAME,
        EXPIRE_PERIODIC_SECONDS,
        run_session_expiry,
    )
    from app.interviews.question_generation import PREPARE_QUESTIONS_JOB, prepare_questions
    from app.resumes.processing import RESUME_PROCESS_JOB, process_resume

    register(RESUME_PROCESS_JOB, process_resume)
    register(PREPARE_QUESTIONS_JOB, prepare_questions)
    register(EVALUATE_JOB_KIND, run_evaluation)
    register_periodic(EXPIRE_PERIODIC_NAME, EXPIRE_PERIODIC_SECONDS, run_session_expiry)


_register_domain_handlers()
