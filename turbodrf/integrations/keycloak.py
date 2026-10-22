"""
TurboDRF Keycloak/OpenID Connect integration.

Provides integration between Keycloak (or other OpenID Connect providers)
and TurboDRF's role-based permission system.

This module supports:
- Role extraction from the claims social-auth stored at login
- Group mapping from Keycloak groups to TurboDRF roles
- Custom claims handling for role assignment

Roles are read from the ``extra_data`` social-auth-app-django saved for the
user's Keycloak association when they logged in. They are NOT re-read from a
live token on each request, so they stay as they were at login until the
user logs in again. Set ``TURBODRF_KEYCLOAK_MAX_ROLE_AGE`` to drop them once
the stored ``auth_time`` is older than that many seconds.

Setup:
1. Install social auth: pip install social-auth-app-django
2. Configure Keycloak as an OAuth2 provider in settings
3. Enable integration: TURBODRF_KEYCLOAK_INTEGRATION = True
4. Configure role mapping: TURBODRF_KEYCLOAK_ROLE_MAPPING
"""

import importlib.util
import logging
import time

from django.conf import settings

logger = logging.getLogger(__name__)


def is_social_auth_installed():
    """
    Check if social-auth-app-django is installed.

    Returns:
        bool: True if social-auth-app-django is installed, False otherwise.
    """
    return importlib.util.find_spec("social_django") is not None


def is_integration_enabled():
    """
    Check if Keycloak integration is enabled in settings.

    Returns:
        bool: True if TURBODRF_KEYCLOAK_INTEGRATION is True, False otherwise.
    """
    return getattr(settings, "TURBODRF_KEYCLOAK_INTEGRATION", False)


def get_role_claim_path():
    """
    Get the JSON path to roles in the ID token claims.

    Different Keycloak configurations place roles in different claims.
    Common paths:
    - 'roles' (simple)
    - 'realm_access.roles' (realm roles)
    - 'resource_access.{client-id}.roles' (client roles)

    Returns:
        str: Dot-separated path to the roles claim. Default: 'roles'
    """
    return getattr(settings, "TURBODRF_KEYCLOAK_ROLE_CLAIM", "roles")


def get_role_mapping():
    """
    Get the role mapping configuration from settings.

    Maps Keycloak role names to TurboDRF role names.

    Returns:
        dict: Mapping of Keycloak role names to TurboDRF role names.
              Empty dict if no mapping is configured.

    Example:
        {
            'realm-admin': 'admin',
            'content-editor': 'editor',
            'basic-user': 'viewer'
        }
    """
    return getattr(settings, "TURBODRF_KEYCLOAK_ROLE_MAPPING", {})


def get_provider_name():
    """
    Get the social-auth provider name whose data carries Keycloak roles.

    Only associations from this provider are trusted for roles. A user's
    other linked accounts (GitHub, Google, ...) can never supply them.

    Returns:
        str: The provider name. Default: 'keycloak'
    """
    return getattr(settings, "TURBODRF_KEYCLOAK_PROVIDER", "keycloak")


def get_max_role_age():
    """
    Get the maximum age, in seconds, of roles captured at login.

    Returns:
        int | float | None: Seconds after the stored ``auth_time`` beyond
        which roles are dropped, or None (default) for no limit.
    """
    return getattr(settings, "TURBODRF_KEYCLOAK_MAX_ROLE_AGE", None)


def _roles_too_old(extra_data):
    """True when TURBODRF_KEYCLOAK_MAX_ROLE_AGE is set and the stored
    ``auth_time`` is older than it. A missing or unreadable ``auth_time``
    counts as too old (fail closed)."""
    max_age = get_max_role_age()
    if max_age is None:
        return False
    auth_time = extra_data.get("auth_time") if isinstance(extra_data, dict) else None
    try:
        age = time.time() - float(auth_time)
    except (TypeError, ValueError):
        logger.warning(
            "Keycloak roles dropped: TURBODRF_KEYCLOAK_MAX_ROLE_AGE is set but "
            "the stored social-auth data has no usable auth_time."
        )
        return True
    if age > max_age:
        logger.info(
            "Keycloak roles dropped: captured at login %ds ago, older than "
            "TURBODRF_KEYCLOAK_MAX_ROLE_AGE=%s. The user must log in again.",
            age,
            max_age,
        )
        return True
    return False


def extract_roles_from_token(token_claims):
    """
    Extract roles from OpenID Connect ID token claims.

    This function navigates the token claims dictionary using the
    configured role claim path and extracts the list of roles.

    Args:
        token_claims (dict): The ID token claims from the OpenID Connect provider.

    Returns:
        list: List of role names from the token.

    Example:
        # Simple roles claim
        token = {'roles': ['admin', 'editor']}
        extract_roles_from_token(token)  # ['admin', 'editor']

        # Nested realm_access
        token = {'realm_access': {'roles': ['admin', 'editor']}}
        # With TURBODRF_KEYCLOAK_ROLE_CLAIM = 'realm_access.roles'
        extract_roles_from_token(token)  # ['admin', 'editor']
    """
    claim_path = get_role_claim_path()
    parts = claim_path.split(".")

    current = token_claims
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            # Path not found, return empty list
            return []

    # Current should now be a list of roles
    if isinstance(current, list):
        return current
    else:
        return []


def map_keycloak_roles_to_turbodrf(keycloak_roles):
    """Map Keycloak role names to TurboDRF role names.

    Behavior depends on TURBODRF_KEYCLOAK_STRICT_ROLES (default True) and
    whether TURBODRF_KEYCLOAK_ROLE_MAPPING is configured:

    - No mapping configured → passthrough (legacy behavior; user clearly
      intends to use Keycloak role names as TurboDRF role names directly)
    - Mapping configured + strict=True (default): the mapping acts as an
      allow-list. Unmapped Keycloak roles are dropped with a warning log.
      A Keycloak role named 'admin' does NOT become TurboDRF role 'admin'
      unless explicitly mapped — otherwise a Keycloak realm admin would
      automatically gain TurboDRF admin if the names happened to match.
    - Mapping configured + strict=False: legacy passthrough — unmapped
      roles pass through under their original name.

    Args:
        keycloak_roles (list): List of role names from Keycloak.

    Returns:
        list: List of TurboDRF role names after mapping.
    """
    role_mapping = get_role_mapping()
    strict = getattr(settings, "TURBODRF_KEYCLOAK_STRICT_ROLES", True)

    if not role_mapping:
        # No mapping configured — passthrough
        return list(keycloak_roles)

    mapped_roles = []
    for role in keycloak_roles:
        if role in role_mapping:
            mapped_roles.append(role_mapping[role])
        elif strict:
            logger.warning(
                "Keycloak role %r has no entry in "
                "TURBODRF_KEYCLOAK_ROLE_MAPPING and was rejected. "
                "Add it to the mapping or set "
                "TURBODRF_KEYCLOAK_STRICT_ROLES=False to allow passthrough.",
                role,
            )
        else:
            # Legacy permissive: pass through unmapped roles
            mapped_roles.append(role)

    return mapped_roles


def get_user_roles_from_social_auth(user):
    """
    Get TurboDRF roles for a user from their Keycloak social auth data.

    Reads the ``extra_data`` social-auth stored for the user's association
    with the configured provider (``TURBODRF_KEYCLOAK_PROVIDER``, default
    'keycloak') at login. Associations with any other provider are ignored:
    a role claim in, say, a GitHub account's data is never trusted.

    Roles reflect the claims at login time. When
    ``TURBODRF_KEYCLOAK_MAX_ROLE_AGE`` is set, roles whose ``auth_time`` is
    older than that are dropped.

    Args:
        user: Django user object with social_auth relationship.

    Returns:
        list: List of TurboDRF role names for the user.

    Example:
        roles = get_user_roles_from_social_auth(request.user)
        # Returns: ['admin', 'editor']
    """
    if not hasattr(user, "social_auth"):
        return []

    provider = get_provider_name()

    for social_auth in user.social_auth.filter(provider=provider):
        # Re-check in Python: never trust another provider's data even if
        # the manager returned it.
        if getattr(social_auth, "provider", None) != provider:
            continue

        extra_data = social_auth.extra_data
        keycloak_roles = extract_roles_from_token(extra_data)
        if not keycloak_roles:
            continue
        if _roles_too_old(extra_data):
            # Stale login data is never used.
            continue
        return map_keycloak_roles_to_turbodrf(keycloak_roles)

    return []


class KeycloakRoleMiddleware:
    """
    Middleware to add roles property to users authenticated via Keycloak.

    This middleware populates the `roles` property on authenticated users
    from the claims social-auth stored at login. It is a no-op unless
    TURBODRF_KEYCLOAK_INTEGRATION is True.

    It runs before DRF authentication, so it only sees session-authenticated
    users. Token/JWT-authenticated users get their roles from the resolver
    TurboDRF installs on the user model at startup (see
    turbodrf.integrations.roles).

    Usage:
        Add to MIDDLEWARE in settings.py:

        MIDDLEWARE = [
            ...
            'turbodrf.integrations.keycloak.KeycloakRoleMiddleware',
            ...
        ]

        Configure Keycloak integration:

        TURBODRF_KEYCLOAK_INTEGRATION = True
        TURBODRF_KEYCLOAK_ROLE_CLAIM = 'realm_access.roles'
        TURBODRF_KEYCLOAK_ROLE_MAPPING = {
            'realm-admin': 'admin',
            'content-editor': 'editor',
        }
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
            # Check if user already has roles configured
            if not hasattr(request.user, "roles") or not request.user.roles:
                # Get roles from social auth
                roles = get_user_roles_from_social_auth(request.user)

                if roles:
                    # Set roles in user dict to avoid conflicts with properties
                    request.user.__dict__["roles"] = roles

        response = self.get_response(request)
        return response


def setup_keycloak_integration():
    """
    Setup helper for configuring Keycloak with TurboDRF.

    Returns:
        dict: Configuration status and recommendations.

    Example:
        status = setup_keycloak_integration()
        if not status['social_auth_installed']:
            print("Install: pip install social-auth-app-django")
    """
    return {
        "social_auth_installed": is_social_auth_installed(),
        "integration_enabled": is_integration_enabled(),
        "role_claim_path": get_role_claim_path(),
        "provider": get_provider_name(),
        "max_role_age": get_max_role_age(),
        "role_mapping": get_role_mapping(),
        "has_custom_mapping": bool(get_role_mapping()),
    }
