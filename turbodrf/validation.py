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


def build_traversal_scope_q(parent_model, field_path, request):
    """Build a Q that scopes every JOIN target along ``field_path`` to
    the rows the request's user can see via that target's own endpoint.

    For each model in the JOIN chain that has registered predicates or
    a ``tenant_field``, this AND's ``<prefix>__in=<visible pks>`` onto the
    parent queryset, the visible rows being ``scoped_target_queryset`` on the
    target's base manager.

    The base manager is deliberate: a JOIN, like Django's FK descriptor,
    ignores the target's default manager, and the serializer path renders the
    same related row. A default-manager filter (soft delete, say) is not a
    scoping layer — tenant_field and predicates are. Scoping on the default
    manager dropped every row whose FK pointed at a soft-deleted target.

    Used at request time to wrap the JOINs a request makes (``?search=``,
    ``?fk__field=...``, ``?ordering=fk__field``, nested fields) so the JOIN
    can't match or render rows the caller cannot see via the target's own
    endpoint.

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

    if not isinstance(field_path, str) or "__" not in field_path:
        return Q()

    try:
        _final, chain = get_nested_field_model(parent_model, field_path)
    except FieldDoesNotExist:
        return Q()

    parts = field_path.split("__")
    q_combined = Q()
    # True once any hop up to the current step can be empty for a parent row.
    may_be_empty = False

    for step_idx, (step_model, _f, _name) in enumerate(chain):
        if step_idx == 0:
            continue
        may_be_empty = may_be_empty or _relation_may_be_empty(chain[step_idx - 1][1])
        scoped = scoped_target_queryset(step_model, request, base_manager=True)
        if scoped is None:
            continue
        prefix = "__".join(parts[:step_idx])
        step_q = Q(**{f"{prefix}__in": scoped.values("pk")})
        if may_be_empty:
            step_q = Q(**{f"{prefix}__isnull": True}) | step_q
        q_combined &= step_q

    return q_combined


def _relation_may_be_empty(field):
    """True if a parent row can have NO target across relation ``field``: a
    nullable FK / one-to-one, or a reverse / M2M relation (zero rows)."""
    if getattr(field, "many_to_many", False) or getattr(field, "one_to_many", False):
        return True
    return bool(getattr(field, "null", False))


def scoped_target_queryset(target_model, request, base_manager=False):
    """Queryset of ``target_model`` rows visible to ``request``'s user via the
    target's OWN tenant + predicates (as its own endpoint scopes them), or
    ``None`` when the target is unscoped (no predicates, no ``tenant_field``)
    and needs no scoping.

    Built on the target's default manager (what an M2M manager and a write's
    related field read through), or on its base manager with
    ``base_manager=True`` — for a hop of a nested path, which Django follows
    without the default manager. Each visible row appears once (see
    ``views.Authorization.scope``).
    """
    from .predicates import get_predicates, get_tenant_field, predicates_q, tenant_q
    from .views import Authorization

    predicates = get_predicates(target_model)
    tenant_field = get_tenant_field(target_model)
    if not predicates and tenant_field is None:
        return None
    manager = (
        target_model._base_manager if base_manager else target_model._default_manager
    )
    scope = Authorization(
        tenant_q(tenant_field, request), predicates_q(predicates, request)
    )
    return scope.scope(manager.all())


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


def is_reverse_one_to_one(model, name):
    """True if ``name`` on ``model`` is a reverse one-to-one. Rendered bare it
    shows the related row's pk without that row's own scope, so it's neither
    listed (a startup error) nor added as the base of a nested path like
    ``twin__name``."""
    try:
        field = model._meta.get_field(name)
    except FieldDoesNotExist:
        return False
    return bool(field.one_to_one and field.auto_created and not field.concrete)


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
