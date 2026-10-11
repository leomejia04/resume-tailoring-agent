from __future__ import annotations

import re
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s.]+(\.[^@\s.]+)+$")
PASSWORD_MIN = 12
PASSWORD_MAX = 128


def _email(value: str) -> str:
    value = value.strip().lower()
    if len(value) > 254 or not _EMAIL.match(value):
        raise ValueError("enter a valid email address")
    return value


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=PASSWORD_MAX)

    _normalize_email = field_validator("email")(lambda cls, v: _email(v))


class SignupRequest(Credentials):
    password: str = Field(min_length=PASSWORD_MIN, max_length=PASSWORD_MAX)


class PasswordResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=254)

    _normalize_email = field_validator("email")(lambda cls, v: _email(v))


class PasswordResetConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_hash: str = Field(min_length=10, max_length=200, pattern=r"^[A-Za-z0-9_\-]+$")
    password: str = Field(min_length=PASSWORD_MIN, max_length=PASSWORD_MAX)


class UserOut(BaseModel):
    id: uuid.UUID
    email: str | None


class SessionOut(BaseModel):
    status: Literal["signed_in", "confirmation_required"]
    user: UserOut | None = None


class CsrfOut(BaseModel):
    csrf_token: str


class AcceptedOut(BaseModel):
    status: Literal["accepted"] = "accepted"
    message: str
