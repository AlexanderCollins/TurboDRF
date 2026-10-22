"""
TurboDRF Sentry integration.

Optional Sentry tagging and breadcrumb reporting for TurboDRF requests.
When enabled, every request gets Sentry tags for tenant_id and user_roles
plus the user's id (never the username or any other PII), and
security-relevant events (FK injection rejected, tenant reassignment
rejected, etc.) are recorded as breadcrumbs.

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
from contextlib import ExitStack

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


def _current_scope(sdk):
    getter = getattr(sdk, "get_current_scope", None)
    if getter is None:
        return None
    try:
        return getter()
    except Exception:
        return None


def _user_context(user):
    """(user_id, tenant_id, roles) for an authenticated user, else None."""
    if user is None or not getattr(user, "is_authenticated", False):
        return None

    from turbodrf.backends import get_user_roles
    from turbodrf.predicates import get_user_tenant

    tenant = get_user_tenant(user)
    tenant_id = getattr(tenant, "pk", tenant) if tenant is not None else None
    roles = list(get_user_roles(user))
    return getattr(user, "pk", None), tenant_id, roles


def set_request_context(request, scope=None):
    """Set Sentry tags + user context for the current request scope.

    Tagged values:
      - tenant_id (from request.user.<TURBODRF_TENANT_USER_FIELD>.pk)
      - user_roles (comma-separated)
    and the Sentry user is set to ``{"id": user.pk}`` only — no username or
    other PII, whatever ``send_default_pii`` says.

    For an anonymous request (or a user with no tenant / no roles) the user
    and tags are CLEARED, so nothing carries over from an earlier request
    that shared the scope.

    Called automatically by SentryContextMiddleware. Safe to call manually.

    Args:
        request: The request whose ``user`` to describe.
        scope: The Sentry scope to write to. Defaults to the module-level
            API (``sentry_sdk.set_user``) and the current scope.
    """
    sdk = _sdk()
    if sdk is None:
        return

    try:
        context = _user_context(getattr(request, "user", None))
        set_user = scope.set_user if scope is not None else sdk.set_user
        tag_scope = scope if scope is not None else _current_scope(sdk)

        if context is None:
            set_user(None)
            tenant_id, roles = None, []
        else:
            user_id, tenant_id, roles = context
            set_user({"id": user_id})

        if tag_scope is not None:
            tags = {
                "tenant_id": str(tenant_id) if tenant_id is not None else None,
                "user_roles": ",".join(roles) if roles else None,
            }
            for tag, value in tags.items():
                if value is None:
                    tag_scope.remove_tag(tag)
                else:
                    tag_scope.set_tag(tag, value)
    except Exception as e:  # never break the request because of Sentry
        logger.debug("Sentry context set failed: %s", e)


def _late_user_event_processor(request):
    """Event processor that describes the user as authenticated by the time
    the event is captured.

    Middleware runs before DRF authentication, so a token/JWT-authenticated
    user is still anonymous when SentryContextMiddleware runs. DRF writes the
    authenticated user back to the Django request, so reading
    ``request.user`` when an event is captured picks it up.
    """

    def processor(event, hint):
        try:
            context = _user_context(getattr(request, "user", None))
            if context is not None:
                user_id, tenant_id, roles = context
                event["user"] = {"id": user_id}
                tags = event.get("tags")
                if not isinstance(tags, dict):
                    tags = {}
                    event["tags"] = tags
                if tenant_id is not None:
                    tags["tenant_id"] = str(tenant_id)
                if roles:
                    tags["user_roles"] = ",".join(roles)
        except Exception as e:  # never drop an event because of TurboDRF
            logger.debug("Sentry event processor failed: %s", e)
        return event

    return processor


def _request_scope(sdk):
    """A context manager yielding a scope confined to one request, or None.

    sentry-sdk 2.x: ``new_scope()``; 1.x: ``push_scope()``.
    """
    factory = getattr(sdk, "new_scope", None) or getattr(sdk, "push_scope", None)
    if factory is None:
        return None
    try:
        return factory()
    except Exception:
        return None


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

    The request runs inside its own Sentry scope, so the user and tags set
    here never carry over to another request. Users that DRF authenticates
    later (token/JWT) are picked up when an event is captured.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        sdk = _sdk()
        if sdk is None:
            return self.get_response(request)

        with ExitStack() as stack:
            scope = None
            scope_cm = _request_scope(sdk)
            if scope_cm is not None:
                try:
                    scope = stack.enter_context(scope_cm)
                except Exception as e:  # never break the request
                    logger.debug("Sentry request scope failed: %s", e)
                    scope = None

            set_request_context(request, scope=scope)
            if scope is not None:
                try:
                    scope.add_event_processor(_late_user_event_processor(request))
                except Exception as e:
                    logger.debug("Sentry event processor registration failed: %s", e)
            return self.get_response(request)
