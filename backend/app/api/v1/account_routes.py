"""Account deletion (ADR 0012): the user's own, confirmed, irreversible request."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.errors import ApiError
from app.auth import rate_limit
from app.auth.cookies import clear_session_cookies
from app.auth.dependencies import (
    client_ip,
    current_user,
    enforce_limit,
    get_app_settings,
    unavailable,
)
from app.auth.gotrue import AuthServiceError, AuthUnavailableError, SupabaseAuthClient
from app.auth.tokens import VerifiedUser
from app.config import Settings
from app.schemas.auth import AccountDeletionRequest
from app.schemas.errors import ErrorResponse
from app.services.account import delete_account

router = APIRouter(prefix="/account", tags=["account"])
_ERRORS = {code: {"model": ErrorResponse} for code in (401, 403, 409, 422, 429, 503)}


@router.post("/deletion", status_code=status.HTTP_204_NO_CONTENT, responses=_ERRORS)
def delete_my_account(
    body: AccountDeletionRequest,
    request: Request,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> Response:
    """Delete the signed-in account and all of its data after re-checking the password."""
    enforce_limit(settings, rate_limit.ACCOUNT_DELETE_USER, str(user.id))
    enforce_limit(settings, rate_limit.LOGIN_IP, client_ip(request))
    if not user.email:
        raise ApiError(403, "password_incorrect", "Your password is incorrect.")
    try:
        client = SupabaseAuthClient(settings)
        check = client.sign_in(user.email, body.password)
        if check.user_id != user.id:
            raise ApiError(403, "password_incorrect", "Your password is incorrect.")
        client.sign_out(check.access_token)  # the check's own session is not needed
    except AuthUnavailableError as exc:
        raise unavailable("Account deletion") from exc
    except AuthServiceError as exc:
        raise ApiError(403, "password_incorrect", "Your password is incorrect.") from exc

    delete_account(settings, user)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookies(response, settings)
    return response
