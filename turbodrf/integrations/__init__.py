"""
TurboDRF integrations package.

Provides optional integrations with popular Django packages.
"""

from .allauth import (
    AllAuthRoleMiddleware,
    get_role_mapping,
    get_user_roles_from_groups,
    is_allauth_installed,
    is_integration_enabled,
    setup_allauth_integration,
)
from .allauth_roles import (
    assign_roles_to_user,
    create_role_groups,
    create_role_mapping,
    get_or_create_role_group,
    get_users_with_role,
    sync_groups_to_roles,
    validate_role_mapping,
)

__all__ = [
    # allauth integration
    "AllAuthRoleMiddleware",
    "get_role_mapping",
    "get_user_roles_from_groups",
    "is_allauth_installed",
    "is_integration_enabled",
    "setup_allauth_integration",
    # role utilities
    "assign_roles_to_user",
    "create_role_groups",
    "create_role_mapping",
    "get_or_create_role_group",
    "get_users_with_role",
    "sync_groups_to_roles",
    "validate_role_mapping",
]
