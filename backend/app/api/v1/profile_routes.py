from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, update

from app.api.errors import ApiError
from app.api.v1.auth_routes import ensure_profile
from app.auth.dependencies import current_user, get_app_settings, user_db
from app.auth.tokens import VerifiedUser
from app.config import Settings
from app.db.models import Profile
from app.schemas.errors import ErrorResponse
from app.schemas.profile import ProfileOut, ProfileUpdate

router = APIRouter(prefix="/profile", tags=["profile"])
_ERRORS = {code: {"model": ErrorResponse} for code in (401, 409, 422, 503)}


def _out(profile: Profile, user: VerifiedUser) -> ProfileOut:
    return ProfileOut(
        email=user.email,
        display_name=profile.display_name,
        filename_prefix=profile.filename_prefix,
        lock_version=profile.lock_version,
    )


@router.get("", response_model=ProfileOut, responses=_ERRORS)
def get_profile(
    user: VerifiedUser = Depends(current_user), settings: Settings = Depends(get_app_settings)
) -> ProfileOut:
    ensure_profile(settings, user.id)
    with user_db(settings, user) as db:
        profile = db.scalars(select(Profile).where(Profile.user_id == user.id)).one()
        return _out(profile, user)


@router.put("", response_model=ProfileOut, responses=_ERRORS)
def update_profile(
    body: ProfileUpdate,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> ProfileOut:
    ensure_profile(settings, user.id)
    with user_db(settings, user) as db:
        # Optimistic concurrency: only update the version the client last saw.
        result = db.execute(
            update(Profile)
            .where(Profile.user_id == user.id, Profile.lock_version == body.expected_version)
            .values(
                display_name=body.display_name,
                filename_prefix=body.filename_prefix,
                lock_version=Profile.lock_version + 1,
                updated_at=func.now(),
            )
            .returning(Profile)
        )
        profile = result.scalars().first()
        if profile is None:
            raise ApiError(
                409,
                "conflict",
                "Your profile changed in another tab. Reload to see the latest version.",
            )
        return _out(profile, user)
