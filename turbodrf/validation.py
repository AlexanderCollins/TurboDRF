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

    from .mixins import get_searchable_fields
    from .predicates import get_predicates, get_tenant_field

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


def build_traversal_scope_q(parent_model, field_path, request):
    """Build a Q that scopes every JOIN target along ``field_path`` to
    the rows the request's user can see via that target's own endpoint.

    For each model in the JOIN chain that has registered predicates or
    a ``tenant_field``, this AND's
    ``<prefix>__in=<TargetModel._base_manager.filter(<target_q>).values("pk")>``
    onto the parent queryset, where ``target_q`` mirrors the target view's
    tenant_q + predicate_q construction (see
    :meth:`turbodrf.views.TurboDRFViewSet._get_tenant_q` and
    :meth:`._get_predicate_q`).

    The base manager is deliberate: a JOIN, like Django's FK descriptor,
    ignores the target's default manager, and the serializer path renders the
    same related row. A default-manager filter (soft delete, say) is not a
    scoping layer — tenant_field and predicates are. Scoping on the default
    manager dropped every row whose FK pointed at a soft-deleted target.

    Used at request time to wrap URL-driven JOINs (``?fk__field=...``,
    ``?ordering=fk__field``) so the JOIN can't render rows the caller
    cannot see via the target's own endpoint — same bug class as the
    compiled M2M / search-field bypasses, but URL-driven so it can't be
    gated at startup.

    A parent row whose relation is EMPTY at a scoped step (a NULL FK, or no
    rows across a reverse / M2M relation — at that hop or any earlier one) is
    kept: an empty relation joins nothing, so it cannot reveal a row the caller
    can't see. Without this, ``NULL IN (subquery)`` is never true and every
    row with a NULL FK silently disappears from lists, counts and filters.
    Rows whose relation points at an out-of-scope target are still excluded.

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
    # True once any hop up to the current step can be empty for a parent row.
    may_be_empty = False

    for step_idx, (step_model, _f, _name) in enumerate(chain):
        if step_idx == 0:
            continue
        may_be_empty = may_be_empty or _relation_may_be_empty(chain[step_idx - 1][1])
        target_predicates = get_predicates(step_model)
        target_tenant_field = get_tenant_field(step_model)

        if not target_predicates and target_tenant_field is None:
            continue

        prefix = "__".join(parts[:step_idx])
        scoped_qs = step_model._base_manager.all()
        fail_closed = False

        # Tenant boundary on the target.
        if target_tenant_field is not None:
            tenant = None
            if (
                request is not None
                and user is not None
                and getattr(user, "is_authenticated", False)
            ):
                tenant = get_user_tenant(user)
            if tenant is None:
                # Fail closed: no resolvable tenant means every JOIN target
                # is out of bounds.
                fail_closed = True
            else:
                scoped_qs = scoped_qs.filter(**{target_tenant_field: tenant})

        # Within-tenant predicates on the target.
        if target_predicates and not fail_closed:
            if request is None:
                fail_closed = True
            else:
                target_q = Q()
                for pred in target_predicates:
                    target_q &= pred.q(request, user_roles)
                # An empty Q() from a predicate chain means "fully bypassed"
                # — no extra scoping needed beyond tenant.
                scoped_qs = scoped_qs.filter(target_q)

        if fail_closed:
            scoped_qs = step_model._base_manager.none()

        step_q = Q(**{f"{prefix}__in": scoped_qs.values("pk")})
        if may_be_empty:
            step_q = Q(**{f"{prefix}__isnull": True}) | step_q
        q_combined &= step_q

        if fail_closed:
            return q_combined

    return q_combined


def _relation_may_be_empty(field):
    """True if a parent row can have NO target across relation ``field``: a
    nullable FK / one-to-one, or a reverse / M2M relation (zero rows)."""
    if getattr(field, "many_to_many", False) or getattr(field, "one_to_many", False):
        return True
    return bool(getattr(field, "null", False))


def scoped_target_queryset(target_model, request, base_manager=False):
    """Queryset of ``target_model`` rows visible to ``request``'s user via the
    target's OWN tenant + predicates, or ``None`` when the target is unscoped
    (public — no predicates, no ``tenant_field``) and needs no scoping.

    Mirrors a single step of :func:`build_traversal_scope_q`; used to scope the
    compiled M2M merge's second query (finding F4), where the leak is a separate
    query rather than a JOIN on the parent queryset.

    Built on the target's default manager (what an M2M manager and a write's
    related field read through), or on its base manager with
    ``base_manager=True`` — for a hop of a nested path, which Django follows
    without the default manager.

    Each visible row appears once: the predicates are applied as a ``pk__in``
    subquery, as ``views.Authorization.scope`` does for one that crosses an
    M2M or reverse relation (``Members``, or an ``Either`` ORing one in) — a
    JOIN would return a row once per matching related row.
    """
    from django.db.models import Q

    from .backends import get_user_roles
    from .predicates import get_predicates, get_tenant_field, get_user_tenant

    predicates = get_predicates(target_model)
    tenant_field = get_tenant_field(target_model)
    if not predicates and tenant_field is None:
        return None

    user = getattr(request, "user", None) if request is not None else None
    manager = (
        target_model._base_manager if base_manager else target_model._default_manager
    )
    qs = manager.all()

    if tenant_field is not None:
        if (
            request is None
            or user is None
            or not getattr(user, "is_authenticated", False)
        ):
            return manager.none()
        tenant = get_user_tenant(user)
        if tenant is None:
            return manager.none()
        qs = qs.filter(**{tenant_field: tenant})

    if predicates:
        if request is None:
            return manager.none()
        roles = set(get_user_roles(user)) if user is not None else set()
        target_q = Q()
        for pred in predicates:
            target_q &= pred.q(request, roles)
        if target_q:
            return manager.filter(pk__in=qs.filter(target_q).values("pk"))

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

    return _path_readable(
        model,
        field_path,
        lambda current: build_permission_snapshot(user, current, use_cache=use_cache),
    )


def _path_readable(model, field_path, snapshot_for):
    """``check_nested_field_permissions`` with the snapshot of each model on
    the path from ``snapshot_for(model)``, so a caller checking many paths can
    reuse them. Each segment is checked by its field name: Django also
    resolves an FK's attname (``author_id``), which no rule names."""
    field_path = _normalised_path(model, field_path)
    parts = field_path.split("__")
    current_model = model

    for i, part in enumerate(parts):
        current_snapshot = snapshot_for(current_model)

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

    # The setting ADDS to the defaults: replacing them let `['ssn']` silently
    # un-deny `password`, `token`, ...
    return set(default_sensitive) | set(
        getattr(settings, "TURBODRF_SENSITIVE_FIELDS", ())
    )


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
    if configured). An FK's attname (``token_id``) is checked as the field
    (``token``).
    """
    if is_field_path_sensitive(_normalised_path(model, field_path)):
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

    # Split into field path and lookup. The field path is resolved against
    # the model FIRST: a related field named like a lookup (`folder__date`,
    # where `date` is a field of the folder) is a field, not a lookup —
    # treating it as one let the JOIN run unscoped and unchecked.
    parts = filter_param.split("__")
    field_parts = _resolve_field_parts(model, parts)
    rest = parts[len(field_parts) :]

    if not field_parts:
        # Not a field of this model at all (the caller rejects it); split off
        # a trailing lookup for the error paths that report it.
        if len(parts) > 1 and parts[-1] in _FILTER_LOOKUPS:
            field_path, lookup = "__".join(parts[:-1]), parts[-1]
        else:
            field_path, lookup = filter_param, "exact"
    elif not rest:
        field_path, lookup = "__".join(field_parts), "exact"
    elif all(part in _FILTER_LOOKUPS for part in rest):
        field_path, lookup = "__".join(field_parts), "__".join(rest)
    elif _is_json_field(model, field_parts):
        # JSON key transforms: the keys are part of the path (and fail the
        # nested permission walk unless permissions are bypassed).
        if rest[-1] in _FILTER_LOOKUPS:
            field_path, lookup = "__".join(field_parts + rest[:-1]), rest[-1]
        else:
            field_path, lookup = "__".join(field_parts + rest), "exact"
    else:
        raise ValidationError(
            f"'{filter_param}' is not a field path with a supported lookup."
        )

    # Validate nesting depth
    validate_nesting_depth(field_path)

    return field_path, lookup


# Lookups (and date/time transforms) a client filter may use. Regex lookups
# are not offered: a client-supplied pattern can backtrack catastrophically.
_FILTER_LOOKUPS = {
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
}


def _resolve_field_parts(model, parts):
    """The field names of the longest prefix of ``parts`` that resolves as a
    field path on ``model`` (following relations). An FK's attname
    (``author_id``) resolves to the field's name (``author``): permission
    rules and the sensitive deny-list name fields, not columns."""
    resolved = []
    current = model
    for part in parts:
        if current is None:
            break
        try:
            field = current._meta.get_field(part)
        except FieldDoesNotExist:
            break
        resolved.append(field.name)
        current = field.related_model if field.is_relation else None
    return resolved


def _normalised_path(model, field_path):
    """``field_path`` with each segment that resolves on ``model`` named by
    its field name (``deal__assigned_broker_id`` -> ``deal__assigned_broker``)."""
    parts = field_path.split("__")
    resolved = _resolve_field_parts(model, parts)
    return "__".join(resolved + parts[len(resolved) :])


def _is_json_field(model, field_parts):
    from django.db.models import JSONField

    field = model
    for part in field_parts:
        field = field._meta.get_field(part)
        if field.is_relation:
            field = field.related_model
    return isinstance(field, JSONField)
