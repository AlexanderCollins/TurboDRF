"""
DRF API Tracking Integration for TurboDRF.

This module provides optional integration with drf-api-tracking for
monitoring API usage, request logging, and analytics.

To enable tracking:
1. Install drf-api-tracking: pip install drf-api-tracking
2. Add to INSTALLED_APPS: 'rest_framework_tracking'
3. Run migrations: python manage.py migrate
4. Enable in settings: TURBODRF_ENABLE_TRACKING = True

Configuration:
    TURBODRF_ENABLE_TRACKING (bool): Enable API tracking. Default: False
    TURBODRF_TRACKING_ANONYMOUS (bool): Also log requests from anonymous
        (unauthenticated) callers. Default: False — they are not logged.

Usage:
    Once enabled, all TurboDRF viewsets will automatically log:
    - Request/response data
    - User information
    - Response time
    - Status codes
    - Query parameters

    Values of keys in TURBODRF_SENSITIVE_FIELDS (passwords, tokens, API
    keys, ...) are masked in the logged request data and query parameters,
    on top of drf-api-tracking's own masked keys.

    View logs in Django admin under "API Request Logs" or query
    the APIRequestLog model programmatically.
"""

from functools import lru_cache

from django.conf import settings


def is_tracking_enabled():
    """Check if API tracking is enabled and available."""
    if not getattr(settings, "TURBODRF_ENABLE_TRACKING", False):
        return False

    # Check if drf-api-tracking is installed
    try:
        import rest_framework_tracking  # noqa: F401

        return True
    except ImportError:
        return False


def is_anonymous_tracking_enabled():
    """True when requests from anonymous callers should be logged."""
    return bool(getattr(settings, "TURBODRF_TRACKING_ANONYMOUS", False))


def get_sensitive_field_names():
    """TurboDRF's sensitive-field deny-list (TURBODRF_SENSITIVE_FIELDS)."""
    from .settings import TURBODRF_SENSITIVE_FIELDS as default_sensitive

    return set(getattr(settings, "TURBODRF_SENSITIVE_FIELDS", default_sensitive))


@lru_cache(maxsize=None)
def build_tracking_mixin(logging_mixin):
    """Wrap drf-api-tracking's LoggingMixin with TurboDRF's defaults.

    - Masks every name in TURBODRF_SENSITIVE_FIELDS (read at log time, so
      settings overrides apply) in addition to drf-api-tracking's built-in
      api/token/key/secret/password/signature list.
    - Skips anonymous requests unless TURBODRF_TRACKING_ANONYMOUS is True.
    """

    class TurboDRFLoggingMixin(logging_mixin):
        @property
        def sensitive_fields(self):
            inherited = getattr(super(), "sensitive_fields", None) or ()
            return set(inherited) | get_sensitive_field_names()

        def should_log(self, request, response):
            if not is_anonymous_tracking_enabled():
                user = getattr(request, "user", None)
                if user is None or not getattr(user, "is_authenticated", False):
                    return False
            parent = getattr(super(), "should_log", None)
            return parent(request, response) if parent is not None else True

    TurboDRFLoggingMixin.__name__ = f"TurboDRF{logging_mixin.__name__}"
    TurboDRFLoggingMixin.__qualname__ = TurboDRFLoggingMixin.__name__
    return TurboDRFLoggingMixin


def get_tracking_mixin():
    """
    Get the tracking mixin class if available.

    Returns:
        class or None: drf-api-tracking's LoggingMixin wrapped with
                      TurboDRF's defaults (see build_tracking_mixin) if
                      tracking is enabled, otherwise None.
    """
    if not is_tracking_enabled():
        return None

    try:
        from rest_framework_tracking.mixins import LoggingMixin
    except ImportError:
        return None

    return build_tracking_mixin(LoggingMixin)


def get_viewset_base_classes():
    """
    Get the base classes for TurboDRFViewSet including tracking if enabled.

    Returns:
        tuple: Base classes to use for the viewset.
    """
    from rest_framework import viewsets

    base_classes = [viewsets.ModelViewSet]

    tracking_mixin = get_tracking_mixin()
    if tracking_mixin:
        # Add tracking mixin before ModelViewSet
        base_classes.insert(0, tracking_mixin)

    return tuple(base_classes)
