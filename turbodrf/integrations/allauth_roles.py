"""
Role mapping utilities for TurboDRF django-allauth integration.

Provides helper functions for creating and managing role groups,
syncing groups to roles, and validating role mappings.

The helpers that go from a role to Django groups (and back) follow
``TURBODRF_ALLAUTH_ROLE_MAPPING`` and ``TURBODRF_ALLAUTH_STRICT_ROLES``
exactly as :func:`turbodrf.integrations.allauth.get_user_roles_from_groups`
does, so a group they create or assign really gives the role, and a user
they report as holding a role really holds it:

- No mapping configured: a group gives the role of the same name.
- A mapping, strict (default): only the mapped groups give roles.
- A mapping, ``TURBODRF_ALLAUTH_STRICT_ROLES = False``: the mapped groups,
  plus an unmapped group under its own name.
"""

from django.conf import settings
from django.contrib.auth.models import Group

from .allauth import get_role_mapping, get_user_roles_from_groups


def _group_names_for_role(role_name):
    """The Django group names whose members get ``role_name``: the mapping
    inverted, in mapping order. Empty when no group can give the role
    (strict mode, and no mapped group maps to it)."""
    mapping = get_role_mapping() or {}
    if not mapping:
        return [role_name]
    names = [group for group, role in mapping.items() if role == role_name]
    strict = getattr(settings, "TURBODRF_ALLAUTH_STRICT_ROLES", True)
    if not strict and role_name not in mapping and role_name not in names:
        # Passthrough: an unmapped group named after the role gives it.
        names.append(role_name)
    return names


def _group_name_for_role(role_name):
    names = _group_names_for_role(role_name)
    if not names:
        raise ValueError(
            f"No Django group gives the TurboDRF role {role_name!r}: it isn't a "
            f"value in TURBODRF_ALLAUTH_ROLE_MAPPING (and strict mode drops "
            f"unmapped groups). Add a group for it to the mapping."
        )
    return names[0]


def create_role_groups(role_names):
    """
    Create the Django groups that give the TurboDRF roles ``role_names``.

    For each role, the first group the mapping maps to it (the role's own
    name when no mapping is configured) is created if it doesn't exist.

    Args:
        role_names: List of role names to create groups for.

    Returns:
        list: List of created/existing Group objects.

    Raises:
        ValueError: If no group can give one of the roles (strict mode,
            role not a mapping value). Nothing is created then.

    Example:
        groups = create_role_groups(['admin', 'editor', 'viewer'])
        # Creates 3 groups if they don't exist
    """
    group_names = [_group_name_for_role(role_name) for role_name in role_names]
    return [Group.objects.get_or_create(name=name)[0] for name in group_names]


def sync_groups_to_roles(user):
    """
    The TurboDRF roles a user's Django groups give.

    Same result as
    :func:`turbodrf.integrations.allauth.get_user_roles_from_groups`: the
    mapping and strict mode apply, so a group that gives no role isn't
    reported as one.

    Args:
        user: Django user object with groups relationship.

    Returns:
        list: List of role names.

    Example:
        user.groups.add(Group.objects.get(name='admin'))
        roles = sync_groups_to_roles(user)  # ['admin'] (no mapping)
    """
    return get_user_roles_from_groups(user)


def validate_role_mapping(mapping):
    """
    Validate a role mapping configuration.

    Checks that the mapping is a dictionary with string keys and values.

    Args:
        mapping: Dictionary to validate.

    Returns:
        bool: True if valid, False otherwise.

    Example:
        valid_mapping = {'Admins': 'admin', 'Editors': 'editor'}
        validate_role_mapping(valid_mapping)  # True

        invalid_mapping = {'Admins': 123}
        validate_role_mapping(invalid_mapping)  # False
    """
    if not isinstance(mapping, dict):
        return False

    for key, value in mapping.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return False

    return True


def create_role_mapping(group_names, role_names=None):
    """
    Create a role mapping dictionary from group and role names.

    If role_names is not provided, group names are used as role names.

    Args:
        group_names: List of Django group names.
        role_names: Optional list of corresponding role names.
                   Must be same length as group_names if provided.

    Returns:
        dict: Mapping of group names to role names.

    Raises:
        ValueError: If lists have different lengths.

    Example:
        # Same names
        mapping = create_role_mapping(['admin', 'editor'])
        # {'admin': 'admin', 'editor': 'editor'}

        # Different names
        mapping = create_role_mapping(
            ['Administrators', 'Content Editors'],
            ['admin', 'editor']
        )
        # {'Administrators': 'admin', 'Content Editors': 'editor'}
    """
    if role_names is None:
        role_names = group_names

    if len(group_names) != len(role_names):
        raise ValueError("group_names and role_names must have the same length")

    return dict(zip(group_names, role_names))


def get_or_create_role_group(role_name):
    """
    Get or create the Django group that gives a role.

    The first group the mapping maps to ``role_name`` (the role's own name
    when no mapping is configured).

    Args:
        role_name: Name of the TurboDRF role.

    Returns:
        tuple: (Group object, created boolean)

    Raises:
        ValueError: If no group can give the role (strict mode, role
            not a mapping value).

    Example:
        admin_group, created = get_or_create_role_group('admin')
        if created:
            print("Created new admin group")
    """
    return Group.objects.get_or_create(name=_group_name_for_role(role_name))


def assign_roles_to_user(user, role_names):
    """
    Give a user exactly the roles ``role_names`` through group membership.

    Clears the user's existing groups, then adds the group that gives each
    role (see :func:`get_or_create_role_group`).

    Args:
        user: Django user object.
        role_names: List of role names to assign.

    Returns:
        list: List of Group objects the user was added to.

    Raises:
        ValueError: If no group can give one of the roles. The user's groups
            are left untouched then.

    Example:
        assign_roles_to_user(user, ['admin', 'editor'])
        # User is now in the groups that give 'admin' and 'editor'
    """
    # Resolve every role before touching the user's groups.
    group_names = [_group_name_for_role(role_name) for role_name in role_names]
    # Clear existing groups
    user.groups.clear()

    groups = []
    for name in group_names:
        group, _ = Group.objects.get_or_create(name=name)
        user.groups.add(group)
        groups.append(group)

    # Roles the integration resolver cached on this instance are stale now.
    from .roles import _CACHE_ATTR

    getattr(user, "__dict__", {}).pop(_CACHE_ATTR, None)
    return groups


def get_users_with_role(role_name):
    """
    Get all users the allauth integration gives a role.

    Members of any group that gives ``role_name`` under the current mapping
    and strict mode.

    Args:
        role_name: Name of the role to search for.

    Returns:
        QuerySet: Matching users (empty when no group gives the role).

    Example:
        admins = get_users_with_role('admin')
        for admin in admins:
            print(admin.username)
    """
    from django.contrib.auth import get_user_model

    User = get_user_model()
    group_names = _group_names_for_role(role_name)
    if not group_names:
        return User._default_manager.none()
    return User._default_manager.filter(groups__name__in=group_names).distinct()
