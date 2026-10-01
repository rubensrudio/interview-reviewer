"""Account routes (plan section 8.1, "Google e conta"; AUTH-10, AUTH-15).

``POST /api/account/terms`` records the acceptance of the current terms of use and privacy
policy. It is reachable before that acceptance (``CurrentUserPendingTerms``) and, as a
non-GET authenticated route, requires the CSRF header (CT-11). Bodies follow the auth limits
of LAC-32.

``DELETE /api/account`` deletes the account and all of its data at once (CT-53, DATA-06),
clears the session cookies and answers with the 30-day backup notice (LAC-12). It is reachable
before a current terms acceptance (LAC-45), still with the session cookie and CSRF header.
"""

from fastapi import APIRouter, Response
from pydantic import BaseModel, StrictBool

from app.api.auth_local import BoundedBodyRoute
from app.api.deps import CurrentUserPendingTerms, DbSession
from app.errors import TERMS_NOT_ACCEPTED, AppError
from app.legal.consent import record_consent
from app.privacy.account_deletion import ACCOUNT_DELETED_MESSAGE, delete_account

router = APIRouter(prefix="/api/account", tags=["account"], route_class=BoundedBodyRoute)


class AcceptTermsRequest(BaseModel):
    # Missing means "not accepted"; strict so "true" or 1 is never read as consent.
    accept: StrictBool = False


class AccountDeletedResponse(BaseModel):
    message: str


@router.post("/terms", status_code=204, response_class=Response)
def accept_terms(body: AcceptTermsRequest, user: CurrentUserPendingTerms, db: DbSession) -> None:
    if body.accept is not True:
        raise AppError.from_catalog(TERMS_NOT_ACCEPTED)
    record_consent(db, user)
    db.commit()


@router.delete("")
def delete_own_account(
    user: CurrentUserPendingTerms, db: DbSession, response: Response
) -> AccountDeletedResponse:
    # LAC-45=A: reachable with pending terms; the session and the CSRF header stay required.
    # delete_account commits its own steps (deletion request first, then the purge).
    delete_account(db, user, response)
    return AccountDeletedResponse(message=ACCOUNT_DELETED_MESSAGE)


__all__ = ["router"]
