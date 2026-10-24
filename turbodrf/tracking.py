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
    on top of drf-api-tracking's own masked keys. That includes requests
    rejected before DRF parsed the body (authentication, permission,
    throttling or parse errors), whose raw body drf-api-tracking would
    otherwise store as-is.

    View logs in Django admin under "API Request Logs" or query
    the APIRequestLog model programmatically.
"""

import json
import logging
from functools import lru_cache

from django.conf import settings

logger = logging.getLogger(__name__)

# drf-api-tracking's own masked key names (BaseLoggingMixin._clean_data).
_TRACKING_BUILTIN_SENSITIVE = ("api", "token", "key", "secret", "password", "signature")


def _is_tracking_app_installed():
    """True when ``rest_framework_tracking`` is in INSTALLED_APPS. Importing
    its mixins without the app installed raises RuntimeError (its models have
    no app), which would break ``turbodrf.views`` at import."""
    from django.apps import apps

    return apps.is_installed("rest_framework_tracking")


def is_tracking_enabled():
    """Check if API tracking is enabled and available.

    True only when TURBODRF_ENABLE_TRACKING is set, drf-api-tracking is
    importable AND ``rest_framework_tracking`` is in INSTALLED_APPS.
    """
    if not getattr(settings, "TURBODRF_ENABLE_TRACKING", False):
        return False

    # Check if drf-api-tracking is installed
    try:
        import rest_framework_tracking  # noqa: F401
    except ImportError:
        return False

    if not _is_tracking_app_installed():
        logger.warning(
            "TURBODRF_ENABLE_TRACKING is True but 'rest_framework_tracking' is "
            "not in INSTALLED_APPS; API request logging is off."
        )
        return False
    return True


def is_anonymous_tracking_enabled():
    """True when requests from anonymous callers should be logged."""
    return bool(getattr(settings, "TURBODRF_TRACKING_ANONYMOUS", False))


def get_sensitive_field_names():
    """TurboDRF's sensitive-field deny-list: the defaults plus every name in
    TURBODRF_SENSITIVE_FIELDS (the setting adds to the defaults, it never
    replaces them)."""
    from .validation import _get_sensitive_fields

    return set(_get_sensitive_fields())


@lru_cache(maxsize=None)
def build_tracking_mixin(logging_mixin):
    """Wrap drf-api-tracking's LoggingMixin with TurboDRF's defaults.

    - Masks every name in TURBODRF_SENSITIVE_FIELDS (read at log time, so
      settings overrides apply) in addition to drf-api-tracking's built-in
      api/token/key/secret/password/signature list.
    - Masks request bodies drf-api-tracking logged raw. It stores
      ``request.body`` as text before DRF authenticates and parses the
      request, and only replaces it with the masked, parsed data once that
      succeeds; a request rejected at authentication, permission, throttling
      or parsing kept its body unmasked.
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

        def handle_log(self):
            log = getattr(self, "log", None)
            if isinstance(log, dict):
                for key in ("data", "query_params"):
                    if isinstance(log.get(key), (str, bytes)):
                        log[key] = self._turbodrf_mask_raw_body(log[key])
            return super().handle_log()

        def _turbodrf_mask_raw_body(self, body):
            """Mask a request body that was logged as raw text.

            A JSON body is parsed and masked like parsed data. Any other body
            (malformed JSON, form or multipart data, ...) is replaced entirely
            when it mentions a sensitive name.
            """
            if isinstance(body, bytes):
                body = body.decode(errors="replace")
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
            if isinstance(parsed, (dict, list)):
                return json.dumps(
                    self._clean_data(parsed), ensure_ascii=False, default=str
                )
            lowered = body.lower()
            sensitive = set(_TRACKING_BUILTIN_SENSITIVE) | {
                str(name).lower() for name in self.sensitive_fields
            }
            if any(name in lowered for name in sensitive):
                return getattr(self, "CLEANED_SUBSTITUTE", "********************")
            return body

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
