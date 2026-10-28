"""
Automatic router for TurboDRF.

This module provides the TurboDRFRouter that automatically discovers
and registers all models with TurboDRFMixin: on the tenant scope (the
default) every enabled model, on the platform scope
(``TurboDRFRouter(scope="platform", gate=[...])``) every model with a
``"platform"`` block (see turbodrf.scope and docs/platform.md).
"""

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured
from django.urls import re_path
from rest_framework.permissions import BasePermission, OperationHolderMixin
from rest_framework.routers import DefaultRouter

from .mixins import TurboDRFMixin
from .scope import (
    PLATFORM,
    SCOPES,
    TENANCY_KEYS,
    TENANT,
    platform_reachable,
    use_scope,
)
from .views import TurboDRFViewSet


def _walk_predicates(predicates):
    """Yield every predicate (recursing into Either's children)."""
    from .predicates import Either

    for p in predicates:
        yield p
        if isinstance(p, Either):
            yield from _walk_predicates(p.predicates)


# CRUD actions that write. A `read_only: True` resource leaves them unrouted.
_CRUD_WRITE_ACTIONS = frozenset({"create", "update", "partial_update", "destroy"})


# Process-level flag: once the bypass-role validation passes for the real
# settings on first init, subsequent router inits (typically under
# override_settings during tests) skip the re-check. Production startup
# hits this once with the real TURBODRF_ROLES; tests with partial role
# overrides won't trip the guard against the model's static bypass list.
_bypass_roles_validated = False


def _field_path_resolves(model, path):
    """True if ``path`` names something the serializer can render: a model
    field or attribute (``@property``), or a ``__`` path whose hops before the
    last are relations."""
    parts = path.split("__")
    current = model
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        try:
            field = current._meta.get_field(part)
        except FieldDoesNotExist:
            return last and bool(part) and hasattr(current, part)
        if not last:
            current = getattr(field, "related_model", None)
            if current is None:
                return False
    return True


def _where(model, where=None):
    return where or f"{model.__name__}.turbodrf()"


def _configured_field_names(model, config, where=None):
    """Every name in the model's ``fields`` config ('__all__' adds none)."""
    fields = config.get("fields", "__all__")
    if fields == "__all__":
        return []
    if isinstance(fields, dict):
        names = []
        for key in ("list", "detail"):
            value = fields.get(key, [])
            if value != "__all__":
                names.extend(value)
        return names
    if isinstance(fields, str):
        raise ImproperlyConfigured(
            f"{_where(model, where)} 'fields' must be a list, a "
            f"{{'list': [...], 'detail': [...]}} dict or '__all__'; got "
            f"{fields!r}."
        )
    return list(fields)


def _is_unique_alone(model, field):
    """True if ``field`` alone identifies a row."""
    if field.unique:
        return True
    name = (field.name,)
    return any(
        tuple(c.fields) == name for c in model._meta.total_unique_constraints
    ) or any(tuple(t) == name for t in model._meta.unique_together)


# Every key turbodrf() may return. Anything else is a typo that would silently
# drop what it meant to configure ('owner_feild' would leave rows unscoped).
_CONFIG_KEYS = frozenset(
    {
        "actions",
        "bypass_owner_roles",
        "compiled",
        "enabled",
        "endpoint",
        "fields",
        "full_clean",
        "http_methods",
        "lookup_field",
        "owner_field",
        "platform",
        "property_relations",
        "public_access",
        "read_only",
        "searchable_fields",
        "tenancy",
        "tenant_field",
        "unscoped_properties",
        "visibility",
    }
)


# Keys a "platform" block may hold: those that shape the API, none that scope
# rows.
_PLATFORM_KEYS = _CONFIG_KEYS - TENANCY_KEYS - {"platform"}


def _validate_config_keys(model, config):
    unknown = sorted(set(config) - _CONFIG_KEYS)
    if not unknown:
        return
    hint = ""
    if "sensitive_fields" in unknown:
        hint = " 'sensitive_fields' is a class attribute on the model, not a key."
    raise ImproperlyConfigured(
        f"{model.__name__}.turbodrf() has unknown keys {unknown}.{hint} "
        f"Valid keys: {', '.join(sorted(_CONFIG_KEYS))}."
    )


def _ends_on_reverse_one_to_one(model, path):
    """True if ``path``'s last hop is a reverse one-to-one: rendered bare, it
    would show the related row's pk without that row's own scope."""
    current = model
    field = None
    for part in path.split("__"):
        try:
            field = current._meta.get_field(part)
        except FieldDoesNotExist:
            return False
        current = getattr(field, "related_model", None) or current
    return bool(
        field and field.one_to_one and field.auto_created and not field.concrete
    )


def _validate_fields_and_lookup(model, config, where=None):
    """Refuse to boot on a ``fields`` entry or ``lookup_field`` the model
    doesn't have. Either one failed every request with a 500 instead: a
    ``fields`` typo only logged "Could not compile …" (and nothing at all with
    ``compiled: False``). ``where`` names the config in errors (a platform
    block)."""
    where = _where(model, where)
    names = _configured_field_names(model, config, where)
    bad = [
        name
        for name in names
        if not isinstance(name, str) or not _field_path_resolves(model, name)
    ]
    if bad:
        raise ImproperlyConfigured(
            f"{where} 'fields' lists {bad}, which "
            f"{model.__name__} doesn't have. Each entry must be a model field, "
            f"a @property, or a '__' path through relations."
        )
    reverse = [name for name in names if _ends_on_reverse_one_to_one(model, name)]
    if reverse:
        raise ImproperlyConfigured(
            f"{where} 'fields' lists {reverse}, which end on "
            f"a reverse one-to-one relation. List a field on the related model "
            f"instead (e.g. '{reverse[0]}__name')."
        )

    lookup = config.get("lookup_field")
    if lookup is None or lookup == "pk":
        return
    try:
        field = model._meta.get_field(lookup) if isinstance(lookup, str) else None
    except FieldDoesNotExist:
        field = None
    if field is None or not getattr(field, "concrete", False) or field.many_to_many:
        raise ImproperlyConfigured(
            f"{where} 'lookup_field' {lookup!r} is not a "
            f"column of {model.__name__}."
        )
    if not _is_unique_alone(model, field):
        raise ImproperlyConfigured(
            f"{where} 'lookup_field' {lookup!r} isn't unique, "
            f"so a detail URL could match several rows. Use a field with "
            f"unique=True (or a single-field UniqueConstraint)."
        )


def _resolved_field_groups(model, config):
    """``(list fields, detail fields)`` of a config, ``'__all__'`` expanded to
    every concrete and many-to-many field (as mixins._resolve_configured_fields
    resolves it)."""
    fields = config.get("fields", "__all__")
    if isinstance(fields, dict):
        list_fields = fields.get("list", fields.get("detail", []))
        detail_fields = fields.get("detail", list_fields)
    else:
        list_fields = detail_fields = fields
    every = [f.name for f in model._meta.fields] + [
        f.name for f in model._meta.many_to_many
    ]
    return tuple(
        every if group == "__all__" else list(group or [])
        for group in (list_fields, detail_fields)
    )


def _unreachable_hop(model, path, bare_relation_ok=True):
    """``(hop, target model)`` of the first relation along ``path`` into a
    model the platform scope can't reach (scope.platform_reachable), else
    None. A path ending on a FK / one-to-one renders the stored id without a
    JOIN, so with ``bare_relation_ok`` its target isn't checked; a to-many
    relation always is."""
    current = model
    parts = path.split("__")
    for i, part in enumerate(parts):
        try:
            field = current._meta.get_field(part)
        except FieldDoesNotExist:
            return None  # a @property (fields are validated separately)
        if not field.is_relation or field.related_model is None:
            return None
        last = i == len(parts) - 1
        to_many = field.many_to_many or field.one_to_many
        if last and bare_relation_ok and not to_many:
            return None
        if not platform_reachable(field.related_model):
            return part, field.related_model
        current = field.related_model
    return None


def _unreachable_error(where, key, path, hop, target):
    return ImproperlyConfigured(
        f"{where} '{key}' names {path!r}, whose relation {hop!r} leads to "
        f"{target._meta.label}, which the platform scope can't reach: a "
        f"TurboDRF model is reachable there only with a 'platform' block of its "
        f"own ('platform': {{'enabled': False}} reaches it without a route), "
        f"or as TURBODRF_TENANT_MODEL."
    )


def _platform_where(model):
    return f"{model.__name__}.turbodrf()['platform']"


def _platform_block_of(model, config):
    """The model's platform block (a dict), None without one."""
    if "platform" not in config:
        return None
    block = config["platform"]
    if not isinstance(block, dict):
        raise ImproperlyConfigured(
            f"{model.__name__}.turbodrf() 'platform' must be a dict of the "
            f"platform API's config (fields, endpoint, actions, ...); got "
            f"{type(block).__name__}."
        )
    return block


def _validate_platform_block(model, config, tenant_field):
    """Refuse to boot on a ``"platform"`` block with the same strictness as
    the top-level config: unknown keys and tenancy keys, fields, the lookup
    field, the property config, and the platform scope's own rules (every
    relation it follows reaches a platform model, search only over configured
    fields, a create can name the row's tenant). Returns the block, or None
    for a model without one. ``tenant_field`` is the model's resolved
    tenant_field."""
    block = _platform_block_of(model, config)
    if block is None:
        return None
    where = _platform_where(model)

    tenancy = sorted(set(block) & TENANCY_KEYS)
    if tenancy:
        raise ImproperlyConfigured(
            f"{where} declares {tenancy}. The platform scope applies no tenant "
            f"boundary and no predicates, so tenancy keys belong at the top "
            f"level of turbodrf() only."
        )
    unknown = sorted(set(block) - _PLATFORM_KEYS)
    if unknown:
        hint = ""
        if "sensitive_fields" in unknown:
            hint = " 'sensitive_fields' is a class attribute on the model, not a key."
        raise ImproperlyConfigured(
            f"{where} has unknown keys {unknown}.{hint} Valid keys: "
            f"{', '.join(sorted(_PLATFORM_KEYS))}."
        )
    _validate_fields_and_lookup(model, block, where)

    list_fields, detail_fields = _resolved_field_groups(model, block)
    configured = set(list_fields) | set(detail_fields)
    for path in sorted(p for p in configured if isinstance(p, str)):
        found = _unreachable_hop(model, path)
        if found:
            raise _unreachable_error(where, "fields", path, *found)

    searchable = block.get("searchable_fields")
    if searchable is not None:
        if isinstance(searchable, str) or not isinstance(searchable, (list, tuple)):
            raise ImproperlyConfigured(
                f"{where} 'searchable_fields' must be a list of field paths."
            )
        for entry in searchable:
            path = entry.lstrip("^=@") if isinstance(entry, str) else entry
            if path not in configured:
                raise ImproperlyConfigured(
                    f"{where} 'searchable_fields' lists {entry!r}, which isn't in "
                    f"its 'fields'. On the platform scope only configured fields "
                    f"are searchable."
                )
            found = _unreachable_hop(model, path, bare_relation_ok=False)
            if found:
                raise _unreachable_error(where, "searchable_fields", path, *found)

    relations = block.get("property_relations")
    if isinstance(relations, dict):
        from .properties import relation_hops

        for paths in relations.values():
            for path in paths if isinstance(paths, (list, tuple)) else ():
                for _source, field, accessor, target in relation_hops(model, path):
                    if not platform_reachable(target):
                        raise _unreachable_error(
                            where, "property_relations", path, accessor, target
                        )

    if tenant_field and not block.get("read_only"):
        methods = block.get("http_methods")
        posts = methods is None or "post" in {str(m).lower() for m in methods}
        hop = tenant_field.split("__", 1)[0]
        if posts and hop not in {str(f).split("__", 1)[0] for f in detail_fields}:
            raise ImproperlyConfigured(
                f"{where} allows creates (POST) but its 'fields' leave out "
                f"{hop!r}, the first hop of tenant_field {tenant_field!r}. A "
                f"create on the platform API must name the row's tenant: add "
                f"{hop!r} to 'fields', or set 'read_only': True / leave 'post' "
                f"out of 'http_methods'."
            )
    return block


def _validate_platform_blocks(models, configs, resolved):
    """``{model: platform block}`` for every model with one, each validated
    (``resolved``: the tenancy registration, ``{model: (tenant_field, ...)}``).
    Both routers run this: a broken block fails at startup whether or not a
    platform router is mounted."""
    blocks = {}
    for model in models:
        block = _validate_platform_block(model, configs[model], resolved[model][0])
        if block is not None:
            blocks[model] = block
    return blocks


def _validate_gate(scope, gate):
    """The router's gate as a tuple of permission classes. The platform scope
    requires one."""
    if scope not in SCOPES:
        raise ImproperlyConfigured(
            f"TurboDRFRouter scope must be one of {SCOPES}; got {scope!r}."
        )
    if gate is None:
        gate = ()
    if isinstance(gate, (str, bytes)) or not isinstance(gate, (list, tuple)):
        gate = (gate,)
    gate = tuple(gate)
    for item in gate:
        if isinstance(item, BasePermission):
            raise ImproperlyConfigured(
                f"TurboDRFRouter gate takes permission classes, not instances: "
                f"pass {type(item).__name__}, not {type(item).__name__}()."
            )
        if not (
            isinstance(item, OperationHolderMixin)
            or (isinstance(item, type) and hasattr(item, "has_permission"))
        ):
            raise ImproperlyConfigured(
                f"TurboDRFRouter gate entries must be DRF permission classes; "
                f"got {item!r}."
            )
    if scope == PLATFORM and not gate:
        raise ImproperlyConfigured(
            "TurboDRFRouter(scope='platform') requires a gate: a non-empty list "
            "of permission classes (e.g. gate=[IsPlatformStaff]) checked before "
            "anything else on every route. The platform scope serves every "
            "tenant's rows."
        )
    return gate


def _platform_roles_setting():
    """TURBODRF_PLATFORM_ROLES, which a platform router requires (static; the
    platform scope never reads TurboDRFRole / UserRole rows)."""
    from django.conf import settings as django_settings

    roles = getattr(django_settings, "TURBODRF_PLATFORM_ROLES", None)
    if not isinstance(roles, dict) or not roles:
        mode = getattr(django_settings, "TURBODRF_PERMISSION_MODE", "static")
        note = (
            " Database permission mode applies to the tenant scope only: platform "
            "roles are always static."
            if mode == "database"
            else ""
        )
        raise ImproperlyConfigured(
            f"TurboDRFRouter(scope='platform') requires TURBODRF_PLATFORM_ROLES: "
            f"a non-empty dict of platform role -> permission strings, the same "
            f"shape as TURBODRF_ROLES.{note}"
        )
    return roles


class TurboDRFRouter(DefaultRouter):
    """
    Router that auto-discovers and registers TurboDRF models.

    This router extends DRF's DefaultRouter to automatically discover all
    Django models that inherit from TurboDRFMixin and register them as
    API endpoints. No manual registration is required.

    Features:
        - Automatic model discovery on initialization
        - Dynamic ViewSet generation for each model
        - Respects model configuration (enabled/disabled, custom endpoints)
        - Inherits all DefaultRouter functionality (browsable API, format suffixes)
        - Handles both trailing and non-trailing slash URLs

    Example:
        >>> # In your urls.py
        >>> from django.urls import path, include
        >>> from turbodrf.router import TurboDRFRouter
        >>>
        >>> router = TurboDRFRouter()
        >>>
        >>> urlpatterns = [
        ...     path('api/', include(router.urls)),
        ... ]

    This will automatically create endpoints for all TurboDRF-enabled models:
        - /api/books/ and /api/books
        - /api/authors/ and /api/authors
        etc. (the lowercase model name plus "s"; set 'endpoint' to change it).

    Platform scope (an operator's own staff, across tenants)::

        platform = TurboDRFRouter(scope="platform", gate=[IsPlatformStaff])
        urlpatterns += [path("console-api/", include(platform.urls))]

    registers only the models whose ``turbodrf()`` config has a ``"platform"``
    block, with no tenant boundary or predicates, roles from
    ``TURBODRF_PLATFORM_ROLE_GETTER`` and permissions from
    ``TURBODRF_PLATFORM_ROLES``. ``gate`` is required there: permission
    classes checked before anything else on every route. ``scope`` and
    ``gate`` may also be class attributes of a subclass. See
    docs/platform.md.
    """

    # "tenant" (the default) or "platform" (see turbodrf.scope).
    scope = TENANT
    # Permission classes checked before every other permission on every
    # route and action, OPTIONS and the schema view included. Required on
    # the platform scope, optional on the tenant scope.
    gate = ()
    # None: on for the tenant scope (DRF's default), off for the platform
    # scope. True / False set it either way (a gated router gates the index
    # too).
    include_root_view = None

    def __init__(self, *args, scope=None, gate=None, include_root_view=None, **kwargs):
        """
        Initialize the router and discover models.

        Args:
            *args: Positional arguments passed to DefaultRouter.
            scope: ``"tenant"`` (default) or ``"platform"``; overrides the
                class attribute.
            gate: Permission classes checked first on every route; overrides
                the class attribute. Required when the scope is platform.
            include_root_view: Serve DRF's API index at the router's root.
                Defaults to True on the tenant scope, False on the platform
                scope.
            **kwargs: Keyword arguments passed to DefaultRouter.
        """
        if scope is not None:
            self.scope = scope
        self.gate = _validate_gate(self.scope, self.gate if gate is None else gate)
        if include_root_view is not None:
            self.include_root_view = include_root_view
        elif self.include_root_view is None:
            self.include_root_view = self.scope == TENANT
        if self.gate:
            # The index lists the routes: gate it like them.
            self.APIRootView = type(
                "GatedAPIRootView",
                (self.APIRootView,),
                {
                    "permission_classes": list(self.gate),
                    "turbodrf_scope": self.scope,
                },
            )
        if self.scope == PLATFORM:
            # Its route names never shadow the tenant routes' in reverse().
            self.root_view_name = f"{PLATFORM}-{self.root_view_name}"
        super().__init__(*args, **kwargs)
        with use_scope(self.scope):
            self.discover_models()

    def get_schema_view(self, **kwargs):
        """A drf-yasg schema view of this router's routes only (see
        documentation.get_turbodrf_schema_view), generated in its scope and
        guarded by its gate. ``kwargs`` are get_turbodrf_schema_view's
        (``title``, ``version``, ...). None with TURBODRF_ENABLE_DOCS = False."""
        from .documentation import get_turbodrf_schema_view

        def views():
            return {viewset for _prefix, viewset, _basename in self.registry}

        return get_turbodrf_schema_view(
            scope=self.scope, gate=self.gate, views=views, **kwargs
        )

    def discover_models(self):
        """
        Discover all models with TurboDRFMixin and register them.

        This method iterates through all registered Django models and
        automatically registers those that inherit from TurboDRFMixin
        and are enabled in their configuration.

        The method:
        1. Finds all models inheriting from TurboDRFMixin
        2. Resolves and registers every one's tenancy (sugar → predicates,
           auto-detection), enabled or not
        3. Checks if the model is enabled (via turbodrf() config)
        4. Validates fields, lookup_field and the tenancy hard-fail
        5. Creates a dynamic ViewSet for the model
        6. Registers the ViewSet with the appropriate endpoint

        Models can customize their endpoint name via the 'endpoint' key
        in their turbodrf() configuration. If not specified, the endpoint
        defaults to the pluralized model name. URL names are prefixed with the
        model name (with the endpoint when another app's model of the same
        name took it first).

        A platform router registers the models with a ``"platform"`` block
        instead (``_discover_platform_models``).
        """
        if self.scope == PLATFORM:
            return self._discover_platform_models()

        import logging

        from django.conf import settings as django_settings

        from .predicates import Owner, has_tenancy_declaration
        from .properties import validate_property_config
        from .tenancy import _register_tenancy
        from .validation import validate_nesting_depth

        global _bypass_roles_validated

        logger = logging.getLogger(__name__)

        tenant_model_setting = getattr(django_settings, "TURBODRF_TENANT_MODEL", None)
        require_tenancy = getattr(django_settings, "TURBODRF_REQUIRE_TENANCY", True)
        autodetect = getattr(django_settings, "TURBODRF_AUTODETECT_TENANT", False)
        known_roles = set(getattr(django_settings, "TURBODRF_ROLES", {}).keys())

        # Read each model's config once for every pass below.
        models = [m for m in apps.get_models() if issubclass(m, TurboDRFMixin)]
        configs = {model: model.turbodrf() for model in models}
        enabled = [m for m in models if configs[m].get("enabled", True)]
        # A misspelt key drops what it meant to configure, on a disabled model
        # (still a scoped FK / filter target) as much as an enabled one.
        for model in models:
            _validate_config_keys(model, configs[model])

        # ---------------------------------------------------------------
        # Resolve and register tenancy for EVERY model, enabled or not: a
        # disabled model is still a FK / nested-field / filter target, and
        # those checks read its scoping from the registry. Returns
        # (tenant_field, predicates, autodetected) per model. Tenant is a
        # SETTING separate from the predicate algebra so OR-composition
        # cannot escape it.
        # ---------------------------------------------------------------
        resolved = _register_tenancy(models, configs)

        # Every platform block is checked here too, so a broken one fails
        # whether or not a platform router is mounted.
        _validate_platform_blocks(models, configs, resolved)

        for model in enabled:
            config = configs[model]

            # Validate nesting depth for configured fields
            for field in _configured_field_names(model, config):
                try:
                    validate_nesting_depth(field)
                except Exception as e:
                    logger.warning(
                        f"Model {model.__name__} field '{field}' "
                        f"validation failed: {str(e)}"
                    )

            # Every configured field and the lookup_field must exist.
            _validate_fields_and_lookup(model, config)

            # property_relations / unscoped_properties must name
            # real properties and loadable relation paths.
            validate_property_config(model)

            tenant_field, predicates, autodetected = resolved[model]

            # Within-tenant predicates don't count: owner_field, or a
            # visibility list whose bypass roles / Conditional can lift the
            # restriction, would let some callers see every tenant's rows.
            if (
                require_tenancy
                and tenant_model_setting is not None
                and tenant_field is None
                and not has_tenancy_declaration(config)
            ):
                # Auto-detection runs for the sugar form only, when enabled.
                detected = (
                    " and no tenant FK could be auto-detected"
                    if autodetect and config.get("visibility") is None
                    else ""
                )
                raise ImproperlyConfigured(
                    f"{model.__name__}.turbodrf() declares no tenant "
                    f"boundary{detected}. Add 'tenant_field': '<path to "
                    f"{tenant_model_setting}>', or 'tenancy': 'shared' for "
                    f"reference data. An 'owner_field' alone scopes rows within "
                    f"a tenant, not across tenants; so does a 'visibility' list "
                    f"without a 'tenant_field' unless one of its predicates "
                    f"restricts every caller (an Owner without bypass roles, "
                    f"Members, Group or Custom). "
                    f"Set TURBODRF_REQUIRE_TENANCY=False to disable "
                    f"this check."
                )

            # Validate bypass roles against TURBODRF_ROLES.
            # Runs once per process at first init. Subsequent
            # inits (typically under override_settings in tests)
            # skip this — the real config has already been
            # checked, and partial role overrides shouldn't
            # spuriously fail the typo guard.
            if known_roles and not _bypass_roles_validated:
                for pred in _walk_predicates(predicates):
                    if isinstance(pred, Owner) and pred.bypass:
                        unknown = pred.bypass - known_roles
                        if unknown:
                            raise ImproperlyConfigured(
                                f"{model.__name__}.turbodrf() declares "
                                f"bypass_owner_roles={sorted(pred.bypass)} "
                                f"but {sorted(unknown)} are not in "
                                f"TURBODRF_ROLES. Typo or stale config?"
                            )

            if autodetected:
                logger.info(
                    f"Auto-detected tenant path for {model.__name__}: "
                    f"{tenant_field}"
                )

            self._register_model(model, config, predicates, tenant_field, logger)

        # Mark bypass-roles validation as complete for this process.
        # Subsequent inits skip the check.
        _bypass_roles_validated = True

        # Second pass: validate compiled-path safety. Must run AFTER the
        # main loop so every model's predicates / tenant_field are
        # registered — model A's M2M target may be model B, and B's
        # predicates may not have registered yet during A's compile.
        # See compiler.validate_compiled_path_safety for the full rule.
        from .compiler import _compiled_plans, validate_compiled_path_safety

        for compiled_model in list(_compiled_plans.keys()):
            validate_compiled_path_safety(compiled_model)

        # Third pass: validate searchable_fields safety. Same class of
        # bug as the compiled M2M target bypass — DRF's SearchFilter
        # joins to the target model without applying the target's own
        # predicates. Runs over every enabled TurboDRF-mixin model
        # regardless of compile mode.
        from .validation import validate_searchable_fields_safety

        for model in enabled:
            validate_searchable_fields_safety(model)

        # Fourth pass: validate Custom predicate write safety. A Custom
        # without a write_validator doesn't check writes against its read
        # rule. See predicates.validate_predicate_write_safety.
        from .predicates import validate_predicate_write_safety

        for model in enabled:
            validate_predicate_write_safety(model)

        # Fifth pass: validate every TURBODRF_ROLES permission string
        # resolves to a real model + field + action. Catches typos like
        # `core.project.titel.read` that silently grant nothing.
        from .predicates import validate_permission_strings

        validate_permission_strings()

    def _register_model(self, model, config, predicates, tenant_field, logger):
        """Build ``model``'s viewset from ``config`` (the top-level config, or
        its platform block), register it and compile its list path."""
        # Get custom endpoint or use default
        endpoint = config.get("endpoint", f"{model._meta.model_name}s")
        viewset_class = self._viewset_class(model, config, predicates, tenant_field)

        # Register the viewset
        self.register(
            endpoint,
            viewset_class,
            basename=self._route_basename(model, endpoint),
        )

        # Compile read path (on by default, opt out with compiled=False)
        if config.get("compiled", True):
            from .compiler import compile_model, register_compiled_plan

            try:
                plan = compile_model(model)
                if plan is not None:
                    register_compiled_plan(model, plan)
                    logger.info(f"Compiled read path for {model.__name__}")
            except Exception as e:
                # If compilation fails, fall back to DRF path silently
                logger.warning(
                    f"Could not compile {model.__name__}: {e}. "
                    f"Falling back to DRF serializer path."
                )

    def _viewset_class(self, model, config, predicates, tenant_field):
        """The generated viewset class for ``model``."""
        # Get lookup field if specified
        lookup_field = config.get("lookup_field", None)

        # Build viewset attributes. No class-level `queryset`: it would be
        # every tenant's rows (a custom action reading `self.queryset`
        # bypassed scoping). Rows come from get_queryset() only.
        platform = self.scope == PLATFORM
        viewset_attrs = {
            "model": model,
            "_predicates": predicates,
            "_tenant_field": tenant_field,
            "turbodrf_scope": self.scope,
            "turbodrf_gate": self.gate,
            "__module__": model.__module__,
            "__doc__": (
                f"Auto-generated {'platform ' if platform else ''}ViewSet for "
                f"{model.__name__} model."
            ),
        }

        # Add lookup_field if specified
        if lookup_field:
            viewset_attrs["lookup_field"] = lookup_field

        # Restrict HTTP methods: `read_only: True` (list/retrieve
        # only) or an explicit `http_methods` allow-list. Disallowed
        # verbs return 405. A read-only resource still serves its
        # declared actions on their own verbs (e.g. a POST action);
        # its CRUD write routes stay unrouted (see get_method_map).
        read_only = config.get("read_only", False)
        http_methods = config.get("http_methods")
        if read_only:
            methods = ["get", "head", "options"]
            for action_func in config.get("actions") or []:
                for method in getattr(action_func, "mapping", None) or {}:
                    if method not in methods:
                        methods.append(method)
            viewset_attrs["http_method_names"] = methods
            viewset_attrs["_read_only"] = True
        elif http_methods:
            methods = [m.lower() for m in http_methods]
            for always in ("head", "options"):
                if always not in methods:
                    methods.append(always)
            viewset_attrs["http_method_names"] = methods

        # Custom endpoints: attach @turbodrf_action-decorated
        # handlers from the config to the generated viewset. They
        # inherit get_object()/get_queryset() tenant + predicate
        # scoping like the CRUD routes. Reject any handler whose name
        # collides with a TurboDRFViewSet method/attribute — silently
        # overwriting e.g. get_queryset/authorize/scope would replace
        # the row-scoping chokepoint.
        reserved_names = set(dir(TurboDRFViewSet))
        for action_func in config.get("actions") or []:
            action_name = getattr(action_func, "__name__", None)
            if not action_name:
                continue
            if action_name in reserved_names:
                raise ImproperlyConfigured(
                    f"{model.__name__} turbodrf() action "
                    f"'{action_name}' collides with a TurboDRFViewSet "
                    f"method/attribute — rename the handler function."
                )
            viewset_attrs[action_name] = action_func

        # Create a custom viewset for this model
        suffix = "PlatformViewSet" if platform else "ViewSet"
        return type(f"{model.__name__}{suffix}", (TurboDRFViewSet,), viewset_attrs)

    def _discover_platform_models(self):
        """Register every model with an enabled ``"platform"`` block.

        Each viewset serves the block's config with no tenant boundary and no
        predicates (``_tenant_field = None``, ``_predicates = []``) on the
        platform scope. Tenancy is still resolved and registered for every
        model: the platform scope's write checks keep a row and the rows it
        points at in one tenant. Then the same startup gates as the tenant
        router run on the platform configs, under the platform scope, and
        ``TURBODRF_PLATFORM_ROLES`` is validated like ``TURBODRF_ROLES``.
        """
        import logging

        from .backends import platform_role_getter
        from .compiler import unregister_compiled_plan, validate_compiled_path_safety
        from .predicates import validate_permission_strings
        from .properties import validate_property_config
        from .tenancy import _register_tenancy
        from .validation import (
            validate_nesting_depth,
            validate_searchable_fields_safety,
        )

        logger = logging.getLogger(__name__)

        models = [m for m in apps.get_models() if issubclass(m, TurboDRFMixin)]
        configs = {model: model.turbodrf() for model in models}
        for model in models:
            _validate_config_keys(model, configs[model])
        resolved = _register_tenancy(models, configs)
        blocks = _validate_platform_blocks(models, configs, resolved)
        roles = _platform_roles_setting()
        platform_role_getter()  # a bad TURBODRF_PLATFORM_ROLE_GETTER fails now

        served = [m for m, block in blocks.items() if block.get("enabled", True)]
        with use_scope(PLATFORM):
            for model in served:
                block = blocks[model]
                for field in _configured_field_names(model, block):
                    try:
                        validate_nesting_depth(field)
                    except Exception as e:
                        logger.warning(
                            f"Model {model.__name__} platform field '{field}' "
                            f"validation failed: {str(e)}"
                        )
                validate_property_config(model)
                # A rebuilt router reflects the current block.
                unregister_compiled_plan(model)
                self._register_model(model, block, [], None, logger)
            for model in served:
                validate_compiled_path_safety(model)
                validate_searchable_fields_safety(model)
        validate_permission_strings(roles, setting="TURBODRF_PLATFORM_ROLES")

    def _route_basename(self, model, endpoint):
        """URL-name prefix for ``model``: its model name, or — when another
        app's model of the same name already has it — its (distinct)
        endpoint. Two models routed to one endpoint raise: the second would
        be unreachable, and DRF refuses a duplicate basename. On the platform
        scope the prefix starts with ``platform-`` (``platform-job-list``), so
        a tenant route's name still reverses to the tenant route."""
        for prefix, viewset, _basename in self.registry:
            if prefix == endpoint:
                raise ImproperlyConfigured(
                    f"{model._meta.label} and {viewset.model._meta.label} both "
                    f"route to '{endpoint}/'. Set a distinct 'endpoint' in one "
                    f"of their turbodrf() configs."
                )
        prefix = f"{PLATFORM}-" if self.scope == PLATFORM else ""
        basename = prefix + model._meta.model_name
        if self.is_already_registered(basename):
            basename = prefix + endpoint
        if self.is_already_registered(basename):
            raise ImproperlyConfigured(
                f"{model._meta.label}'s routes would be named "
                f"'{basename}-list' / '{basename}-detail', which another model "
                f"already uses. Set a distinct 'endpoint' in its turbodrf() "
                f"config."
            )
        return basename

    def get_method_map(self, viewset, method_map):
        """Bind HTTP methods to viewset actions, leaving the CRUD write
        actions unbound on a `read_only: True` resource (405), while its
        declared custom actions keep their verbs."""
        bound = super().get_method_map(viewset, method_map)
        if getattr(viewset, "_read_only", False):
            bound = {
                method: action
                for method, action in bound.items()
                if action not in _CRUD_WRITE_ACTIONS
            }
        return bound

    def get_urls(self):
        """
        Generate URL patterns that work with or without trailing slashes.

        This override ensures that POST requests work regardless of whether
        the client includes a trailing slash, avoiding the common Django
        redirect issue that loses POST data.
        """
        urls = super().get_urls()

        # Create duplicate patterns without trailing slashes
        additional_urls = []
        for url_pattern in urls:
            if hasattr(url_pattern, "pattern") and hasattr(
                url_pattern.pattern, "_regex"
            ):
                # Get the regex pattern
                regex = url_pattern.pattern._regex

                # If it ends with '/$', create a version without it
                if regex.endswith("/$"):
                    new_regex = regex[:-2] + "$"  # Remove / before $

                    # Create new URL pattern without trailing slash
                    new_pattern = re_path(
                        new_regex,
                        url_pattern.callback,
                        url_pattern.default_args,
                        url_pattern.name + "_no_slash" if url_pattern.name else None,
                    )
                    additional_urls.append(new_pattern)

        return urls + additional_urls
