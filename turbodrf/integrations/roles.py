"""
Lazy role resolution for the Keycloak and allauth integrations.

The integrations' middleware runs before DRF authenticates the request, so a
token/JWT-authenticated user is still anonymous when the middleware runs and
would never get integration roles. To cover every authentication method,
TurboDRF installs a ``roles`` property on the user model at startup when an
integration is enabled and the model has no ``roles`` attribute of its own.
``get_user_roles()`` reads ``user.roles``, so the roles are resolved from the
user object itself, whatever authenticated it.

The resolved roles are cached on the user instance (normally one instance per
request), so the social-auth / group lookup runs once per request.
"""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

_CACHE_ATTR = "_turbodrf_integration_roles"


def integrations_enabled():
    """True when the Keycloak or allauth integration is enabled."""
    return bool(
        getattr(settings, "TURBODRF_KEYCLOAK_INTEGRATION", False)
        or getattr(settings, "TURBODRF_ALLAUTH_INTEGRATION", False)
    )


def resolve_integration_roles(user):
    """Roles for ``user`` from every enabled integration.

    Keycloak roles (from the user's Keycloak social-auth association) come
    first, then allauth roles (from Django groups); duplicates are removed.
    Disabled integrations contribute nothing.
    """
    instance_dict = getattr(user, "__dict__", None)
    if instance_dict is not None:
        cached = instance_dict.get(_CACHE_ATTR)
        if cached is not None:
            return list(cached)

    from . import allauth, keycloak

    roles = []
    if keycloak.is_integration_enabled():
        roles.extend(keycloak.get_user_roles_from_social_auth(user))
    if allauth.is_integration_enabled():
        roles.extend(allauth.get_user_roles_from_groups(user))
    roles = list(dict.fromkeys(roles))

    if instance_dict is not None:
        instance_dict[_CACHE_ATTR] = roles
    return list(roles)


def _integration_roles(self):
    return resolve_integration_roles(self)


def install_integration_role_resolver(user_model=None):
    """Install the lazy ``roles`` property on the user model.

    Called from TurboDRFConfig.ready(). Does nothing, and returns False, when
    neither integration is enabled or when the user model (or another app)
    already defines ``roles`` — an existing definition is never replaced.

    Args:
        user_model: The class to install on. Defaults to the active user
            model.

    Returns:
        bool: True if the resolver is installed on ``user_model``.
    """
    if not integrations_enabled():
        return False
    if user_model is None:
        from django.contrib.auth import get_user_model

        user_model = get_user_model()

    existing = getattr(user_model, "roles", None)
    if isinstance(existing, property) and existing.fget is _integration_roles:
        return True
    if hasattr(user_model, "roles"):
        logger.info(
            "%s already defines 'roles'; TurboDRF's integration role resolver "
            "was not installed. Token/JWT-authenticated users get only the "
            "roles your 'roles' attribute returns.",
            user_model.__name__,
        )
        return False

    setattr(
        user_model,
        "roles",
        property(
            _integration_roles,
            doc="TurboDRF roles resolved from the enabled auth integrations.",
        ),
    )
    return True
