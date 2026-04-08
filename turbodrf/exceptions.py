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


def turbodrf_exception_handler(exc, context):
    """Custom exception handler that wraps errors in a standard format."""
    response = exception_handler(exc, context)

    if response is not None:
        code = getattr(exc, "default_code", None) or "error"
        if hasattr(exc, "detail"):
            detail = exc.detail
            if isinstance(detail, dict):
                # Validation errors — keep field-level detail
                message = detail
            elif isinstance(detail, list):
                message = detail
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
