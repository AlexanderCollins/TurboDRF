"""
Fallback values for the TurboDRF settings that TurboDRF imports from here
when your Django settings don't define them.

Don't edit this module: set ``TURBODRF_*`` in your project's settings.py.
Every setting and its default is listed in docs/settings_reference.md; the
other settings' defaults live where they're read.
"""

# Maximum nesting depth for nested fields and filters
# Default: 3 (e.g., author__publisher__name)
# WARNING: Values > 3 are UNSUPPORTED and may cause performance issues,
# security risks, and unexpected behavior. Increase at your own risk.
TURBODRF_MAX_NESTING_DEPTH = 3

# Fields that are NEVER exposed via the API, regardless of configuration.
# These are always stripped from responses and cannot be filtered on.
# TURBODRF_SENSITIVE_FIELDS in your settings.py ADDS names to this list.
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
