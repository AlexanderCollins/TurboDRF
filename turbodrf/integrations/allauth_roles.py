"""
Role mapping utilities for TurboDRF django-allauth integration.

Provides helper functions for creating and managing role groups,
syncing groups to roles, and validating role mappings.
"""

from django.contrib.auth.models import Group


def create_role_groups(role_names):
    """
    Create Django groups for standard TurboDRF roles.

    This helper creates Group objects for each role name provided.
    If a group already exists, it is skipped.

    Args:
        role_names: List of role names to create groups for.

    Returns:
        list: List of created/existing Group objects.

    Example:
        groups = create_role_groups(['admin', 'editor', 'viewer'])
        # Creates 3 groups if they don't exist
    """
    created_groups = []

    for role_name in role_names:
        group, created = Group.objects.get_or_create(name=role_name)
        created_groups.append(group)

    return created_groups


def sync_groups_to_roles(user):
    """
    Sync a user's Django groups to TurboDRF role format.

    This is a convenience function that extracts role names from
    a user's group membership.

    Args:
        user: Django user object with groups relationship.

    Returns:
        list: List of role names (group names).

    Example:
        user.groups.add(Group.objects.get(name='admin'))
        roles = sync_groups_to_roles(user)  # ['admin']
    """
    return [group.name for group in user.groups.all()]


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
    Get or create a Django group for a role.

    Convenience function for getting/creating a single group.

    Args:
        role_name: Name of the role/group.

    Returns:
        tuple: (Group object, created boolean)

    Example:
        admin_group, created = get_or_create_role_group('admin')
        if created:
            print("Created new admin group")
    """
    return Group.objects.get_or_create(name=role_name)


def assign_roles_to_user(user, role_names):
    """
    Assign roles to a user by adding them to corresponding groups.

    This helper clears existing groups and assigns new ones based on
    the provided role names.

    Args:
        user: Django user object.
        role_names: List of role names to assign.

    Returns:
        list: List of Group objects the user was added to.

    Example:
        assign_roles_to_user(user, ['admin', 'editor'])
        # User is now in 'admin' and 'editor' groups
    """
    # Clear existing groups
    user.groups.clear()

    # Create/get groups and add user
    groups = []
    for role_name in role_names:
        group, _ = get_or_create_role_group(role_name)
        user.groups.add(group)
        groups.append(group)

    return groups


def get_users_with_role(role_name):
    """
    Get all users with a specific role.

    Args:
        role_name: Name of the role to search for.

    Returns:
        QuerySet: Users in the group with the given role name.

    Example:
        admins = get_users_with_role('admin')
        for admin in admins:
            print(admin.username)
    """
    try:
        group = Group.objects.get(name=role_name)
        return group.user_set.all()
    except Group.DoesNotExist:
        # Return empty queryset if group doesn't exist
        from django.contrib.auth import get_user_model

        User = get_user_model()
        return User.objects.none()
