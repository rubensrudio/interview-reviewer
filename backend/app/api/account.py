"""Account routes (plan section 8.1, "Google e conta"; AUTH-10, AUTH-15).

``POST /api/account/terms`` records the acceptance of the current terms of use and privacy
policy. It is reachable before that acceptance (``CurrentUserPendingTerms``) and, as a
non-GET authenticated route, requires the CSRF header (CT-11). Bodies follow the auth limits
of LAC-32.
"""

from fastapi import APIRouter, Response
from pydantic import BaseModel, StrictBool

from app.api.auth_local import BoundedBodyRoute
from app.api.deps import CurrentUserPendingTerms, DbSession
from app.errors import TERMS_NOT_ACCEPTED, AppError
from app.legal.consent import record_consent

router = APIRouter(prefix="/api/account", tags=["account"], route_class=BoundedBodyRoute)


class AcceptTermsRequest(BaseModel):
    # Missing means "not accepted"; strict so "true" or 1 is never read as consent.
    accept: StrictBool = False


@router.post("/terms", status_code=204, response_class=Response)
def accept_terms(body: AcceptTermsRequest, user: CurrentUserPendingTerms, db: DbSession) -> None:
    if body.accept is not True:
        raise AppError.from_catalog(TERMS_NOT_ACCEPTED)
    record_consent(db, user)
    db.commit()


__all__ = ["router"]
