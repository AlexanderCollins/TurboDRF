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
    TURBODRF_TRACKING_ANONYMOUS (bool): Track anonymous users. Default: False

Usage:
    Once enabled, all TurboDRF viewsets will automatically log:
    - Request/response data
    - User information
    - Response time
    - Status codes
    - Query parameters

    View logs in Django admin under "API Request Logs" or query
    the APIRequestLog model programmatically.
"""

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


def get_tracking_mixin():
    """
    Get the tracking mixin class if available.

    Returns:
        class or None: The APILoggingMixin if tracking is enabled,
                      otherwise None.
    """
    if not is_tracking_enabled():
        return None

    try:
        from rest_framework_tracking.mixins import LoggingMixin

        return LoggingMixin
    except ImportError:
        return None


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
