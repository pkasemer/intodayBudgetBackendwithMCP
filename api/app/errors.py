class ApiError(Exception):
    """Raised anywhere in a request; rendered as the spec's error envelope (§7.2)."""

    def __init__(self, status: int, code: str, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


def validation_error(message: str) -> ApiError:
    return ApiError(422, "validation", message)


def error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}
