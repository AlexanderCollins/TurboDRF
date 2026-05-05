"""
Tenant FK auto-detection and field path validation.

Two responsibilities:
1. Walk a model's FK graph at startup to find the shortest path to the
   configured tenant model. Used to fill in `tenant_field` when not explicitly
   declared. Ambiguous (multiple shortest paths) → raise loudly.
2. Validate `__`-separated field paths against the actual model graph at
   startup. Bad segments raise ImproperlyConfigured with a "did you mean"
   suggestion so misconfiguration is loud, not silent.
"""

import difflib
from collections import deque

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured


class AmbiguousTenantPath(ImproperlyConfigured):
    """Raised when multiple FK paths from a model lead to the tenant model."""


def _resolve_tenant_model(tenant_model_setting):
    """Resolve a 'app.Model' string or model class to a model class."""
    if tenant_model_setting is None:
        return None
    if isinstance(tenant_model_setting, str):
        try:
            return apps.get_model(tenant_model_setting)
        except (LookupError, ValueError) as e:
            raise ImproperlyConfigured(
                f"TURBODRF_TENANT_MODEL={tenant_model_setting!r} cannot be "
                f"resolved: {e}"
            )
    return tenant_model_setting


def find_tenant_path(model, tenant_model, max_depth=4):
    """BFS the FK graph from `model` to `tenant_model`.

    Returns the shortest path as a `__`-joined string (e.g.
    'bank_account__deal__brokerage') or None if no path exists.

    Raises AmbiguousTenantPath if two or more paths of the same shortest length
    exist. We refuse to guess in that case.
    """
    tenant_model = _resolve_tenant_model(tenant_model)
    if tenant_model is None:
        return None
    if model is tenant_model:
        return None  # caller decides what to do (e.g. 'tenancy': 'self')

    # BFS over (model, path-so-far)
    queue = deque([(model, [])])
    visited = {model}
    found = []  # all shortest paths
    shortest_len = None

    while queue:
        current_model, path = queue.popleft()
        if shortest_len is not None and len(path) >= shortest_len:
            continue
        if len(path) >= max_depth:
            continue

        for field in current_model._meta.get_fields():
            # Forward FK / OneToOne, not reverse
            if not getattr(field, "is_relation", False):
                continue
            if not (field.many_to_one or field.one_to_one):
                continue
            related = field.related_model
            if related is None:
                continue

            new_path = path + [field.name]

            if related is tenant_model:
                if shortest_len is None or len(new_path) <= shortest_len:
                    shortest_len = len(new_path)
                    found.append(new_path)
            else:
                if related not in visited:
                    visited.add(related)
                    queue.append((related, new_path))

    if not found:
        return None

    # Filter to only paths of shortest length (BFS may have added longer ones)
    shortest = [p for p in found if len(p) == shortest_len]

    if len(shortest) > 1:
        # Multiple equally-short paths — refuse to guess
        formatted = ", ".join("'" + "__".join(p) + "'" for p in shortest)
        raise AmbiguousTenantPath(
            f"Auto-detection found multiple paths from {model.__name__} "
            f"to {tenant_model.__name__}: {formatted}. "
            f"Set 'tenant_field' explicitly in {model.__name__}.turbodrf() "
            f"to disambiguate."
        )

    return "__".join(shortest[0])


def validate_field_path(model, path):
    """Walk `path` segment by segment against the Django field graph.

    Raises ImproperlyConfigured with a helpful message + did-you-mean
    suggestions if any segment is invalid.

    Used for `tenant_field`, `owner_field`, members fields, and any other
    `__`-path declared in turbodrf() config.
    """
    if not path or not isinstance(path, str):
        raise ImproperlyConfigured(
            f"Field path must be a non-empty string; got {path!r}"
        )

    parts = path.split("__")
    current_model = model

    for i, part in enumerate(parts):
        try:
            field = current_model._meta.get_field(part)
        except FieldDoesNotExist:
            available = _available_field_names(current_model)
            suggestions = difflib.get_close_matches(part, available, n=3, cutoff=0.5)
            traversed = "__".join(parts[:i]) if i > 0 else "(start)"
            hint = (
                f". Did you mean: {', '.join(repr(s) for s in suggestions)}?"
                if suggestions
                else ""
            )
            raise ImproperlyConfigured(
                f"{model.__name__}.turbodrf() declares path "
                f"{path!r}, but field {part!r} does not exist on "
                f"{current_model.__name__} (after traversing {traversed}){hint}"
            )

        # If this isn't the last segment, the field must be a relation
        if i < len(parts) - 1:
            related = getattr(field, "related_model", None)
            if related is None:
                raise ImproperlyConfigured(
                    f"{model.__name__}.turbodrf() declares path {path!r}, "
                    f"but {current_model.__name__}.{part} is not a relation; "
                    f"cannot traverse into it."
                )
            current_model = related


def _available_field_names(model):
    """List of field names on a model (for did-you-mean suggestions)."""
    names = set()
    for f in model._meta.get_fields():
        if hasattr(f, "name"):
            names.add(f.name)
    return list(names)


def resolve_tenancy_for_model(model, config, tenant_model_setting, autodetect=True):
    """Resolve a model's tenancy at startup.

    Returns (tenant_field: str|None, predicates: list[Predicate], autodetected: bool).

    Tenant is a SETTING applied as a separate AND outside the predicate
    algebra. The visibility predicates (Owner / Members / Either / Custom)
    operate only within-tenant.
    """
    from .predicates import parse_config

    autodetected = False
    config = config or {}

    if config.get("tenancy") == "shared":
        return None, [], False

    # Power form (visibility=...): parse_config extracts any Tenant() into
    # tenant_field setting (with deprecation warning) and rejects Tenant
    # inside Either.
    if config.get("visibility") is not None:
        tenant_field, predicates = parse_config(config)
        if tenant_field is not None:
            validate_field_path(model, tenant_field)
        for p in predicates:
            _validate_predicate_paths(model, p)
        return tenant_field, predicates, False

    # Sugar form (tenant_field + owner_field + bypass_owner_roles)
    config = dict(config)
    has_explicit_tenant = config.get("tenant_field") is not None

    if (
        not has_explicit_tenant
        and tenant_model_setting is not None
        and autodetect
        and not _model_is_tenant(model, tenant_model_setting)
    ):
        path = find_tenant_path(model, tenant_model_setting)
        if path is not None:
            config["tenant_field"] = path
            autodetected = True

    tenant_field, predicates = parse_config(config)

    if tenant_field is not None:
        validate_field_path(model, tenant_field)
    for p in predicates:
        _validate_predicate_paths(model, p)

    return tenant_field, predicates, autodetected


def _model_is_tenant(model, tenant_model_setting):
    """True if `model` is the tenant model itself."""
    tenant_model = _resolve_tenant_model(tenant_model_setting)
    return model is tenant_model


def _validate_predicate_paths(model, predicate):
    """Best-effort path validation for known predicate types.

    For known Predicate subclasses (Owner, Members, Group, Either), validate
    each declared field path against the model graph. Custom / Conditional
    are caller's responsibility.

    Tenant is no longer accepted in the predicate algebra — it's a setting,
    validated separately by validate_field_path() in the caller.
    """
    from .predicates import Either, Group, Members, Owner

    if isinstance(predicate, Owner):
        for f in predicate.fields:
            validate_field_path(model, f)
    elif isinstance(predicate, Members):
        validate_field_path(model, predicate.m2m_field)
    elif isinstance(predicate, Group):
        validate_field_path(model, predicate.field)
    elif isinstance(predicate, Either):
        for child in predicate.predicates:
            _validate_predicate_paths(model, child)
    # Custom / Conditional / unknown subclasses skipped
