"""统一错误处理：业务异常一律携带 field 指向具体字段。"""

from __future__ import annotations

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, message: str, *, field: str | None = None,
                 status_code: int = 400, details=None):
        super().__init__(message)
        self.message = message
        self.field = field
        self.status_code = status_code
        self.details = details


def _flatten_validation(errors: list[dict]) -> list[dict]:
    out = []
    for e in errors:
        loc = [str(x) for x in e.get("loc", []) if x != "body"]
        out.append({
            "field": ".".join(loc) if loc else "body",
            "message": e.get("msg", "参数不合法"),
            "type": e.get("type"),
        })
    return out


def register_exception_handlers(app):
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        body = {"error": exc.message}
        if exc.field:
            body["field"] = exc.field
        if exc.details is not None:
            body["details"] = exc.details
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        details = _flatten_validation(exc.errors())
        first = details[0] if details else {"field": "body",
                                            "message": "参数不合法"}
        return JSONResponse(
            status_code=422,
            content={"error": first["message"], "field": first["field"],
                     "details": details},
        )
