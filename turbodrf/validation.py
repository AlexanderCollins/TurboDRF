"""
Validation utilities for TurboDRF nested fields and filters.

This module provides utilities for validating nesting depth and traversing
Django ORM relationships to check permissions at each level.
"""

import logging

from django.conf import settings
from django.core.exceptions import FieldDoesNotExist, ValidationError

logger = logging.getLogger(__name__)


def get_max_nesting_depth():
    """
    Get the maximum nesting depth from settings.

    Returns:
        int or None: Maximum nesting depth, or None for unlimited
    """
    from .settings import TURBODRF_MAX_NESTING_DEPTH as default_depth

    return getattr(settings, "TURBODRF_MAX_NESTING_DEPTH", default_depth)


def validate_nesting_depth(field_name, max_depth=None):
    """
    Validate that a field name doesn't exceed the maximum nesting depth.

    Args:
        field_name: Field name potentially with __ notation
            (e.g., 'author__publisher__name')
        max_depth: Maximum allowed depth, or None to use setting

    Returns:
        bool: True if valid

    Raises:
        ValidationError: If nesting depth exceeds maximum

    Examples:
        >>> validate_nesting_depth('title')  # depth 0
        True
        >>> validate_nesting_depth('author__name')  # depth 1
        True
        >>> validate_nesting_depth('author__publisher__name')  # depth 2
        True
        >>> validate_nesting_depth('a__b__c__d')  # depth 3
        True
        >>> validate_nesting_depth('a__b__c__d__e')  # depth 4 - EXCEEDS DEFAULT
        ValidationError
    """
    if max_depth is None:
        max_depth = get_max_nesting_depth()

    # If max_depth is None, unlimited nesting is allowed
    if max_depth is None:
        return True

    # Count the number of __ separators to determine nesting depth
    depth = field_name.count("__")

    if depth > max_depth:
        raise ValidationError(
            f"Field '{field_name}' exceeds maximum nesting depth of {max_depth}. "
            f"Current depth: {depth}. "
            f"WARNING: Increasing TURBODRF_MAX_NESTING_DEPTH beyond 3 is "
            f"UNSUPPORTED and may cause performance issues, security risks, "
            f"and unexpected behavior."
        )

    return True


def get_nested_field_model(model, field_path):
    """
    Traverse a nested field path and return the final model and field info.

    Args:
        model: Starting Django model class
        field_path: Field path with __ notation (e.g., 'author__publisher__name')

    Returns:
        tuple: (final_model, field_chain)
            - final_model: The model class of the final field
            - field_chain: List of (model, field, field_name) tuples for each step

    Raises:
        FieldDoesNotExist: If any field in the path doesn't exist

    Example:
        >>> model, chain = get_nested_field_model(Book, 'author__publisher__name')
        >>> # Returns: (Publisher, [
        >>> #   (Book, ForeignKey, 'author'),
        >>> #   (Author, ForeignKey, 'publisher'),
        >>> #   (Publisher, CharField, 'name')
        >>> # ])
    """
    parts = field_path.split("__")
    field_chain = []
    current_model = model

    for part in parts:
        try:
            field = current_model._meta.get_field(part)
            field_chain.append((current_model, field, part))

            # If this is a relational field, get the related model
            if hasattr(field, "related_model") and field.related_model:
                current_model = field.related_model
            # For the final field, keep the current model
        except FieldDoesNotExist:
            raise FieldDoesNotExist(
                f"Field '{part}' does not exist on model {current_model.__name__}"
            )

    return current_model, field_chain


def validate_searchable_fields_safety(model):
    """Refuse to boot if a model's ``searchable_fields`` traverse into a
    target whose predicates / tenant_field DRF's ``SearchFilter`` does
    not apply to the join.

    DRF's ``SearchFilter`` generates ``WHERE <path> ILIKE ...`` joined to
    the target model. The parent rows are already tenant + predicate
    scoped via ``get_queryset()``, but the JOIN to the target does not
    apply the target's own visibility rules. A search query can match
    against target rows the user cannot see via the target's own
    endpoint — leaking row existence and partial column values via
    substring inference (``?search=secret``).

    Same class of bug as :func:`turbodrf.compiler.validate_compiled_path_safety`.
    See ``docs/security.md`` for the full bug class.

    Detection is fully static: walk every ``searchable_fields`` entry
    that contains ``__``; for every model along the chain (excluding
    the parent itself), look up registered predicates and tenant_field;
    refuse to start if any link is unsafe.

    Escape hatches (raised in the error message):
        * Drop the ``__``-path from ``searchable_fields`` (use only flat
          fields on the parent model).
        * Set ``TURBODRF_ALLOW_UNSAFE_SEARCH_FIELDS = True`` to bypass
          (logs a loud warning per offending entry; for migrations
          only).
    """
    from django.conf import settings as _s

    from .predicates import get_predicates, get_tenant_field

    from .mixins import get_searchable_fields

    searchable = get_searchable_fields(model)
    if not searchable:
        return

    allow_unsafe = getattr(_s, "TURBODRF_ALLOW_UNSAFE_SEARCH_FIELDS", False)
    parent_tenant = get_tenant_field(model)

    for path in searchable:
        if not isinstance(path, str) or "__" not in path:
            continue
        try:
            _final_model, chain = get_nested_field_model(model, path)
        except FieldDoesNotExist:
            # Resolvability check at views.py:_is_resolvable_search_path
            # already silently drops these at request time; skip here.
            continue

        # Walk the chain. Skip the parent itself (chain[0]) — the unsafe
        # case is the JOIN target, not the source. Each subsequent
        # entry's `model` field is the model the previous FK pointed at.
        offending = []
        for step_idx, (step_model, _field, _name) in enumerate(chain):
            if step_idx == 0:
                continue
            target_predicates = get_predicates(step_model)
            target_tenant = get_tenant_field(step_model)

            unsafe_predicates = bool(target_predicates)
            unsafe_tenant_drift = target_tenant is not None and parent_tenant is None
            if unsafe_predicates or unsafe_tenant_drift:
                reasons = []
                if unsafe_predicates:
                    reasons.append(
                        f"{step_model.__name__} has "
                        f"{len(target_predicates)} registered predicate(s) "
                        f"({', '.join(type(p).__name__ for p in target_predicates)}) "
                        f"that DRF's SearchFilter does not apply to the join"
                    )
                if unsafe_tenant_drift:
                    reasons.append(
                        f"{step_model.__name__} declares "
                        f"tenant_field={target_tenant!r} but "
                        f"{model.__name__} is shared (no tenant_field) — "
                        f"a shared parent searching tenanted rows leaks "
                        f"across tenants"
                    )
                offending.append((step_model, reasons))

        if not offending:
            continue

        joined = "; ".join(f"[{m.__name__}] {' / '.join(r)}" for m, r in offending)
        message = (
            f"{model.__name__}.searchable_fields contains "
            f"'{path}', but: {joined}.\n\n"
            f"This is the same data-leak class as the compiled M2M "
            f"target bypass — the search query joins to "
            f"{offending[-1][0].__name__} without applying its own "
            f"visibility rules, so '?search=secret' can match against "
            f"rows the caller cannot see via the "
            f"{offending[-1][0].__name__} endpoint. See "
            f"docs/security.md#search-field-target-bypass.\n\n"
            f"Fix one of:\n"
            f"  • Drop '{path}' from {model.__name__}.searchable_fields "
            f"(use only flat fields on {model.__name__} itself).\n"
            f"  • Remove predicates / tenant_field from "
            f"{offending[-1][0].__name__} only if it is genuinely "
            f"public reference data with no row-level rules.\n"
            f"  • Set TURBODRF_ALLOW_UNSAFE_SEARCH_FIELDS=True to "
            f"bypass this gate (NOT recommended; logs a warning)."
        )

        if allow_unsafe:
            logger.warning(
                "TURBODRF_ALLOW_UNSAFE_SEARCH_FIELDS=True — bypassing "
                "search-field safety gate. %s",
                message,
            )
            continue

        from django.core.exceptions import ImproperlyConfigured

        raise ImproperlyConfigured(message)


def path_traverses_predicate_target(parent_model, field_path):
    """True if the JOIN chain for ``field_path`` passes through a model
    with registered predicates or tenant-drift relative to the parent.

    Used at REQUEST time by :func:`build_traversal_scope_q` to decide
    whether a ``__``-path filter / ordering needs the target-scoping
    AND clause. Same bug class as the compiled M2M / search-fields
    startup gates, applied at request time because URL-driven JOINs
    aren't statically enumerable.

    Returns False on unresolvable paths so normal validation can
    surface the error.
    """
    from .predicates import get_predicates, get_tenant_field

    parent_tenant = get_tenant_field(parent_model)
    try:
        _final_model, chain = get_nested_field_model(parent_model, field_path)
    except FieldDoesNotExist:
        return False

    for step_idx, (step_model, _field, _name) in enumerate(chain):
        if step_idx == 0:
            continue
        if get_predicates(step_model):
            return True
        target_tenant = get_tenant_field(step_model)
        if target_tenant is not None and parent_tenant is None:
            return True
    return False


def build_traversal_scope_q(parent_model, field_path, request):
    """Build a Q that scopes every JOIN target along ``field_path`` to
    the rows the request's user can see via that target's own endpoint.

    For each model in the JOIN chain that has registered predicates or
    a ``tenant_field``, this AND's
    ``<prefix>__pk__in=<TargetModel.objects.filter(<target_q>)>`` onto
    the parent queryset, where ``target_q`` mirrors the target view's
    tenant_q + predicate_q construction (see
    :meth:`turbodrf.views.TurboDRFViewSet._get_tenant_q` and
    :meth:`._get_predicate_q`).

    Used at request time to wrap URL-driven JOINs (``?fk__field=...``,
    ``?ordering=fk__field``) so the JOIN can't render rows the caller
    cannot see via the target's own endpoint — same bug class as the
    compiled M2M / search-field bypasses, but URL-driven so it can't be
    gated at startup.

    Returns ``Q()`` (no-op) for paths whose chain is fully unscoped, or
    when no ``__`` is present in the path.
    """
    from django.db.models import Q

    from .backends import get_user_roles
    from .predicates import (
        get_predicates,
        get_tenant_field,
        get_user_tenant,
    )

    if not isinstance(field_path, str) or "__" not in field_path:
        return Q()

    try:
        _final, chain = get_nested_field_model(parent_model, field_path)
    except FieldDoesNotExist:
        return Q()

    user = getattr(request, "user", None) if request is not None else None
    user_roles = set(get_user_roles(user)) if user is not None else set()
    parts = field_path.split("__")

    q_combined = Q()

    for step_idx, (step_model, _f, _name) in enumerate(chain):
        if step_idx == 0:
            continue
        target_predicates = get_predicates(step_model)
        target_tenant_field = get_tenant_field(step_model)

        if not target_predicates and target_tenant_field is None:
            continue

        prefix = "__".join(parts[:step_idx])
        scoped_qs = step_model.objects.all()

        # Tenant boundary on the target.
        if target_tenant_field is not None:
            if (
                request is None
                or user is None
                or not getattr(user, "is_authenticated", False)
            ):
                # Fail closed: no resolvable tenant means the JOIN target
                # is out of bounds.
                return q_combined & Q(**{f"{prefix}__in": step_model.objects.none()})
            tenant = get_user_tenant(user)
            if tenant is None:
                return q_combined & Q(**{f"{prefix}__in": step_model.objects.none()})
            scoped_qs = scoped_qs.filter(**{target_tenant_field: tenant})

        # Within-tenant predicates on the target.
        if target_predicates:
            if request is None:
                return q_combined & Q(**{f"{prefix}__in": step_model.objects.none()})
            target_q = Q()
            for pred in target_predicates:
                target_q &= pred.q(request, user_roles)
            # An empty Q() from a predicate chain means "fully bypassed"
            # — no extra scoping needed beyond tenant.
            scoped_qs = scoped_qs.filter(target_q)

        q_combined &= Q(**{f"{prefix}__in": scoped_qs.values("pk")})

    return q_combined


def scoped_target_queryset(target_model, request):
    """Queryset of ``target_model`` rows visible to ``request``'s user via the
    target's OWN tenant + predicates, or ``None`` when the target is unscoped
    (public — no predicates, no ``tenant_field``) and needs no scoping.

    Mirrors a single step of :func:`build_traversal_scope_q`; used to scope the
    compiled M2M merge's second query (finding F4), where the leak is a separate
    query rather than a JOIN on the parent queryset.
    """
    from django.db.models import Q

    from .backends import get_user_roles
    from .predicates import get_predicates, get_tenant_field, get_user_tenant

    predicates = get_predicates(target_model)
    tenant_field = get_tenant_field(target_model)
    if not predicates and tenant_field is None:
        return None

    user = getattr(request, "user", None) if request is not None else None
    qs = target_model.objects.all()

    if tenant_field is not None:
        if (
            request is None
            or user is None
            or not getattr(user, "is_authenticated", False)
        ):
            return target_model.objects.none()
        tenant = get_user_tenant(user)
        if tenant is None:
            return target_model.objects.none()
        qs = qs.filter(**{tenant_field: tenant})

    if predicates:
        if request is None:
            return target_model.objects.none()
        roles = set(get_user_roles(user)) if user is not None else set()
        target_q = Q()
        for pred in predicates:
            target_q &= pred.q(request, roles)
        qs = qs.filter(target_q)

    return qs


def check_nested_field_permissions(model, field_path, user, use_cache=True):
    """
    Check permissions for a nested field path using permission snapshots.

    This function traverses the relationship chain and checks read permissions
    at each level, building snapshots for related models as needed.

    Args:
        model: Starting Django model class
        field_path: Field path with __ notation
        user: Django user object for permission checking
        use_cache: Whether to use permission snapshot caching (default: True)

    Returns:
        bool: True if user has permission to read the entire path

    Example:
        For 'author__salary__amount':
        1. Check Book.author permission (build Book snapshot)
        2. Build Author model snapshot, check Author.salary permission
        3. Build Salary model snapshot, check Salary.amount permission
        Returns True only if ALL checks pass.
    """
    from .backends import build_permission_snapshot

    # Simple field (no nesting) - check on base model
    if "__" not in field_path:
        snapshot = build_permission_snapshot(user, model, use_cache=use_cache)
        base_field = field_path
        if snapshot.has_read_rule(base_field):
            return snapshot.can_read_field(base_field)
        else:
            return snapshot.can_perform_action("read")

    # Nested field - traverse and check permissions at each level
    parts = field_path.split("__")
    current_model = model

    for i, part in enumerate(parts):
        # Build snapshot for current model
        current_snapshot = build_permission_snapshot(
            user, current_model, use_cache=use_cache
        )

        # Check permission for this field
        if current_snapshot.has_read_rule(part):
            if not current_snapshot.can_read_field(part):
                logger.debug(
                    f"Permission denied: {current_model.__name__}.{part} "
                    f"(explicit read rule failed)"
                )
                return False
        else:
            if not current_snapshot.can_perform_action("read"):
                logger.debug(
                    f"Permission denied: {current_model.__name__}.{part} "
                    f"(model-level read permission failed)"
                )
                return False

        # If not the last part, traverse to the related model
        if i < len(parts) - 1:
            try:
                field = current_model._meta.get_field(part)
                if hasattr(field, "related_model") and field.related_model:
                    # Get the related model for the next iteration
                    current_model = field.related_model
                else:
                    # Not a relational field, can't traverse further
                    remaining_path = ".".join(parts[i + 1 :])
                    logger.warning(
                        f"Field '{part}' on {current_model.__name__} is not a "
                        f"relational field, cannot traverse to '{remaining_path}'"
                    )
                    return False
            except FieldDoesNotExist:
                logger.warning(
                    f"Field '{part}' does not exist on model {current_model.__name__}"
                )
                return False

    return True


def _get_sensitive_fields():
    from django.conf import settings

    from .settings import TURBODRF_SENSITIVE_FIELDS as default_sensitive

    return set(getattr(settings, "TURBODRF_SENSITIVE_FIELDS", default_sensitive))


def is_field_path_sensitive(field_path):
    """True if ANY segment of the `__`-path is in the sensitive deny-list.

    Every hop must be checked, not just the first — otherwise a path like
    `related__password` would pass through and expose a denied field via
    a relation traversal.
    """
    sensitive = _get_sensitive_fields()
    for segment in field_path.split("__"):
        if segment in sensitive:
            return True
    return False


def is_field_visible_to_user(model, field_path, user, use_cache=True):
    """Single canonical check: should this field path be visible to this user?

    This is the helper that all serialization paths (search, ordering,
    filter, OPTIONS, compiled, DRF serializer) should consult so they don't
    drift apart. Combines:

      1. Sensitive deny-list at every `__` segment.
      2. Nested-path permission walk via check_nested_field_permissions.

    Pass user=None for anonymous (the snapshot system handles 'guest' role
    if configured).
    """
    if is_field_path_sensitive(field_path):
        return False
    return check_nested_field_permissions(model, field_path, user, use_cache=use_cache)


def filter_readable_fields(model, fields, user, use_cache=True):
    """Return the subset of `fields` (list of `__`-paths) that user can read."""
    return [
        f
        for f in fields
        if is_field_visible_to_user(model, f, user, use_cache=use_cache)
    ]


def validate_filter_field(model, filter_param):
    """
    Validate a filter parameter including nesting depth and field existence.

    Args:
        model: Django model class
        filter_param: Filter parameter (e.g., 'author__name__icontains')

    Returns:
        tuple: (field_path, lookup) or raises ValidationError

    Example:
        >>> validate_filter_field(Book, 'author__name__icontains')
        ('author__name', 'icontains')
        >>> validate_filter_field(Book, 'price__gte')
        ('price', 'gte')
    """
    # Strip _or suffix if present
    if filter_param.endswith("_or"):
        filter_param = filter_param[:-3]

    # Split into field path and lookup
    parts = filter_param.split("__")

    # Common Django lookups
    lookups = {
        "exact",
        "iexact",
        "contains",
        "icontains",
        "in",
        "gt",
        "gte",
        "lt",
        "lte",
        "startswith",
        "istartswith",
        "endswith",
        "iendswith",
        "range",
        "date",
        "year",
        "month",
        "day",
        "week",
        "week_day",
        "quarter",
        "time",
        "hour",
        "minute",
        "second",
        "isnull",
        "regex",
        "iregex",
    }

    # Check if last part is a lookup
    if parts[-1] in lookups:
        field_path = "__".join(parts[:-1])
        lookup = parts[-1]
    else:
        field_path = filter_param
        lookup = "exact"

    # Validate nesting depth
    validate_nesting_depth(field_path)

    return field_path, lookup
