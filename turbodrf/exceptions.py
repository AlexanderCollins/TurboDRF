"""
Standardised error responses for TurboDRF.

All error responses follow a consistent format:
{
    "error": {
        "status": 403,
        "code": "permission_denied",
        "message": "You do not have permission to perform this action."
    }
}
"""

from rest_framework.exceptions import APIException
from rest_framework.views import exception_handler


class NoRoleAssigned(APIException):
    status_code = 403
    default_detail = "No role assigned. Contact an administrator to request access."
    default_code = "no_role_assigned"


def _coerce_error_detail(value):
    """Recursively convert DRF's ErrorDetail (a str subclass) to plain Python
    types so the fast JSON renderer (msgspec/orjson) can encode them."""
    if isinstance(value, dict):
        return {k: _coerce_error_detail(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_coerce_error_detail(v) for v in value]
    return str(value) if value is not None else None


def turbodrf_exception_handler(exc, context):
    """Custom exception handler that wraps errors in a standard format."""
    response = exception_handler(exc, context)

    if response is not None:
        code = getattr(exc, "default_code", None) or "error"
        if hasattr(exc, "detail"):
            detail = exc.detail
            if isinstance(detail, dict):
                message = _coerce_error_detail(detail)
            elif isinstance(detail, list):
                message = _coerce_error_detail(detail)
            else:
                message = str(detail)
        else:
            message = str(exc)

        response.data = {
            "error": {
                "status": response.status_code,
                "code": code,
                "message": message,
            }
        }

    return response
