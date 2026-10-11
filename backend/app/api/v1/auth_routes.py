"""Account routes: the only endpoints usable without a session.

These public routes are rate-limited per IP and per email (shared counters in
PostgreSQL), CSRF-protected like every other state-changing route, and return
deliberately generic messages so they do not reveal which emails have accounts.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.dialects.postgresql import insert

from app.api.errors import ApiError, error_body, request_id_of
from app.auth import rate_limit
from app.auth.cookies import (
    clear_session_cookies,
    cookie_names,
    new_csrf_token,
    set_csrf_cookie,
    set_session_cookies,
)
from app.auth.dependencies import (
    client_ip,
    current_user,
    enforce_limit,
    get_app_settings,
    unavailable,
    user_db,
)
from app.auth.gotrue import AuthServiceError, AuthSession, AuthUnavailableError, SupabaseAuthClient
from app.auth.tokens import VerifiedUser
from app.config import Settings
from app.db.models import Profile
from app.schemas.auth import (
    AcceptedOut,
    Credentials,
    CsrfOut,
    PasswordResetConfirm,
    PasswordResetRequest,
    SessionOut,
    SignupRequest,
    UserOut,
)
from app.schemas.errors import ErrorResponse, FieldError

logger = logging.getLogger("app.auth")
router = APIRouter(prefix="/auth", tags=["auth"])
_ERRORS = {code: {"model": ErrorResponse} for code in (400, 401, 403, 409, 422, 429, 503)}


def _auth_client(settings: Settings) -> SupabaseAuthClient:
    try:
        return SupabaseAuthClient(settings)
    except AuthUnavailableError as exc:
        raise unavailable("Sign-in") from exc


def ensure_profile(settings: Settings, user_id: uuid.UUID) -> None:
    """Create the user's empty profile row on first sign-in (idempotent)."""
    user = VerifiedUser(id=user_id, email=None, access_token="")
    with user_db(settings, user) as db:
        db.execute(insert(Profile).values(user_id=user_id).on_conflict_do_nothing())


def _signed_in(response: Response, settings: Settings, session: AuthSession) -> SessionOut:
    set_session_cookies(response, settings, session)
    ensure_profile(settings, session.user_id)
    return SessionOut(status="signed_in", user=UserOut(id=session.user_id, email=session.email))


@router.get("/csrf", response_model=CsrfOut)
def csrf_token(
    request: Request, response: Response, settings: Settings = Depends(get_app_settings)
) -> CsrfOut:
    """Issue (or re-issue) the CSRF cookie the frontend echoes in X-CSRF-Token."""
    existing = request.cookies.get(cookie_names(settings).csrf)
    token = existing if existing and len(existing) >= 32 else new_csrf_token()
    set_csrf_cookie(response, settings, token)
    return CsrfOut(csrf_token=token)


@router.post(
    "/signup", response_model=SessionOut, responses=_ERRORS, status_code=status.HTTP_201_CREATED
)
def sign_up(
    body: SignupRequest,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_app_settings),
) -> SessionOut:
    enforce_limit(settings, rate_limit.SIGNUP_IP, client_ip(request))
    client = _auth_client(settings)
    try:
        session = client.sign_up(body.email, body.password)
    except AuthUnavailableError as exc:
        raise unavailable("Sign-up") from exc
    except AuthServiceError as exc:
        if exc.error_code == "weak_password":
            raise ApiError(
                422,
                "validation_error",
                "Choose a stronger password.",
                fields=[
                    FieldError(
                        loc=["body", "password"],
                        message=exc.message or "Password is too weak.",
                        type="weak_password",
                    )
                ],
            ) from exc
        if exc.error_code in {"email_address_invalid", "validation_failed"}:
            raise ApiError(
                422,
                "validation_error",
                "Some fields are missing or invalid.",
                fields=[
                    FieldError(
                        loc=["body", "email"],
                        message="Enter a valid email address.",
                        type="invalid_email",
                    )
                ],
            ) from exc
        # Existing accounts and other refusals get one generic answer.
        raise ApiError(
            400,
            "signup_failed",
            "We couldn't create an account with those details. "
            "If you already have one, sign in or reset your password.",
        ) from exc
    if session is None:
        response.status_code = status.HTTP_202_ACCEPTED
        return SessionOut(status="confirmation_required")
    return _signed_in(response, settings, session)


@router.post("/login", response_model=SessionOut, responses=_ERRORS)
def log_in(
    body: Credentials,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_app_settings),
) -> SessionOut:
    enforce_limit(settings, rate_limit.LOGIN_IP, client_ip(request))
    enforce_limit(settings, rate_limit.LOGIN_EMAIL, body.email)
    client = _auth_client(settings)
    try:
        session = client.sign_in(body.email, body.password)
    except AuthUnavailableError as exc:
        raise unavailable("Sign-in") from exc
    except AuthServiceError as exc:
        if exc.error_code == "email_not_confirmed":
            raise ApiError(
                403, "email_not_confirmed", "Confirm your email address, then sign in."
            ) from exc
        raise ApiError(401, "invalid_credentials", "Email or password is incorrect.") from exc
    return _signed_in(response, settings, session)


@router.post("/refresh", response_model=SessionOut, responses=_ERRORS)
def refresh_session(
    request: Request, response: Response, settings: Settings = Depends(get_app_settings)
) -> SessionOut | JSONResponse:
    enforce_limit(settings, rate_limit.REFRESH_IP, client_ip(request))
    refresh_token = request.cookies.get(cookie_names(settings).refresh)
    if not refresh_token or len(refresh_token) > 512:
        raise ApiError(401, "unauthenticated", "Please sign in.")
    client = _auth_client(settings)
    try:
        session = client.refresh(refresh_token)
    except AuthUnavailableError as exc:
        raise unavailable("Sign-in") from exc
    except AuthServiceError:
        # Return (not raise) the error so the cookie deletions travel with it.
        expired = JSONResponse(status_code=401, content=None)
        expired.body = error_body(
            "session_expired",
            "Your session has ended. Please sign in again.",
            request_id_of(request),
        )
        expired.headers["content-length"] = str(len(expired.body))
        clear_session_cookies(expired, settings)
        return expired
    set_session_cookies(response, settings, session)
    return SessionOut(status="signed_in", user=UserOut(id=session.user_id, email=session.email))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def log_out(request: Request, settings: Settings = Depends(get_app_settings)) -> Response:
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    access = request.cookies.get(cookie_names(settings).access)
    if access:
        try:
            _auth_client(settings).sign_out(access)  # revokes the refresh token server-side
        except (ApiError, AuthUnavailableError, AuthServiceError):
            logger.warning("remote sign-out failed; clearing cookies anyway")
    clear_session_cookies(response, settings)
    return response


@router.get("/me", response_model=UserOut, responses=_ERRORS)
def me(user: VerifiedUser = Depends(current_user)) -> UserOut:
    return UserOut(id=user.id, email=user.email)


@router.post(
    "/password-reset",
    response_model=AcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERRORS,
)
def request_password_reset(
    body: PasswordResetRequest, request: Request, settings: Settings = Depends(get_app_settings)
) -> AcceptedOut:
    enforce_limit(settings, rate_limit.RESET_IP, client_ip(request))
    enforce_limit(settings, rate_limit.RESET_EMAIL, body.email)
    try:
        _auth_client(settings).request_password_reset(body.email)
    except AuthServiceError:
        pass  # same answer whether or not the account exists
    except AuthUnavailableError as exc:
        raise unavailable("Password reset") from exc
    return AcceptedOut(message="If an account exists for that email, a reset link is on its way.")


@router.post("/password-reset/confirm", response_model=SessionOut, responses=_ERRORS)
def confirm_password_reset(
    body: PasswordResetConfirm,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_app_settings),
) -> SessionOut:
    enforce_limit(settings, rate_limit.RESET_IP, client_ip(request))
    client = _auth_client(settings)
    try:
        session = client.verify_recovery(body.token_hash)
        client.update_password(session.access_token, body.password)
    except AuthUnavailableError as exc:
        raise unavailable("Password reset") from exc
    except AuthServiceError as exc:
        if exc.error_code == "weak_password":
            raise ApiError(
                422,
                "validation_error",
                "Choose a stronger password.",
                fields=[
                    FieldError(
                        loc=["body", "password"],
                        message=exc.message or "Password is too weak.",
                        type="weak_password",
                    )
                ],
            ) from exc
        raise ApiError(
            400,
            "invalid_reset_link",
            "This reset link is invalid or has expired. Request a new one.",
        ) from exc
    return _signed_in(response, settings, session)
