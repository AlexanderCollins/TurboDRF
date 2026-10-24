"""
TurboDRF integrations package.

Provides optional integrations with popular Django packages.

The names below are re-exported lazily: importing any
``turbodrf.integrations.*`` module (``sentry``, ``keycloak``, ``roles``, ...)
doesn't import the allauth helpers or Django's auth models. They load on
first access, e.g. ``from turbodrf.integrations import assign_roles_to_user``.
"""

import importlib

_LAZY_EXPORTS = {
    # allauth integration
    "AllAuthRoleMiddleware": "allauth",
    "get_role_mapping": "allauth",
    "get_user_roles_from_groups": "allauth",
    "is_allauth_installed": "allauth",
    "is_integration_enabled": "allauth",
    "setup_allauth_integration": "allauth",
    # role utilities
    "assign_roles_to_user": "allauth_roles",
    "create_role_groups": "allauth_roles",
    "create_role_mapping": "allauth_roles",
    "get_or_create_role_group": "allauth_roles",
    "get_users_with_role": "allauth_roles",
    "sync_groups_to_roles": "allauth_roles",
    "validate_role_mapping": "allauth_roles",
}

__all__ = list(_LAZY_EXPORTS)


def __getattr__(name):
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
    module = importlib.import_module(f".{module_name}", __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
