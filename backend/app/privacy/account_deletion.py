"""Definitive deletion of an account and all of its data (CT-53; DATA-06, DATA-07, DATA-93).

``delete_account`` runs in three steps:

1. In its own transaction (committed here): lock the ``users`` row, set
   ``deletion_requested_at``, revoke every authenticated session (CT-11) and enqueue an
   ``account.purge`` job (CT-6). From this commit on the account cannot be used: login, Google
   sign-in, password reset and every authenticated route reject a user with a pending deletion.
   The session cookies of the response are cleared.
2. Delete the stored files of the user (``delete_user_files``, CT-23).
3. Delete the ``users`` row. Resumes, extractions, sessions, messages, answers, evaluations,
   reports, auth sessions and one-time tokens go with it through ``ON DELETE CASCADE``; the
   Google link is a column of the row. The queued ``account.purge`` jobs of the user and the
   ``login_throttles`` row of the account (``account:<HMAC of the e-mail>``, LAC-46) are deleted
   in the same transaction, so no row keeps the user id and a new sign-up with the same e-mail
   never inherits a lock. Origin rows (``ip:...``) are not tied to the account and stay.

Steps 2-3 run inline under the ``users`` row lock and commit together. If they fail (e.g. the
storage is unavailable) they are rolled back as a whole, the account stays unusable with all of
its data, and the ``account.purge`` job (``purge_account``) repeats them until they succeed:
a failed attempt enqueues the next one (``PURGE_RETRY_DELAY``) in the same transaction that
closes the current job, so the deletion is never abandoned after a fixed number of attempts
(DATA-93). Every step is idempotent: an unknown user or one already purged is a no-op, and an
account without a deletion request is never purged.

Lock order is fixed: ``users`` row, then the ``login_throttles`` row, then ``jobs`` rows (login
locks throttle rows but never the ``users`` row, so no lock cycle exists). Resume uploads lock
the same ``users`` row before writing a file, so no file is written between steps 2 and 3. The
inline job has a short grace period (``INLINE_PURGE_GRACE``) so the worker normally finds the
work already done; if it runs concurrently it waits on the ``users`` lock and then finds nothing
to do.

Logs carry no personal data (no e-mail, no user id) and are emitted only after the commit.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import Response
from sqlalchemy import Select, delete, event, select
from sqlalchemy.orm import Session, SessionTransaction

from app.auth.sessions import COOKIE_PATH, SESSION_COOKIE, XSRF_COOKIE, revoke_all_sessions
from app.auth.throttle import account_key
from app.config import get_settings
from app.jobs.queue import enqueue
from app.models.account import LoginThrottle, User
from app.models.job import Job, JobStatus
from app.observability import log_event
from app.resumes import storage

__all__ = [
    "ACCOUNT_DELETED_MESSAGE",
    "ACCOUNT_PURGE_JOB",
    "delete_account",
    "purge_account",
]

ACCOUNT_PURGE_JOB = "account.purge"
USER_PAYLOAD_KEY = "user_id"
ACCOUNT_DELETED_MESSAGE = "Your account and data were deleted. Backup copies expire within 30 days."

# Delay before the worker may pick up the job enqueued by step 1: the request normally
# finishes steps 2-3 inline first.
INLINE_PURGE_GRACE = timedelta(seconds=60)
# Delay between purge attempts after a failure (e.g. storage unavailable).
PURGE_RETRY_DELAY = timedelta(minutes=5)

_PENDING_LOGS_KEY = "privacy.pending_account_logs"


@dataclass(frozen=True)
class _PendingLog:
    transaction: SessionTransaction | None
    event: str
    fields: dict[str, str]


# --- logging after commit ------------------------------------------------------------------


def _pending_logs(db: Session) -> list[_PendingLog]:
    pending: list[_PendingLog] = db.info.setdefault(_PENDING_LOGS_KEY, [])
    return pending


def _emit_pending_logs(db: Session) -> None:
    # Fired only when the outermost transaction commits.
    pending = _pending_logs(db)
    committed = list(pending)
    pending.clear()
    for item in committed:
        log_event(item.event, **item.fields)


def _drop_pending_logs(db: Session, transaction: SessionTransaction) -> None:
    # A root transaction that ended without committing drops its pending logs.
    pending = _pending_logs(db)
    pending[:] = [item for item in pending if item.transaction is not transaction]


def _log_after_commit(db: Session, name: str, **fields: str) -> None:
    """Emit ``name`` once the current root transaction commits; drop it otherwise.

    Must be called outside any savepoint, so a rolled-back savepoint never logs.
    """
    if not event.contains(db, "after_commit", _emit_pending_logs):
        event.listen(db, "after_commit", _emit_pending_logs)
        event.listen(db, "after_transaction_end", _drop_pending_logs)
    root = db.get_transaction()
    _pending_logs(db).append(_PendingLog(transaction=root, event=name, fields=fields))


# --- steps ---------------------------------------------------------------------------------


def _clear_cookies(response: Response) -> None:
    secure = get_settings().cookie_secure
    response.delete_cookie(
        SESSION_COOKIE, path=COOKIE_PATH, secure=secure, httponly=True, samesite="lax"
    )
    response.delete_cookie(
        XSRF_COOKIE, path=COOKIE_PATH, secure=secure, httponly=False, samesite="lax"
    )


def _lock_user(db: Session, user_id: UUID) -> User | None:
    statement = (
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return db.execute(statement).scalar_one_or_none()


def _purge_jobs_statement(user_id: UUID) -> Select[Job]:
    return select(Job).where(
        Job.kind == ACCOUNT_PURGE_JOB,
        Job.payload[USER_PAYLOAD_KEY].astext == str(user_id),
    )


def _has_queued_purge(db: Session, user_id: UUID) -> bool:
    statement = _purge_jobs_statement(user_id).where(Job.status == JobStatus.QUEUED).limit(1)
    return db.execute(statement).scalar_one_or_none() is not None


def _enqueue_purge(db: Session, user_id: UUID, delay: timedelta) -> None:
    """Enqueue the next purge attempt unless one is already pending."""
    if _has_queued_purge(db, user_id):
        return
    enqueue(
        db,
        ACCOUNT_PURGE_JOB,
        {USER_PAYLOAD_KEY: str(user_id)},
        run_after=datetime.now(UTC) + delay,
    )


def _delete_finished_purge_jobs(db: Session, user_id: UUID) -> None:
    """Delete the user's purge jobs that no worker is running (queued, done or failed).

    A ``running`` job belongs to a worker: its handler waits on the ``users`` lock, then finds
    nothing to purge and the worker marks it ``done``.
    """
    statement = (
        _purge_jobs_statement(user_id)
        .where(Job.status != JobStatus.RUNNING)
        .order_by(Job.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    job_ids = [job.id for job in db.execute(statement).scalars().all()]
    if job_ids:
        db.execute(
            delete(Job).where(Job.id.in_(job_ids)).execution_options(synchronize_session="fetch")
        )


def _purge(db: Session, user_id: UUID) -> bool:
    """Steps 2-3 in the current transaction: files, then the ``users`` row (cascade).

    Returns ``False`` (and changes nothing) when the user no longer exists or has no
    deletion request. The caller commits.
    """
    user = _lock_user(db, user_id)
    if user is None or user.deletion_requested_at is None:
        return False
    throttle_key = account_key(user.email_normalized)
    storage.delete_user_files(user_id)
    db.delete(user)
    db.flush()
    db.execute(delete(LoginThrottle).where(LoginThrottle.key == throttle_key))
    _delete_finished_purge_jobs(db, user_id)
    db.flush()
    return True


def _parse_user_id(payload: dict[str, str]) -> UUID:
    raw = payload.get(USER_PAYLOAD_KEY) if isinstance(payload, dict) else None
    if not isinstance(raw, str):
        raise ValueError("account.purge payload must hold a user_id")
    try:
        return UUID(raw)
    except ValueError as error:
        raise ValueError("account.purge payload holds a malformed user_id") from error


# --- public API ----------------------------------------------------------------------------


def delete_account(db: Session, user: User, response: Response) -> None:
    """Delete ``user`` and all of their data (CT-53); clear the session cookies.

    Commits step 1 (deletion request, revoked sessions, queued ``account.purge`` job) and then
    steps 2-3. A failure in steps 2-3 is not raised: the account is already unusable and the
    job completes the deletion (DATA-93). A failure in step 1 is raised and changes nothing.
    """
    user_id = user.id
    locked = _lock_user(db, user_id)
    if locked is None:
        # Already purged by a concurrent request.
        db.rollback()
        _clear_cookies(response)
        return
    if locked.deletion_requested_at is None:
        locked.deletion_requested_at = datetime.now(UTC)
    revoke_all_sessions(db, user_id)
    _enqueue_purge(db, user_id, INLINE_PURGE_GRACE)
    db.commit()
    _clear_cookies(response)
    log_event("account.deletion_requested")

    try:
        purged = _purge(db, user_id)
        db.commit()
    except Exception as error:  # noqa: BLE001 - the account.purge job retries the deletion
        db.rollback()
        log_event("account.purge_deferred", source="request", error_code=type(error).__name__)
        return
    if purged:
        log_event("account.purged", source="request")


def purge_account(db: Session, payload: dict[str, str]) -> None:
    """``account.purge`` job handler (CT-53, CT-7): finish an interrupted account deletion.

    Idempotent. Raises ``ValueError`` for a malformed payload (the job fails without retry).
    When the purge fails, its writes are rolled back and the next attempt is enqueued, so the
    current job still ends ``done``; if even that cannot be written, ``RetryableJobError`` is
    raised and the worker retries the job.
    """
    user_id = _parse_user_id(payload)
    try:
        with db.begin_nested():
            purged = _purge(db, user_id)
    except Exception as error:  # noqa: BLE001 - rescheduled below, never abandoned
        error_code = type(error).__name__
        try:
            _enqueue_purge(db, user_id, PURGE_RETRY_DELAY)
        except Exception as enqueue_error:
            # Imported here: the registry imports this module to register the handler.
            from app.jobs.registry import RetryableJobError

            raise RetryableJobError("account purge could not be rescheduled") from enqueue_error
        _log_after_commit(db, "account.purge_deferred", source="job", error_code=error_code)
        return
    if purged:
        _log_after_commit(db, "account.purged", source="job")
    else:
        _log_after_commit(db, "account.purge_skipped")
