"""
Define role-based permissions here.
Format: role_name -> list of permissions
"""

# Maximum nesting depth for nested fields and filters
# Default: 3 (e.g., author__publisher__name)
# WARNING: Values > 3 are UNSUPPORTED and may cause performance issues,
# security risks, and unexpected behavior. Increase at your own risk.
TURBODRF_MAX_NESTING_DEPTH = 3

# ---------------------------------------------------------------------------
# Row-level access control (predicate system)
# ---------------------------------------------------------------------------
# The tenant model that owns rows in your application (e.g. 'accounts.Brokerage').
# When set, every TurboDRF model must declare its relationship to this model
# via 'tenant_field', 'visibility', or 'tenancy': 'shared' — otherwise the
# router refuses to register the endpoint at startup.
TURBODRF_TENANT_MODEL = None

# Attribute on request.user that resolves to the user's tenant (object or PK).
# Required when TURBODRF_TENANT_MODEL is set. Example: 'brokerage' (so
# request.user.brokerage returns the Brokerage instance).
TURBODRF_TENANT_USER_FIELD = None

# When True, the router refuses to register a model that has neither a tenancy
# declaration ('tenant_field' / 'visibility') nor an explicit
# 'tenancy': 'shared' opt-out. Forces deliberate decisions about row scope.
# Recommended for new projects.
TURBODRF_REQUIRE_TENANCY = True

# When True, the router walks the FK graph at startup to fill in 'tenant_field'
# automatically when not declared. Ambiguous paths (multiple shortest routes
# to the tenant model) raise loudly rather than guessing. Default False —
# explicit declarations are easier to reason about; opt in if you want it.
TURBODRF_AUTODETECT_TENANT = False

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
