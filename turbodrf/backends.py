"""
Permission snapshot system for TurboDRF with caching support.

This module provides high-performance permission checking by building
policy snapshots once per request and caching them for reuse.
"""

from dataclasses import dataclass, field
from typing import Optional, Set

from django.conf import settings
from django.core.cache import cache


@dataclass
class PermissionSnapshot:
    """
    A snapshot of effective permissions for a user on a specific model.

    This dataclass provides O(1) permission checking using set membership.
    All permission checks are reduced to simple set lookups.

    Attributes:
        allowed_actions: Set of allowed model actions
            ('read', 'create', 'update', 'delete')
        readable_fields: Set of field names the user can read
        writable_fields: Set of field names the user can write
        fields_with_read_rules: Set of fields that have explicit read rules
        fields_with_write_rules: Set of fields that have explicit write rules

    Example:
        snapshot = PermissionSnapshot(
            allowed_actions={'read', 'update'},
            readable_fields={'title', 'content', 'author'},
            writable_fields={'title', 'content'},
            fields_with_read_rules=set(),  # No explicit read rules
            fields_with_write_rules={'price'}  # price has explicit write rule
        )

        # O(1) checks
        can_read = 'read' in snapshot.allowed_actions
        can_see_title = 'title' in snapshot.readable_fields
        can_write_title = 'title' in snapshot.writable_fields
    """

    allowed_actions: Set[str] = field(default_factory=set)
    readable_fields: Set[str] = field(default_factory=set)
    writable_fields: Set[str] = field(default_factory=set)
    fields_with_read_rules: Set[str] = field(default_factory=set)
    fields_with_write_rules: Set[str] = field(default_factory=set)

    def can_perform_action(self, action: str) -> bool:
        """Check if user can perform a model-level action."""
        return action in self.allowed_actions

    def can_read_field(self, field_name: str) -> bool:
        """Check if user can read a specific field."""
        return field_name in self.readable_fields

    def can_write_field(self, field_name: str) -> bool:
        """Check if user can write a specific field."""
        return field_name in self.writable_fields

    def has_read_rule(self, field_name: str) -> bool:
        """Check if field has explicit read permission rule."""
        return field_name in self.fields_with_read_rules

    def has_write_rule(self, field_name: str) -> bool:
        """Check if field has explicit write permission rule."""
        return field_name in self.fields_with_write_rules


def get_permission_mode() -> str:
    """
    Get the current permission mode from settings.

    Returns:
        str: One of 'static', 'django', or 'database'
    """
    return getattr(settings, "TURBODRF_PERMISSION_MODE", "static")


def get_user_roles(user) -> list:
    """
    Get roles for a user based on the current permission mode.

    Args:
        user: Django user object

    Returns:
        list: List of role names for the user
    """
    # Handle unauthenticated users
    if not user or not user.is_authenticated:
        # Check if guest role is configured
        mode = get_permission_mode()
        if mode == "static":
            from .settings import TURBODRF_ROLES as default_roles

            TURBODRF_ROLES = getattr(settings, "TURBODRF_ROLES", default_roles)
            if "guest" in TURBODRF_ROLES:
                return ["guest"]
        elif mode == "database":
            from .models import TurboDRFRole

            if TurboDRFRole.objects.filter(name="guest").exists():
                return ["guest"]
        return []

    mode = get_permission_mode()

    if mode == "database":
        # Get roles from UserRole table
        from .models import UserRole

        role_names = list(
            UserRole.objects.filter(user=user)
            .select_related("role")
            .values_list("role__name", flat=True)
        )
        return role_names
    elif hasattr(user, "roles"):
        # Use existing roles property (static mode or custom)
        return user.roles if isinstance(user.roles, list) else list(user.roles)
    elif hasattr(user, "_test_roles"):
        # Support test users with _test_roles property (for backward compatibility)
        return (
            user._test_roles
            if isinstance(user._test_roles, list)
            else list(user._test_roles)
        )
    else:
        return []


def build_permission_snapshot_static(user, model) -> PermissionSnapshot:
    """
    Build permission snapshot from static TURBODRF_ROLES configuration.

    This is the original TurboDRF permission system using settings.py

    Args:
        user: Django user object with 'roles' attribute
        model: Django model class

    Returns:
        PermissionSnapshot: Computed permission snapshot
    """
    from .settings import TURBODRF_ROLES as default_roles

    TURBODRF_ROLES = getattr(settings, "TURBODRF_ROLES", default_roles)

    snapshot = PermissionSnapshot()
    app_label = model._meta.app_label
    model_name = model._meta.model_name

    # Get all permissions for user's roles
    user_permissions = set()
    user_roles = get_user_roles(user)
    for role in user_roles:
        user_permissions.update(TURBODRF_ROLES.get(role, []))

    # Scan all roles ONCE to find fields with explicit rules for this model
    all_field_read_rules = set()
    all_field_write_rules = set()

    for role_name, role_perms in TURBODRF_ROLES.items():
        for perm in role_perms:
            parts = perm.split(".")
            if len(parts) == 4 and parts[0] == app_label and parts[1] == model_name:
                field_name = parts[2]
                perm_type = parts[3]
                if perm_type == "read":
                    all_field_read_rules.add(field_name)
                elif perm_type == "write":
                    all_field_write_rules.add(field_name)

    # Build allowed actions
    for action in ["read", "create", "update", "delete"]:
        perm = f"{app_label}.{model_name}.{action}"
        if perm in user_permissions:
            snapshot.allowed_actions.add(action)

    # Store fields with explicit rules
    snapshot.fields_with_read_rules = all_field_read_rules
    snapshot.fields_with_write_rules = all_field_write_rules

    # Build readable/writable fields
    # Get all model fields (including M2M fields)
    try:
        all_model_fields = {f.name for f in model._meta.fields}
        # Also include ManyToMany fields
        all_model_fields.update({f.name for f in model._meta.many_to_many})
    except (AttributeError, TypeError):
        # Mock model without proper _meta - use empty set
        all_model_fields = set()

    for field_name in all_model_fields:
        # Check readable: if explicit read rule exists, check it; else use model-level
        if field_name in all_field_read_rules:
            field_perm = f"{app_label}.{model_name}.{field_name}.read"
            if field_perm in user_permissions:
                snapshot.readable_fields.add(field_name)
        else:
            # No explicit read rule, fall back to model-level permission
            if "read" in snapshot.allowed_actions:
                snapshot.readable_fields.add(field_name)

        # Check writable: if explicit write rule exists, check it; else use model-level
        if field_name in all_field_write_rules:
            field_perm = f"{app_label}.{model_name}.{field_name}.write"
            if field_perm in user_permissions:
                snapshot.writable_fields.add(field_name)
        else:
            # No explicit write rule, fall back to model-level permission
            if (
                "create" in snapshot.allowed_actions
                or "update" in snapshot.allowed_actions
            ):
                snapshot.writable_fields.add(field_name)

    return snapshot


def build_permission_snapshot_database(user, model) -> PermissionSnapshot:
    """
    Build permission snapshot from database-backed permissions.

    This queries the TurboDRFRole and TurboDRFPermission tables
    to compute effective permissions.

    Args:
        user: Django user object
        model: Django model class

    Returns:
        PermissionSnapshot: Computed permission snapshot
    """
    from .models import RolePermission, TurboDRFRole

    snapshot = PermissionSnapshot()
    app_label = model._meta.app_label
    model_name = model._meta.model_name

    # Get user's roles
    user_roles = get_user_roles(user)
    if not user_roles:
        return snapshot

    # Query all permissions for this user's roles and this model
    # Single database query for all permissions

    role_ids = TurboDRFRole.objects.filter(name__in=user_roles).values_list(
        "id", flat=True
    )

    permissions = RolePermission.objects.filter(
        role_id__in=role_ids, app_label=app_label, model_name=model_name
    ).select_related("role")

    # Track which fields have explicit rules (scan all permissions once)
    all_field_read_rules = set()
    all_field_write_rules = set()

    # First pass: collect model-level permissions and field rules
    user_field_perms = set()
    for perm in permissions:
        if perm.action:
            # Model-level permission
            snapshot.allowed_actions.add(perm.action)
        elif perm.field_name and perm.permission_type:
            # Field-level permission
            if perm.permission_type == "read":
                all_field_read_rules.add(perm.field_name)
                user_field_perms.add(f"{perm.field_name}.read")
            elif perm.permission_type == "write":
                all_field_write_rules.add(perm.field_name)
                user_field_perms.add(f"{perm.field_name}.write")

    # Store fields with explicit rules
    snapshot.fields_with_read_rules = all_field_read_rules
    snapshot.fields_with_write_rules = all_field_write_rules

    # Second pass: build readable/writable fields
    try:
        # Get all model fields (including M2M fields)
        all_model_fields = {f.name for f in model._meta.fields}
        # Also include ManyToMany fields
        all_model_fields.update({f.name for f in model._meta.many_to_many})
    except (AttributeError, TypeError):
        # Mock model without proper _meta - use empty set
        all_model_fields = set()

    for field_name in all_model_fields:
        # Check readable: if explicit read rule exists, check it; else use model-level
        if field_name in all_field_read_rules:
            if f"{field_name}.read" in user_field_perms:
                snapshot.readable_fields.add(field_name)
        else:
            # No explicit read rule, fall back to model-level permission
            if "read" in snapshot.allowed_actions:
                snapshot.readable_fields.add(field_name)

        # Check writable: if explicit write rule exists, check it; else use model-level
        if field_name in all_field_write_rules:
            if f"{field_name}.write" in user_field_perms:
                snapshot.writable_fields.add(field_name)
        else:
            # No explicit write rule, fall back to model-level permission
            if (
                "create" in snapshot.allowed_actions
                or "update" in snapshot.allowed_actions
            ):
                snapshot.writable_fields.add(field_name)

    return snapshot


def get_cache_key(user, model) -> str:
    """
    Generate cache key for permission snapshot.

    Args:
        user: Django user object
        model: Django model class

    Returns:
        str: Cache key
    """
    cache_prefix = getattr(
        settings, "TURBODRF_PERMISSION_CACHE_PREFIX", "turbodrf_perm"
    )
    user_id = (
        getattr(user, "id", "mock") if user and user.is_authenticated else "anonymous"
    )
    app_label = model._meta.app_label
    model_name = model._meta.model_name

    # Include role versions for cache invalidation
    mode = get_permission_mode()
    if mode == "database":
        from .models import TurboDRFRole

        # Get version numbers for all user's roles
        user_roles = get_user_roles(user)
        if user_roles:
            versions = TurboDRFRole.objects.filter(name__in=user_roles).values_list(
                "version", flat=True
            )
            version_hash = hash(tuple(sorted(versions)))
        else:
            version_hash = 0
    else:
        # For static mode, use a simple hash of TURBODRF_ROLES
        # This doesn't change at runtime, so version is always 1
        version_hash = 1

    return f"{cache_prefix}:{user_id}:{app_label}:{model_name}:{version_hash}"


def get_cached_snapshot(user, model) -> Optional[PermissionSnapshot]:
    """
    Get cached permission snapshot if available.

    Args:
        user: Django user object
        model: Django model class

    Returns:
        PermissionSnapshot or None if not cached
    """
    cache_key = get_cache_key(user, model)
    return cache.get(cache_key)


def set_cached_snapshot(user, model, snapshot: PermissionSnapshot):
    """
    Cache permission snapshot.

    Args:
        user: Django user object
        model: Django model class
        snapshot: PermissionSnapshot to cache
    """
    cache_key = get_cache_key(user, model)
    cache_timeout = getattr(
        settings, "TURBODRF_PERMISSION_CACHE_TIMEOUT", 300
    )  # 5 minutes default
    cache.set(cache_key, snapshot, cache_timeout)


def build_permission_snapshot(user, model, use_cache=True) -> PermissionSnapshot:
    """
    Build permission snapshot for a user and model.

    This is the main entry point for permission snapshot creation.
    It handles caching and delegates to the appropriate builder based
    on the permission mode.

    Args:
        user: Django user object
        model: Django model class
        use_cache: Whether to use caching (default: True)

    Returns:
        PermissionSnapshot: Computed or cached permission snapshot

    Example:
        snapshot = build_permission_snapshot(request.user, Article)
        if snapshot.can_perform_action('read'):
            # User can read articles
            pass
    """
    # Try cache first
    if use_cache:
        cached = get_cached_snapshot(user, model)
        if cached is not None:
            return cached

    # Build snapshot based on permission mode
    mode = get_permission_mode()

    if mode == "database":
        snapshot = build_permission_snapshot_database(user, model)
    else:
        # static mode (or fallback)
        snapshot = build_permission_snapshot_static(user, model)

    # Cache the result
    if use_cache:
        set_cached_snapshot(user, model, snapshot)

    return snapshot


def attach_snapshot_to_request(request, model):
    """
    Attach permission snapshot to request for reuse within the same request.

    This provides request-level caching so the snapshot is built once
    per request even if cache is disabled.

    Args:
        request: Django request object
        model: Django model class
    """
    if not hasattr(request, "_turbodrf_snapshots"):
        request._turbodrf_snapshots = {}

    cache_key = f"{model._meta.app_label}.{model._meta.model_name}"

    if cache_key not in request._turbodrf_snapshots:
        snapshot = build_permission_snapshot(request.user, model)
        request._turbodrf_snapshots[cache_key] = snapshot

    return request._turbodrf_snapshots[cache_key]


def get_snapshot_from_request(request, model) -> Optional[PermissionSnapshot]:
    """
    Get permission snapshot from request cache.

    Args:
        request: Django request object
        model: Django model class

    Returns:
        PermissionSnapshot or None if not cached on request
    """
    if not hasattr(request, "_turbodrf_snapshots"):
        return None

    cache_key = f"{model._meta.app_label}.{model._meta.model_name}"
    return request._turbodrf_snapshots.get(cache_key)
