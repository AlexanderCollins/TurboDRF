"""
TurboDRF Sentry integration.

Optional Sentry tagging and breadcrumb reporting for TurboDRF requests.
When enabled, every request gets Sentry tags for tenant_id, user_id, and
user_roles, and security-relevant events (FK injection rejected, tenant
reassignment rejected, etc.) are recorded as breadcrumbs.

Off by default. To enable:

    pip install sentry-sdk
    # settings.py
    import sentry_sdk
    sentry_sdk.init(dsn="...", traces_sample_rate=0.1)

    TURBODRF_ENABLE_SENTRY = True

    MIDDLEWARE = [
        ...
        'turbodrf.integrations.sentry.SentryContextMiddleware',
    ]

When `sentry-sdk` is not installed, all functions in this module are no-ops
— you can call `report_security_event()` from anywhere in the framework
without worrying about whether Sentry is set up.
"""

import logging

logger = logging.getLogger(__name__)


def _is_sentry_enabled():
    """True only if the user opted in AND sentry-sdk is installed."""
    from django.conf import settings

    if not getattr(settings, "TURBODRF_ENABLE_SENTRY", False):
        return False
    try:
        import sentry_sdk  # noqa: F401

        return True
    except ImportError:
        return False


def _sdk():
    """Lazy import of sentry_sdk. Returns None if unavailable."""
    if not _is_sentry_enabled():
        return None
    try:
        import sentry_sdk

        return sentry_sdk
    except ImportError:
        return None


def set_request_context(request):
    """Set Sentry tags + user context for the current request scope.

    Tagged values:
      - tenant_id (from request.user.<TURBODRF_TENANT_USER_FIELD>.pk)
      - user_id
      - user_roles (comma-separated)

    Called automatically by SentryContextMiddleware. Safe to call manually.
    """
    sdk = _sdk()
    if sdk is None:
        return

    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return

    from turbodrf.backends import get_user_roles
    from turbodrf.predicates import get_user_tenant

    tenant = get_user_tenant(user)
    tenant_id = (
        getattr(tenant, "pk", tenant) if tenant is not None else None
    )
    roles = list(get_user_roles(user))

    try:
        sdk.set_user({"id": user.pk, "username": getattr(user, "username", None)})
        scope = sdk.get_current_scope()
        if tenant_id is not None:
            scope.set_tag("tenant_id", str(tenant_id))
        if roles:
            scope.set_tag("user_roles", ",".join(roles))
    except Exception as e:  # never break the request because of Sentry
        logger.debug("Sentry context set failed: %s", e)


def report_security_event(event_type, message, **extra):
    """Record a security-relevant event as a Sentry breadcrumb.

    Examples:
      - "fk_injection_rejected" — user tried to assign a cross-tenant FK
      - "tenant_reassignment_rejected" — user tried to PATCH tenant_id
      - "owner_assignment_rejected" — non-bypass user assigned row to other user
      - "predicate_denial" — predicate Q filtered out a row on detail/PATCH

    `extra` becomes structured breadcrumb data. Useful for post-incident
    forensics.

    No-op when Sentry isn't enabled.
    """
    sdk = _sdk()
    if sdk is None:
        return

    try:
        sdk.add_breadcrumb(
            category="turbodrf.security",
            message=message,
            level="warning",
            type="security",
            data={"event_type": event_type, **extra},
        )
    except Exception as e:
        logger.debug("Sentry breadcrumb failed: %s", e)


def capture_security_message(message, **extra):
    """Send a discrete Sentry message (not just a breadcrumb).

    For events significant enough to surface as their own Sentry issue —
    e.g. repeated cross-tenant FK injection attempts from the same user.
    Use sparingly to avoid Sentry quota waste.

    No-op when Sentry isn't enabled.
    """
    sdk = _sdk()
    if sdk is None:
        return

    try:
        with sdk.push_scope() as scope:
            for k, v in extra.items():
                scope.set_extra(k, v)
            scope.set_tag("turbodrf.security_event", "true")
            sdk.capture_message(message, level="warning")
    except Exception as e:
        logger.debug("Sentry capture_message failed: %s", e)


class SentryContextMiddleware:
    """Per-request Sentry tagging.

    Install AFTER `AuthenticationMiddleware` so request.user is available.
    No-op when sentry-sdk isn't installed or TURBODRF_ENABLE_SENTRY=False.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        set_request_context(request)
        return self.get_response(request)
