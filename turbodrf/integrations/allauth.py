"""
TurboDRF django-allauth integration.

Provides seamless integration between django-allauth authentication
and TurboDRF's role-based permission system.
"""

import importlib.util
import logging

from django.conf import settings
from django.contrib.auth.models import AnonymousUser

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
    Middleware to add roles property to authenticated users.

    This middleware populates the `roles` property on authenticated users
    based on their Django group membership. It is a no-op unless
    TURBODRF_ALLAUTH_INTEGRATION is True.

    The middleware respects existing `roles` properties, so if a user
    already has roles defined (e.g., custom User model), they won't
    be overridden.

    It runs before DRF authentication, so it only sees session-authenticated
    users. Token/JWT-authenticated users get their roles from the resolver
    TurboDRF installs on the user model at startup (see
    turbodrf.integrations.roles).

    Usage:
        Add to MIDDLEWARE in settings.py:

        MIDDLEWARE = [
            ...
            'turbodrf.integrations.allauth.AllAuthRoleMiddleware',
            ...
        ]
    """

    def __init__(self, get_response):
        """
        Initialize the middleware.

        Args:
            get_response: Django's get_response callable.
        """
        self.get_response = get_response

    def __call__(self, request):
        """
        Process the request and add roles to user.

        Args:
            request: Django request object.

        Returns:
            Response from the next middleware/view.
        """
        if not is_integration_enabled():
            return self.get_response(request)

        # Add roles to authenticated users
        if hasattr(request, "user") and request.user.is_authenticated:
            # Check if user already has _test_roles (used by test property)
            # or if roles is already customized in __dict__
            if (
                not hasattr(request.user, "_test_roles")
                and "roles" not in request.user.__dict__
            ):
                # Get roles from groups
                roles = get_user_roles_from_groups(request.user)

                # Set _test_roles if the User model uses that pattern
                # This works with the test User model's roles property
                if hasattr(request.user, "roles") and isinstance(
                    getattr(type(request.user), "roles", None), property
                ):
                    # If roles is a property, set _test_roles which it checks first
                    request.user._test_roles = roles
                else:
                    # Otherwise set roles directly in __dict__
                    request.user.__dict__["roles"] = roles
        elif hasattr(request, "user") and isinstance(request.user, AnonymousUser):
            # Anonymous users get empty roles
            request.user.__dict__["roles"] = []

        response = self.get_response(request)
        return response


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
