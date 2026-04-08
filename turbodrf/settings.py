"""
Define role-based permissions here.
Format: role_name -> list of permissions
"""

# Maximum nesting depth for nested fields and filters
# Default: 3 (e.g., author__publisher__name)
# WARNING: Values > 3 are UNSUPPORTED and may cause performance issues,
# security risks, and unexpected behavior. Increase at your own risk.
TURBODRF_MAX_NESTING_DEPTH = 3

# Fields that are NEVER exposed via the API, regardless of configuration.
# These are always stripped from responses and cannot be filtered on.
# Override in your settings.py to customise.
TURBODRF_SENSITIVE_FIELDS = [
    "password",
    "password_hash",
    "secret_key",
    "api_key",
    "token",
    "access_token",
    "refresh_token",
    "session_key",
]

TURBODRF_ROLES = {
    "super_admin": [
        # Model-level permissions (all models)
        "app_name.model_name.read",
        "app_name.model_name.create",
        "app_name.model_name.update",
        "app_name.model_name.delete",
        # Field-level permissions
        "app_name.model_name.field_name.read",
        "app_name.model_name.field_name.write",
    ],
    "editor": [
        "app_name.model_name.read",
        "app_name.model_name.update",
        "app_name.model_name.field_name.read",
    ],
    "viewer": [
        "app_name.model_name.read",
        "app_name.model_name.field_name.read",
    ],
}
