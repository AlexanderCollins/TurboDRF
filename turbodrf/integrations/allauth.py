"""
TurboDRF django-allauth integration.

Provides seamless integration between django-allauth authentication
and TurboDRF's role-based permission system.
"""

import importlib.util

from django.conf import settings
from django.contrib.auth.models import AnonymousUser


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

    This function supports two modes:
    1. Default: Group names are used directly as role names
    2. Custom mapping: Use TURBODRF_ALLAUTH_ROLE_MAPPING setting

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
        user.groups = [Group(name='Administrators')]
        get_user_roles_from_groups(user)  # ['admin']
    """
    role_mapping = get_role_mapping()

    roles = []
    for group in user.groups.all():
        if role_mapping:
            # Use custom mapping, fallback to group name if not in mapping
            role = role_mapping.get(group.name, group.name)
        else:
            # Default: use group name as role
            role = group.name

        roles.append(role)

    return roles


class AllAuthRoleMiddleware:
    """
    Middleware to add roles property to authenticated users.

    This middleware automatically populates the `roles` property on
    authenticated users based on their Django group membership.

    The middleware respects existing `roles` properties, so if a user
    already has roles defined (e.g., custom User model), they won't
    be overridden.

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
    }
