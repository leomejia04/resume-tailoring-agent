from __future__ import annotations

from pydantic import BaseModel, Field


class FieldError(BaseModel):
    loc: list[str | int] = Field(description="Where the problem is, e.g. ['body', 'description']")
    message: str
    type: str


class ErrorDetail(BaseModel):
    code: str = Field(description="Stable machine-readable error code")
    message: str = Field(description="Safe, human-readable explanation")
    request_id: str = Field(description="Quote this when reporting a problem")
    fields: list[FieldError] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    error: ErrorDetail
