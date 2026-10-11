"""One safe JSON error envelope for every failure.

Responses never include stack traces, internal exception text, or the
submitted values that failed validation (they may be resume content).
"""

from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.schemas.errors import ErrorDetail, ErrorResponse, FieldError

_STATUS_CODES = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    429: "rate_limited",
}
_GENERIC_MESSAGES = {
    404: "The requested resource was not found.",
    405: "This method is not allowed for this resource.",
}


class ApiError(StarletteHTTPException):
    """An HTTP error with a stable machine-readable code and a safe message."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
        fields: list[FieldError] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.code = code
        self.fields = fields or []


def request_id_of(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def error_body(
    code: str, message: str, request_id: str, fields: list[FieldError] | None = None
) -> bytes:
    detail = ErrorDetail(code=code, message=message, request_id=request_id, fields=fields or [])
    return json.dumps(ErrorResponse(error=detail).model_dump(mode="json")).encode()


def _json(status: int, body: bytes) -> JSONResponse:
    response = JSONResponse(status_code=status, content=None)
    response.body = body
    response.headers["content-length"] = str(len(body))
    return response


async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    fields = []
    for err in exc.errors():
        message = "Request body is not valid JSON." if err["type"] == "json_invalid" else err["msg"]
        # Pydantic prefixes custom validator messages with "Value error, ".
        message = message.removeprefix("Value error, ")
        loc = [part if isinstance(part, int) else str(part) for part in err["loc"]]
        fields.append(FieldError(loc=loc, message=message, type=err["type"]))
    body = error_body(
        "validation_error", "Some fields are missing or invalid.", request_id_of(request), fields
    )
    return _json(422, body)


async def _http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    status = exc.status_code
    if isinstance(exc, ApiError):
        message = exc.detail  # written by us, always safe to show
    elif status >= 500:
        message = "Something went wrong on our side."
    else:
        message = _GENERIC_MESSAGES.get(status) or (
            exc.detail if isinstance(exc.detail, str) else "The request could not be completed."
        )
    code = getattr(exc, "code", None) or _STATUS_CODES.get(status, "http_error")
    fields = getattr(exc, "fields", None)
    body = error_body(code, message, request_id_of(request), fields)
    response = _json(status, body)
    if exc.headers:
        response.headers.update(exc.headers)
    return response


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(StarletteHTTPException, _http_handler)
