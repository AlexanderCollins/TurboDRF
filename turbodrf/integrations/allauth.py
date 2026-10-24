"""
TurboDRF django-allauth integration.

Maps a user's Django groups to TurboDRF roles. With
``TURBODRF_ALLAUTH_INTEGRATION = True`` at startup, TurboDRF installs a
``roles`` resolver on the user model (turbodrf.integrations.roles) that uses
:func:`get_user_roles_from_groups`, so every request gets the same roles
whatever authenticated it. No middleware is needed.
"""

import importlib.util
import logging
import warnings

from django.conf import settings

logger = logging.getLogger(__name__)


def is_allauth_installed():
    """
    Check if django-allauth is installed.

    Returns:
        bool: True if allauth is installed, False otherwise.
    """
    return importlib.util.find_spec("allauth") is not None


def is_integration_enabled():
    """
    Check if allauth integration is enabled in settings.

    Returns:
        bool: True if TURBODRF_ALLAUTH_INTEGRATION is True, False otherwise.
    """
    return getattr(settings, "TURBODRF_ALLAUTH_INTEGRATION", False)


def get_role_mapping():
    """
    Get the role mapping configuration from settings.

    Returns:
        dict: Mapping of Django group names to TurboDRF role names.
              Empty dict if no mapping is configured.
    """
    return getattr(settings, "TURBODRF_ALLAUTH_ROLE_MAPPING", {})


def get_user_roles_from_groups(user):
    """
    Get TurboDRF roles for a user based on their Django groups.

    Behavior depends on TURBODRF_ALLAUTH_ROLE_MAPPING and
    TURBODRF_ALLAUTH_STRICT_ROLES (default True):

    - No mapping configured: group names are used directly as role names.
    - Mapping configured + strict (default): the mapping is an allow-list.
      Groups not in the mapping are dropped (and logged), so a Django Group
      that happens to be named 'admin' does NOT become the TurboDRF 'admin'
      role unless the mapping says so.
    - Mapping configured + strict=False: legacy passthrough, unmapped groups
      become roles under their own names.

    Args:
        user: Django user object with groups relationship.

    Returns:
        list: List of role names for the user.

    Example:
        # Default mode (no mapping)
        user.groups = [Group(name='admin'), Group(name='editor')]
        get_user_roles_from_groups(user)  # ['admin', 'editor']

        # Custom mapping mode
        settings.TURBODRF_ALLAUTH_ROLE_MAPPING = {
            'Administrators': 'admin',
            'Content Editors': 'editor'
        }
        user.groups = [Group(name='Administrators'), Group(name='admin')]
        get_user_roles_from_groups(user)  # ['admin'] ('admin' group dropped)
    """
    role_mapping = get_role_mapping()
    strict = getattr(settings, "TURBODRF_ALLAUTH_STRICT_ROLES", True)

    roles = []
    for group in user.groups.all():
        if not role_mapping:
            # No mapping: use group name as role
            roles.append(group.name)
        elif group.name in role_mapping:
            roles.append(role_mapping[group.name])
        elif strict:
            logger.warning(
                "Django group %r has no entry in TURBODRF_ALLAUTH_ROLE_MAPPING "
                "and was not mapped to a role. Add it to the mapping or set "
                "TURBODRF_ALLAUTH_STRICT_ROLES=False to allow passthrough.",
                group.name,
            )
        else:
            # Legacy permissive: unmapped groups pass through by name
            roles.append(group.name)

    return roles


class AllAuthRoleMiddleware:
    """
    Deprecated: does nothing. Remove it from ``MIDDLEWARE``.

    When ``TURBODRF_ALLAUTH_INTEGRATION`` is True at startup, TurboDRF
    installs a ``roles`` resolver on the user model (see
    turbodrf.integrations.roles), which gives every request its group roles
    whatever authenticated it: session, token or JWT.

    This middleware used to write the roles onto ``request.user`` for
    session requests only. That shadowed the user model's own ``roles`` for
    those requests, so the same user could get different roles on a session
    request and on a token request, and it queried the groups on every
    session request, API or not. It now passes requests straight through and
    warns once at startup.
    """

    def __init__(self, get_response):
        """
        Initialize the middleware.

        Args:
            get_response: Django's get_response callable.
        """
        warnings.warn(
            "turbodrf.integrations.allauth.AllAuthRoleMiddleware is deprecated "
            "and does nothing; remove it from MIDDLEWARE. With "
            "TURBODRF_ALLAUTH_INTEGRATION=True, the roles resolver TurboDRF "
            "installs on the user model resolves group roles for every request.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.get_response = get_response

    def __call__(self, request):
        """Pass the request through unchanged."""
        return self.get_response(request)


def setup_allauth_integration():
    """
    Setup helper for configuring allauth with TurboDRF.

    This function provides guidance for setting up allauth with TurboDRF.
    It checks if allauth is installed and if integration is enabled.

    Returns:
        dict: Configuration status and recommendations.

    Example:
        status = setup_allauth_integration()
        if status['allauth_installed'] and not status['integration_enabled']:
            print("Enable integration by setting TURBODRF_ALLAUTH_INTEGRATION=True")
    """
    return {
        "allauth_installed": is_allauth_installed(),
        "integration_enabled": is_integration_enabled(),
        "role_mapping": get_role_mapping(),
        "has_custom_mapping": bool(get_role_mapping()),
        "strict_roles": getattr(settings, "TURBODRF_ALLAUTH_STRICT_ROLES", True),
    }
