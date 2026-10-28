"""
Permission snapshot system for TurboDRF with caching support.

This module provides high-performance permission checking by building
policy snapshots once per request and caching them for reuse.
"""

import contextvars
import hashlib
import secrets
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional, Set

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured

from .scope import TENANT, current_scope, is_platform

# Per-request memo of roles, cache-key parts and snapshots (see
# _permission_memo). None outside a request: every lookup then goes to the
# cache / database, as before.
_request_memo = contextvars.ContextVar("turbodrf_permission_memo", default=None)


@contextmanager
def _permission_memo():
    """Memoise permission lookups for the duration of one request.

    A request checks the same user's roles and snapshots many times (once per
    field, filter hop, search field...). Inside this context each is computed
    once: in database mode that's one role query instead of one per check, and
    a snapshot is one cache read instead of a key hash plus two round trips per
    check. TurboDRFViewSet.dispatch opens it, so a role or permission change
    still takes effect on the next request."""
    token = _request_memo.set({})
    try:
        yield
    finally:
        _request_memo.reset(token)


def _memoised(key, owner, compute):
    """``compute()``, memoised under ``key`` for the current request. ``owner``
    (a user, or None) is held with the value: an id() in ``key`` can't be
    reused by another object while the memo lives."""
    memo = _request_memo.get()
    if memo is None:
        return compute()
    entry = memo.get(key)
    if entry is not None and entry[0] is owner:
        return entry[1]
    value = compute()
    memo[key] = (owner, value)
    return value


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
        str: 'static' or 'database'
    """
    return getattr(settings, "TURBODRF_PERMISSION_MODE", "static")


class RoleOnlyIdentity:
    """A permission-only caller holding just a list of role names: the
    schema's ``?role=`` narrowing. No primary key, so its snapshots are never
    cached. It holds the same roles on both scopes: ``roles`` on the tenant
    scope (static mode) and ``platform_roles`` on the platform scope, whatever
    TURBODRF_PLATFORM_ROLE_GETTER reads for a real user."""

    is_authenticated = True
    is_anonymous = False
    pk = None

    def __init__(self, roles):
        self.roles = list(roles)
        self.platform_roles = list(roles)


def get_user_roles(user) -> list:
    """
    Get roles for a user on the current scope: on the tenant scope by the
    permission mode, on the platform scope from
    TURBODRF_PLATFORM_ROLE_GETTER. A role from one never applies on the other.

    Args:
        user: Django user object

    Returns:
        list: List of role names for the user
    """
    resolve = _resolve_platform_roles if is_platform() else _resolve_user_roles
    roles = _memoised(("roles", current_scope(), id(user)), user, lambda: resolve(user))
    return list(roles)


def _default_platform_roles(user):
    """The default TURBODRF_PLATFORM_ROLE_GETTER: ``user.platform_roles``."""
    return getattr(user, "platform_roles", None)


_role_getters = {}


def platform_role_getter():
    """The callable TURBODRF_PLATFORM_ROLE_GETTER names (a dotted path or a
    callable ``user -> list[str]``); ``user.platform_roles`` by default."""
    from django.utils.module_loading import import_string

    setting = getattr(settings, "TURBODRF_PLATFORM_ROLE_GETTER", None)
    if setting is None:
        return _default_platform_roles
    if callable(setting):
        return setting
    if not isinstance(setting, str):
        raise ImproperlyConfigured(
            f"TURBODRF_PLATFORM_ROLE_GETTER must be a dotted path or a callable "
            f"(user -> list of role names); got {type(setting).__name__}."
        )
    getter = _role_getters.get(setting)
    if getter is None:
        try:
            getter = import_string(setting)
        except ImportError as exc:
            raise ImproperlyConfigured(
                f"TURBODRF_PLATFORM_ROLE_GETTER={setting!r} can't be imported: {exc}"
            ) from exc
        if not callable(getter):
            raise ImproperlyConfigured(
                f"TURBODRF_PLATFORM_ROLE_GETTER={setting!r} isn't callable."
            )
        _role_getters[setting] = getter
    return getter


def platform_roles_config():
    """TURBODRF_PLATFORM_ROLES (role -> permission strings), or ``{}``."""
    roles = getattr(settings, "TURBODRF_PLATFORM_ROLES", None)
    return roles if isinstance(roles, dict) else {}


def _resolve_platform_roles(user) -> list:
    """Platform roles: none for an anonymous caller (there is no platform
    guest role), else whatever TURBODRF_PLATFORM_ROLE_GETTER returns."""
    if not user or not getattr(user, "is_authenticated", False):
        return []
    if isinstance(user, RoleOnlyIdentity):
        return list(user.platform_roles)
    return _role_names(
        platform_role_getter()(user), what="TURBODRF_PLATFORM_ROLE_GETTER"
    )


def _resolve_user_roles(user) -> list:
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
        return _role_names(user.roles)
    elif hasattr(user, "_test_roles"):
        return _role_names(user._test_roles)
    else:
        return []


def _role_names(roles, what="user.roles") -> list:
    """``user.roles`` (or ``what``) as a list of role names.

    It may be None (a custom user model that hasn't set it), a list or any
    iterable of names, or a single name. A related manager / queryset of role
    objects gives their ``name`` field. Anything else is a configuration error:
    raise rather than guess (a string iterated as characters would grant every
    single-letter role)."""
    from django.db.models import Manager, QuerySet

    if isinstance(roles, (Manager, QuerySet)):
        try:
            roles.model._meta.get_field("name")
        except Exception:
            raise ImproperlyConfigured(
                f"{what} is a manager of {roles.model.__name__}, which has "
                f"no 'name' field. Expose the role names as a list, e.g. a "
                f"property returning list(...values_list('<field>', flat=True))."
            ) from None
        return list(roles.values_list("name", flat=True))
    if not roles:
        return []
    if isinstance(roles, str):
        return [roles]
    if isinstance(roles, list):
        return roles
    try:
        return list(roles)
    except TypeError:
        raise ImproperlyConfigured(
            f"{what} must be a list of role names, not {type(roles).__name__}."
        ) from None


def _grants_public_read(user, model) -> bool:
    """``public_access`` gives anonymous callers model-level read. Field rules
    still apply (a field with a read rule needs that grant — from the guest
    role, if one exists), so anonymous callers see what any role with only
    model-level read sees."""
    if is_platform():
        return False  # the platform scope has no anonymous access
    if user is not None and getattr(user, "is_authenticated", False):
        return False
    try:
        config = model.turbodrf()
    except Exception:
        return False
    return isinstance(config, dict) and bool(config.get("public_access"))


def _model_restricted_fields(model) -> Set[str]:
    """Fields the model declares in ``sensitive_fields``: readable only through
    an explicit field read rule (``app.model.field.read``), never through
    model-level read."""
    declared = getattr(model, "sensitive_fields", None) or ()
    if isinstance(declared, str):
        declared = [declared]
    return set(declared)


def build_permission_snapshot_static(user, model) -> PermissionSnapshot:
    """
    Build permission snapshot from static TURBODRF_ROLES configuration.

    This is the original TurboDRF permission system using settings.py

    On the platform scope the roles are the caller's platform roles and the
    map is TURBODRF_PLATFORM_ROLES, whatever the permission mode.

    Args:
        user: Django user object with 'roles' attribute
        model: Django model class

    Returns:
        PermissionSnapshot: Computed permission snapshot
    """
    TURBODRF_ROLES = _static_roles_config()

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

    all_field_read_rules |= _model_restricted_fields(model)

    # Build allowed actions
    for action in ["read", "create", "update", "delete"]:
        perm = f"{app_label}.{model_name}.{action}"
        if perm in user_permissions:
            snapshot.allowed_actions.add(action)
    if _grants_public_read(user, model):
        snapshot.allowed_actions.add("read")

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


def _static_roles_config():
    """The static role map of the current scope."""
    if is_platform():
        return platform_roles_config()
    from .settings import TURBODRF_ROLES as default_roles

    return getattr(settings, "TURBODRF_ROLES", default_roles)


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
    if not user_roles and not _grants_public_read(user, model):
        return snapshot

    # Query all permissions for this user's roles and this model
    # Single database query for all permissions

    role_ids = TurboDRFRole.objects.filter(name__in=user_roles).values_list(
        "id", flat=True
    )

    permissions = RolePermission.objects.filter(
        role_id__in=role_ids, app_label=app_label, model_name=model_name
    ).select_related("role")

    # A field with an explicit rule on ANY role is readable / writable only
    # through that rule — as in static mode. Collecting rules from the user's
    # own roles only let a role with no field rules read and write every field
    # another role had restricted.
    all_field_read_rules = set()
    all_field_write_rules = set()
    for field_name, permission_type in RolePermission.objects.filter(
        app_label=app_label, model_name=model_name, field_name__isnull=False
    ).values_list("field_name", "permission_type"):
        if permission_type == "read":
            all_field_read_rules.add(field_name)
        elif permission_type == "write":
            all_field_write_rules.add(field_name)
    all_field_read_rules |= _model_restricted_fields(model)

    # Collect the user's model-level permissions and field grants
    user_field_perms = set()
    for perm in permissions:
        if perm.action:
            # Model-level permission
            snapshot.allowed_actions.add(perm.action)
        elif perm.field_name and perm.permission_type:
            user_field_perms.add(f"{perm.field_name}.{perm.permission_type}")
    if _grants_public_read(user, model):
        snapshot.allowed_actions.add("read")

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


def get_cache_key(user, model) -> Optional[str]:
    """
    Generate cache key for a user's permission snapshot.

    Args:
        user: Django user object
        model: Django model class

    Returns:
        str: the cache key, or ``None`` when the snapshot must not be cached
        (an authenticated user with no persistent primary key). Callers treat
        ``None`` as "build fresh, do not read or write the cache".
    """
    cache_prefix = getattr(
        settings, "TURBODRF_PERMISSION_CACHE_PREFIX", "turbodrf_perm"
    )
    scope = current_scope()

    if user and user.is_authenticated:
        # Key on `.pk`, NEVER `.id`. A custom user model with a non-'id'
        # primary key (e.g. a UUID pk) has no `.id` attribute, so the old
        # `getattr(user, "id", "mock")` silently collapsed EVERY such user to
        # one shared sentinel — one identity's action/field snapshot then
        # served to all others until the TTL expired (privilege escalation,
        # OWASP A01). `.pk` resolves the real primary key on every Django
        # model regardless of the field's name.
        user_id = getattr(user, "pk", None)
        if user_id is None:
            # Authenticated but no persistent identity (unsaved / mock user).
            # Refuse to cache rather than fall back to a shared sentinel —
            # sharing one snapshot across distinct identities is exactly the
            # bug being fixed here.
            return None
    else:
        # All anonymous callers legitimately share one identity: they resolve
        # to the same guest/none role, and row-level visibility (predicates)
        # is never cached in the snapshot.
        user_id = "anonymous"
    app_label = model._meta.app_label
    model_name = model._meta.model_name

    # The user's own assigned roles are part of the key so a runtime role
    # change invalidates the cached snapshot — not only a change to the global
    # role->permission map.
    user_roles = get_user_roles(user)

    # The role configuration (as a digest, computed once per request: a large
    # role map is slow to repr, and every key folds it in); bumped by
    # invalidate_user_permissions(): a new generation is a new key.
    sig = (
        _memoised(
            ("roles_signature", scope),
            None,
            lambda: hashlib.sha256(repr(_roles_signature()).encode()).hexdigest(),
        ),
        _memoised(
            ("generations", user_id),
            None,
            lambda: _generation_tokens(cache_prefix, user_id),
        ),
        _model_signature(model),
    )
    if scope != TENANT:
        sig += (scope,)

    # hashlib (not builtin hash()) so the key is stable across processes.
    # Builtin hash() is PYTHONHASHSEED-randomized per worker, which fragments a
    # shared cache (each Gunicorn worker computing a different key for the same
    # config, silently collapsing the hit rate).
    version_hash = hashlib.sha256(
        repr((tuple(sorted(user_roles)), sig)).encode("utf-8")
    ).hexdigest()[:16]

    # A platform snapshot never shares a key with a tenant one (the tenant
    # key keeps its pre-0.8 form).
    scope_part = "" if scope == TENANT else f"{scope}:"
    return (
        f"{cache_prefix}:{scope_part}{user_id}:{app_label}:{model_name}:"
        f"{version_hash}"
    )


def _roles_signature():
    """The role configuration every snapshot depends on."""
    if is_platform():
        return _static_signature(platform_roles_config())
    if get_permission_mode() == "database":
        from .models import TurboDRFRole

        # (name, version) of EVERY role, not only the user's, and for anonymous
        # callers too: a field rule added to any role restricts that field for
        # all roles (and for anonymous callers on a public_access model), so it
        # must invalidate every cached snapshot. Role identity is included so
        # two roles that share a version number don't produce the same key.
        return tuple(sorted(TurboDRFRole.objects.values_list("name", "version")))
    # Static mode: fold in TURBODRF_ROLES so the key changes when the role
    # configuration changes — including override_settings(TURBODRF_ROLES=...)
    # in tests. Without this a snapshot built under restricted roles in one
    # test pollutes the cache for later tests reusing the same user.pk.
    return _static_signature(getattr(settings, "TURBODRF_ROLES", {}))


def _static_signature(roles):
    try:
        return tuple(
            sorted((role, tuple(sorted(perms))) for role, perms in roles.items())
        )
    except (TypeError, AttributeError):
        return ("__unhashable__",)


def _model_signature(model):
    """The model's own declarations a snapshot depends on, so a deploy that
    changes them (with a cache shared across the rolling deploy) can't serve a
    snapshot built under the old ones."""
    try:
        config = model.turbodrf()
    except Exception:
        config = None
    public = (
        not is_platform()
        and isinstance(config, dict)
        and bool(config.get("public_access"))
    )
    return (tuple(sorted(_model_restricted_fields(model))), public)


def _generation_key(cache_prefix, user_id):
    scope = "all" if user_id is None else user_id
    return f"{cache_prefix}:generation:{scope}"


def _generation_tokens(cache_prefix, user_id):
    """The current global and per-user generation tokens. A missing token is
    seeded with a fresh random one: a token evicted from the cache must not
    revert keys to their pre-invalidation form (and so to stale snapshots
    still cached under it)."""
    keys = [_generation_key(cache_prefix, None), _generation_key(cache_prefix, user_id)]
    found = cache.get_many(keys)
    tokens = []
    for key in keys:
        token = found.get(key)
        if token is None:
            token = secrets.token_hex(8)
            if not cache.add(key, token, None):
                # Another process seeded it first.
                token = cache.get(key, token)
        tokens.append(token)
    return tuple(tokens)


def invalidate_user_permissions(user=None):
    """Drop cached permission snapshots: one user's (an anonymous user means
    every anonymous caller), or everyone's when ``user`` is None. Snapshots
    already pick up role changes on their own (the cache key covers the user's
    roles and every role's configuration); use this after changing permissions
    some other way — a bulk ``RolePermission`` QuerySet ``update()`` /
    ``delete()``, say."""
    cache_prefix = getattr(
        settings, "TURBODRF_PERMISSION_CACHE_PREFIX", "turbodrf_perm"
    )
    if user is None:
        key = _generation_key(cache_prefix, None)
    elif not getattr(user, "is_authenticated", False):
        key = _generation_key(cache_prefix, "anonymous")
    else:
        user_id = getattr(user, "pk", None)
        if user_id is None:
            return  # never cached (see get_cache_key)
        key = _generation_key(cache_prefix, user_id)
    # A fresh token, never a counter: an evicted counter restarted from the
    # same value would bring back keys of stale snapshots.
    cache.set(key, secrets.token_hex(8), None)
    memo = _request_memo.get()
    if memo is not None:
        memo.clear()


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
    if cache_key is None:
        return None
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
    if cache_key is None:
        # Uncacheable identity (authenticated, no pk) — never persist a
        # snapshot under a shared key.
        return
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
    if not use_cache:
        return _build_snapshot(user, model)
    return _memoised(
        ("snapshot", current_scope(), id(user), model),
        user,
        lambda: _cached_snapshot(user, model),
    )


def _build_snapshot(user, model):
    # The platform scope is always static: TURBODRF_PLATFORM_ROLES.
    if not is_platform() and get_permission_mode() == "database":
        return build_permission_snapshot_database(user, model)
    # static mode (or fallback)
    return build_permission_snapshot_static(user, model)


def _cached_snapshot(user, model):
    """The snapshot from the cache, or built and cached (the key computed
    once)."""
    cache_key = get_cache_key(user, model)
    if cache_key is not None:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    snapshot = _build_snapshot(user, model)
    if cache_key is not None:
        cache.set(
            cache_key,
            snapshot,
            getattr(settings, "TURBODRF_PERMISSION_CACHE_TIMEOUT", 300),
        )
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

    cache_key = request_snapshot_key(model)

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

    return request._turbodrf_snapshots.get(request_snapshot_key(model))


def request_snapshot_key(model):
    """The key of ``model``'s snapshot in a request's ``_turbodrf_snapshots``
    on the current scope."""
    key = f"{model._meta.app_label}.{model._meta.model_name}"
    scope = current_scope()
    return key if scope == TENANT else f"{scope}:{key}"
