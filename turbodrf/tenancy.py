"""
Tenant FK auto-detection, field path validation and tenancy registration.

Three responsibilities:
1. Walk a model's FK graph at startup to find the shortest path to the
   configured tenant model. Used to fill in `tenant_field` when not explicitly
   declared. Ambiguous (multiple shortest paths) → raise loudly.
2. Validate `__`-separated field paths against the actual model graph at
   startup. Bad segments raise ImproperlyConfigured with a "did you mean"
   suggestion so misconfiguration is loud, not silent.
3. Register every TurboDRF model's tenant boundary and predicates
   (`_register_tenancy`), from `AppConfig.ready()` and every router build.
"""

import difflib

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


def _is_forward_relation(field):
    """True for a concrete forward ForeignKey / one-to-one (a reverse
    one-to-one is one_to_one too, but isn't concrete)."""
    return (
        getattr(field, "concrete", False)
        and (field.many_to_one or field.one_to_one)
        and field.related_model is not None
    )


def find_tenant_path(model, tenant_model, max_depth=4):
    """BFS the FK graph from `model` to `tenant_model`.

    Returns the shortest path as a `__`-joined string (e.g.
    'bank_account__deal__brokerage') or None if no path exists. Only concrete
    forward FK / one-to-one hops are followed.

    Raises AmbiguousTenantPath if two or more paths of the same shortest length
    exist. We refuse to guess in that case. Every path into a model at its
    shortest depth is kept, so two FKs to the same intermediate model (a
    transfer's ``source`` and ``dest`` accounts) are ambiguous rather than the
    first one found winning.
    """
    tenant_model = _resolve_tenant_model(tenant_model)
    if tenant_model is None:
        return None
    if model is tenant_model:
        return None  # the tenant model itself: see docs/tenancy.md

    # Level-by-level BFS over paths: a model is expanded only at the depth it
    # is first reached, but along every path that reaches it at that depth.
    frontier = [(model, [])]
    reached = {model}
    found = []
    for _depth in range(max_depth):
        next_frontier = []
        reached_now = set()
        for current_model, path in frontier:
            for field in current_model._meta.get_fields():
                if not _is_forward_relation(field):
                    continue
                related = field.related_model
                new_path = path + [field.name]
                if related is tenant_model:
                    found.append(new_path)
                elif related not in reached:
                    reached_now.add(related)
                    next_frontier.append((related, new_path))
        if found:
            break
        reached |= reached_now
        frontier = next_frontier

    if not found:
        return None

    if len(found) > 1:
        # Multiple equally-short paths — refuse to guess
        formatted = ", ".join("'" + "__".join(p) + "'" for p in found)
        raise AmbiguousTenantPath(
            f"Auto-detection found multiple paths from {model.__name__} "
            f"to {tenant_model.__name__}: {formatted}. "
            f"Set 'tenant_field' explicitly in {model.__name__}.turbodrf() "
            f"to disambiguate."
        )

    return "__".join(found[0])


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


def _validate_tenant_path(model, path, tenant_model_setting=None):
    """Validate a ``tenant_field`` path: every hop a forward ForeignKey /
    one-to-one, named by field name (not ``<fk>_id``), the last one
    referencing the tenant's primary key, ending at ``TURBODRF_TENANT_MODEL``
    when that is set.

    A hop through an M2M or reverse relation duplicates list rows and makes
    detail lookups raise ``MultipleObjectsReturned``. A path ending anywhere
    but the tenant model compares the caller's tenant pk with another table's
    keys, and matches unrelated rows. So does a last hop with ``to_field``: an
    integer tenant (TURBODRF_TENANT_USER_FIELD may return one) is compared
    with, and written into, that column.
    """
    validate_field_path(model, path)
    current = model
    for part in path.split("__"):
        field = current._meta.get_field(part)
        if not _is_forward_relation(field):
            raise ImproperlyConfigured(
                f"{model.__name__}.turbodrf() declares tenant_field {path!r}, "
                f"but {current.__name__}.{part} is not a forward ForeignKey or "
                f"OneToOneField. Every hop of a tenant_field must be one."
            )
        if field.name != part:
            raise ImproperlyConfigured(
                f"{model.__name__}.turbodrf() declares tenant_field {path!r}: "
                f"name the relation {field.name!r}, not its column {part!r}."
            )
        current = field.related_model
    if not field.target_field.primary_key:
        raise ImproperlyConfigured(
            f"{model.__name__}.turbodrf() declares tenant_field {path!r}, but "
            f"{field.model.__name__}.{field.name} references "
            f"{current.__name__}.{field.target_field.name}. The last hop of a "
            f"tenant_field must reference the tenant's primary key (drop "
            f"to_field)."
        )
    tenant_model = _resolve_tenant_model(tenant_model_setting)
    if (
        tenant_model is not None
        and current._meta.concrete_model is not tenant_model._meta.concrete_model
    ):
        raise ImproperlyConfigured(
            f"{model.__name__}.turbodrf() declares tenant_field {path!r}, which "
            f"ends at {current._meta.label}, not at TURBODRF_TENANT_MODEL "
            f"({tenant_model._meta.label})."
        )


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

    # Resolve the setting even when this model doesn't use it: a typo in
    # TURBODRF_TENANT_MODEL fails at startup, not never.
    _resolve_tenant_model(tenant_model_setting)

    if config.get("tenancy") is not None:
        # parse_config rejects any value but 'shared', and 'shared' combined
        # with scoping keys.
        parse_config(config)
        return None, [], False

    # Power form (visibility=...): parse_config extracts any Tenant() into
    # tenant_field setting (with deprecation warning) and rejects Tenant
    # inside Either.
    if config.get("visibility") is not None:
        tenant_field, predicates = parse_config(config)
        if tenant_field is not None:
            _validate_tenant_path(model, tenant_field, tenant_model_setting)
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
        _validate_tenant_path(model, tenant_field, tenant_model_setting)
    for p in predicates:
        _validate_predicate_paths(model, p)

    return tenant_field, predicates, autodetected


def _register_tenancy(models=None, configs=None):
    """Resolve and register the tenant boundary and predicates of every
    TurboDRF model in ``models`` (default: all installed), enabled or not.

    FK-injection checks, nested-value scoping, filter / search traversal and
    the startup gates read a *target* model's scoping from this registry, so
    a disabled model, or a process that hasn't built a router (a Celery
    worker, a WSGI worker before its first request), must still have it
    registered or the target reads as unscoped. Called from
    ``AppConfig.ready()`` and again by every router build. ``configs`` maps a
    model to its already-read ``turbodrf()`` dict.

    Every model is resolved before any is registered, so a configuration
    error leaves the registry as it was. Returns
    ``{model: (tenant_field, predicates, autodetected)}``.
    """
    from django.conf import settings

    from .mixins import TurboDRFMixin
    from .predicates import register_predicates, register_tenant_field

    if models is None:
        models = [m for m in apps.get_models() if issubclass(m, TurboDRFMixin)]
    configs = configs or {}
    tenant_model_setting = getattr(settings, "TURBODRF_TENANT_MODEL", None)
    autodetect = getattr(settings, "TURBODRF_AUTODETECT_TENANT", False)

    resolved = {}
    for model in models:
        config = configs[model] if model in configs else model.turbodrf()
        resolved[model] = resolve_tenancy_for_model(
            model, config, tenant_model_setting, autodetect=autodetect
        )

    scoped = sorted(m.__name__ for m, (tf, _p, _a) in resolved.items() if tf)
    if scoped and not getattr(settings, "TURBODRF_TENANT_USER_FIELD", None):
        raise ImproperlyConfigured(
            f"{', '.join(scoped)} declare a tenant_field, but "
            f"TURBODRF_TENANT_USER_FIELD is not set: no caller would have a "
            f"tenant, so every request would match nothing. Set it to the user "
            f"attribute that returns the tenant (e.g. 'workspace')."
        )

    for model, (tenant_field, predicates, _autodetected) in resolved.items():
        register_tenant_field(model, tenant_field)
        register_predicates(model, predicates)
    return resolved


def _model_is_tenant(model, tenant_model_setting):
    """True if `model` is the tenant model itself."""
    tenant_model = _resolve_tenant_model(tenant_model_setting)
    return model is tenant_model


def _validate_user_relation(model, path, what):
    """``path`` must resolve and end on a relation to the user model.
    Returns the last field."""
    from django.contrib.auth import get_user_model

    validate_field_path(model, path)
    current = model
    field = None
    for part in path.split("__"):
        field = current._meta.get_field(part)
        current = field.related_model
    user_model = get_user_model()
    if current is None or (
        current._meta.concrete_model is not user_model._meta.concrete_model
    ):
        target = current._meta.label if current is not None else "a plain column"
        raise ImproperlyConfigured(
            f"{model.__name__}.turbodrf() declares {what}, but {path!r} leads "
            f"to {target}, not to the user model ({user_model._meta.label})."
        )
    return field


def _validate_predicate_paths(model, predicate):
    """Best-effort path validation for known predicate types.

    For known Predicate subclasses (Owner, Members, Group, Either), validate
    each declared field path against the model graph. An ``Owner`` column
    must be a ForeignKey / one-to-one to the user model, and a ``Group``'s
    ``<field>__<user_via>`` lookup must reach the user model (a typo there
    raised FieldError on every request). Custom / Conditional are the
    caller's responsibility.

    The tenant boundary is a setting (``tenant_field``), validated
    separately by _validate_tenant_path() in the caller.
    """
    from .predicates import Either, Group, Members, Owner

    if isinstance(predicate, Owner):
        for f in predicate.fields:
            field = _validate_user_relation(model, f, f"Owner({f!r})")
            if not _is_forward_relation(field):
                raise ImproperlyConfigured(
                    f"{model.__name__}.turbodrf() declares Owner({f!r}), but "
                    f"{f!r} is not a ForeignKey or OneToOneField to the user "
                    f"model. Use Members(...) for a many-to-many."
                )
    elif isinstance(predicate, Members):
        validate_field_path(model, predicate.m2m_field)
    elif isinstance(predicate, Group):
        validate_field_path(model, predicate.field)
        _validate_user_relation(
            model,
            f"{predicate.field}__{predicate.user_via}",
            f"Group({predicate.field!r}, user_via={predicate.user_via!r})",
        )
    elif isinstance(predicate, Either):
        for child in predicate.predicates:
            _validate_predicate_paths(model, child)
    # Custom / Conditional / unknown subclasses skipped
