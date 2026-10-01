"""Definitive deletion of an interview session (CT-52; DATA-05, DATA-91).

In the caller's transaction, ``delete_session``:

1. locks the owned session (``SELECT ... FOR UPDATE`` through ``get_owned_session``; 404
   ``RESOURCE_NOT_FOUND`` for unknown ids and sessions of other users alike);
2. marks the session's pending (``queued``) jobs ``done``, so they never run for a deleted
   session. A job already ``running`` is left to the worker: its handler re-reads the session
   under lock, finds nothing and writes nothing (DATA-91);
3. deletes the session row. Messages, questions, answers, evaluations and the report go with it
   through the foreign keys (``ON DELETE CASCADE``); the snapshot is a column of the row. No copy
   is kept anywhere ("soft delete" is not used).

A session may be deleted in any status, including ``evaluating``: the evaluation pipeline locks
the session row before writing and discards its result when the row is gone (DATA-91).

Lock order: the session row, then its job rows by id. The worker takes the same order (the
handler locks the session, then the job is locked to record its outcome), and the queue only
ever locks job rows on their own, so no lock cycle exists. Users and resumes are not locked.

Like the other service modules, this one only flushes (CT-2); nothing is deleted until the
caller commits, and a rollback keeps everything. No file is stored per session, so there is
nothing to remove outside the database. Logs carry ids and counts only.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.interviews.sessions import get_owned_session
from app.jobs.queue import mark_done
from app.models.account import User
from app.models.job import Job, JobStatus
from app.observability import log_event

__all__ = ["delete_session"]

SESSION_PAYLOAD_KEY = "session_id"


def _close_pending_jobs(db: Session, session_id: UUID) -> int:
    """Mark the queued jobs of the session ``done``; return how many were closed.

    ``FOR UPDATE`` without ``SKIP LOCKED``: a job being claimed right now is waited for and
    re-checked, so it is either closed here or already ``running`` (and left to the worker).
    """
    statement = (
        select(Job)
        .where(
            Job.status == JobStatus.QUEUED,
            Job.payload[SESSION_PAYLOAD_KEY].astext == str(session_id),
        )
        .order_by(Job.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    jobs = list(db.execute(statement).scalars().all())
    for job in jobs:
        mark_done(db, job)
    return len(jobs)


def delete_session(db: Session, user: User, session_id: UUID) -> None:
    """Delete a session owned by ``user`` and everything that depends on it (CT-52).

    Raises ``AppError`` ``RESOURCE_NOT_FOUND`` (404) for unknown or malformed ids and for
    sessions of other users; nothing is changed then. The caller commits.
    """
    session = get_owned_session(db, user, session_id, for_update=True)
    deleted_id = session.id

    jobs_closed = _close_pending_jobs(db, deleted_id)
    db.delete(session)
    db.flush()
    log_event("session.deleted", session_id=str(deleted_id), jobs_closed=jobs_closed)
