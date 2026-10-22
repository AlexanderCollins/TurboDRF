from django.conf import settings
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from .filter_backends import (
    ORFilterBackend,
    TurboDRFFilterBackend,
    TurboDRFOrderingFilter,
    TurboDRFSearchFilter,
)
from .metadata import TurboDRFMetadata
from .permissions import (
    DefaultDjangoPermission,
    TurboDRFPermission,
    permissions_bypassed,
)
from .serializers import TurboDRFSerializer
from .tracking import get_viewset_base_classes

_UNSET = object()

# CRUD actions that write. Refused on a `read_only: True` resource.
_CRUD_WRITE_ACTIONS = frozenset({"create", "update", "partial_update", "destroy"})


class Authorization:
    """The single authorization decision for one request — the chokepoint every
    data route routes through.

    Produced once by ``TurboDRFViewSet.authorize()``. It carries the row-level
    scope (tenant + within-tenant predicate ``Q`` objects, applied by
    ``scope()``) and the field-level allowlist (``readable_fields``). The tenant
    ``Q`` is applied as a SEPARATE filter, outside the predicate algebra, so an
    ``Either()`` can never OR-compose it away.

    ``readable_fields`` is computed lazily so the hot ``get_queryset`` path does
    not build a permission snapshot it does not need.
    """

    __slots__ = ("tenant_q", "predicate_q", "_fields_fn", "_fields")

    def __init__(self, tenant_q, predicate_q, fields_fn):
        self.tenant_q = tenant_q
        self.predicate_q = predicate_q
        self._fields_fn = fields_fn
        self._fields = _UNSET

    @property
    def readable_fields(self):
        """Field-level allowlist (a set of base field names), or ``None`` when
        no role system applies. Computed on first access."""
        if self._fields is _UNSET:
            self._fields = self._fields_fn()
        return self._fields

    def scope(self, queryset):
        """Apply row-level access — the one place row scoping happens. Tenant
        then predicate, as two separate filters (behaviour-identical to the
        original two-layer code).

        A predicate that crosses a multi-valued relation (``Members``, or an
        ``Either`` ORing one with another column) is applied as a ``pk__in``
        subquery: a direct JOIN returns a row once per matching related row,
        duplicating list rows, inflating counts and making ``get_object()``
        raise MultipleObjectsReturned."""
        if self.tenant_q is not None:
            queryset = queryset.filter(self.tenant_q)
        if self.predicate_q is not None:
            if self.predicate_q and _q_spans_multivalued(
                queryset.model, self.predicate_q
            ):
                queryset = queryset.filter(
                    pk__in=queryset.model._base_manager.filter(self.predicate_q).values(
                        "pk"
                    )
                )
            else:
                queryset = queryset.filter(self.predicate_q)
        return queryset


def _requested_client_fields(request):
    """Output keys named in ``?fields=`` (``author.name`` -> ``author_name``)."""
    params = getattr(request, "query_params", None) or getattr(request, "GET", {})
    raw = params.get("fields") or ""
    return [f.strip().replace(".", "_") for f in raw.split(",") if f.strip()]


def _q_spans_multivalued(model, q):
    """True if any lookup in ``q`` traverses an M2M or reverse relation."""
    from django.core.exceptions import FieldDoesNotExist
    from django.db.models import Q

    for child in q.children:
        if isinstance(child, Q):
            if _q_spans_multivalued(model, child):
                return True
            continue
        current = model
        for part in child[0].split("__"):
            if current is None:
                break
            try:
                field = current._meta.get_field(part)
            except FieldDoesNotExist:
                break
            if field.many_to_many or field.one_to_many:
                return True
            current = field.related_model if field.is_relation else None
    return False


class TurboDRFPagination(PageNumberPagination):
    """
    Custom pagination class for TurboDRF API responses.

    Extends Django REST Framework's PageNumberPagination to provide
    a more structured response format with comprehensive pagination metadata.

    Configuration:
        - Default page size: 20 items
        - Maximum page size: 100 items
        - Page size can be customized via 'page_size' query parameter

    Response Format:
        {
            "pagination": {
                "next": "http://api.example.com/items/?page=3",
                "previous": "http://api.example.com/items/?page=1",
                "current_page": 2,
                "total_pages": 10,
                "total_items": 200
            },
            "data": [...]
        }

    Example Usage:
        GET /api/articles/?page=2&page_size=50
    """

    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100

    def get_paginated_response(self, data):
        """
        Create a paginated response with metadata.

        Overrides the default pagination response to include additional
        metadata that's useful for frontend pagination components.

        Args:
            data: The serialized page data.

        Returns:
            Response: A Response object containing pagination metadata
                     and the serialized data.
        """
        from rest_framework.response import Response

        return Response(
            {
                "pagination": {
                    "next": self.get_next_link(),
                    "previous": self.get_previous_link(),
                    "current_page": self.page.number,
                    "total_pages": self.page.paginator.num_pages,
                    "total_items": self.page.paginator.count,
                },
                "data": data,
            }
        )

    def get_paginated_response_schema(self, schema):
        """The OpenAPI schema of the envelope get_paginated_response returns."""
        link = {"type": "string", "nullable": True, "format": "uri"}
        integer = {"type": "integer"}
        return {
            "type": "object",
            "required": ["pagination", "data"],
            "properties": {
                "pagination": {
                    "type": "object",
                    "properties": {
                        "next": link,
                        "previous": link,
                        "current_page": integer,
                        "total_pages": integer,
                        "total_items": integer,
                    },
                },
                "data": schema,
            },
        }


# Get base classes with optional tracking mixin
_viewset_bases = get_viewset_base_classes()


def _is_resolvable_search_path(model, path):
    """True if `path` walks resolvable concrete fields on `model`.

    A search field must be a real model field (or a `__`-traversed chain
    of FK relations ending in a concrete field) so DRF's SearchFilter can
    build a valid `icontains` Q. An unresolvable path (typo, FK with no
    sub-field) raises FieldError at SQL-build time.
    """
    from django.core.exceptions import FieldDoesNotExist

    if not isinstance(path, str) or not path:
        return False
    if "." in path:  # DRF uses `__`, not `.`
        return False
    current = model
    parts = path.split("__")
    for i, part in enumerate(parts):
        try:
            field = current._meta.get_field(part)
        except FieldDoesNotExist:
            return False
        is_last = i == len(parts) - 1
        if is_last:
            return True
        if not field.is_relation or field.related_model is None:
            return False
        current = field.related_model
    return False


class TurboDRFViewSet(*_viewset_bases):
    """
    Base ViewSet for TurboDRF-enabled models with automatic configuration.

    This ViewSet provides automatic API endpoint generation with:
    - Dynamic serializer creation based on model configuration
    - Role-based field filtering and permissions
    - Automatic query optimization with select_related/prefetch_related
    - Built-in filtering, searching, and ordering
    - Pagination with detailed metadata

    The ViewSet reads configuration from the model's turbodrf() method
    and automatically configures serializers, permissions, and query
    optimizations based on that configuration.

    Features:
        - Dynamic field selection for list vs detail views
        - Automatic handling of nested field relationships
        - Permission-based field filtering per user role
        - Query optimization based on requested fields
        - Full CRUD operations with permission checking

    Model Configuration Example:
        class Article(models.Model):
            title = models.CharField(max_length=200)
            content = models.TextField()
            author = models.ForeignKey(User, on_delete=models.CASCADE)

            @classmethod
            def turbodrf(cls):
                return {
                    'fields': {
                        'list': ['id', 'title', 'author__name'],
                        'detail': [
                            'id', 'title', 'content',
                            'author__name', 'author__email'
                        ]
                    }
                }

            searchable_fields = ['title', 'content']

    Attributes:
        model: The Django model class (set automatically by TurboDRFRouter)
        permission_classes: Uses TurboDRFPermission for role-based access
        pagination_class: Uses TurboDRFPagination for structured responses
        filter_backends: Enables filtering, searching, and ordering
    """

    # Declared default for introspection; the ACTUAL permission instances are
    # resolved per-request in get_permissions() so settings (and
    # override_settings) take effect live instead of being frozen at import.
    permission_classes = [TurboDRFPermission]
    metadata_class = TurboDRFMetadata
    pagination_class = TurboDRFPagination
    filter_backends = [
        TurboDRFFilterBackend,
        TurboDRFSearchFilter,
        TurboDRFOrderingFilter,
        ORFilterBackend,
    ]

    def get_renderers(self):
        """Resolve renderers per request, upgrading stock JSONRenderer to the
        fast msgspec/orjson-backed TurboDRFRenderer when available.

        Unlike a class-level ``renderer_classes`` override, this respects the
        project's ``DEFAULT_RENDERER_CLASSES`` — e.g. a production config that
        removes BrowsableAPIRenderer keeps working, and a project's custom
        JSONRenderer subclass is left untouched (only the exact stock class
        is swapped).
        """
        renderers = super().get_renderers()
        try:
            from .renderers import FAST_JSON_AVAILABLE, TurboDRFRenderer
        except ImportError:
            return renderers
        if not FAST_JSON_AVAILABLE:
            return renderers
        from rest_framework.renderers import JSONRenderer

        return [TurboDRFRenderer() if type(r) is JSONRenderer else r for r in renderers]

    # Set custom swagger schema class for better OpenAPI documentation
    # This prevents custom actions from incorrectly showing all model fields
    try:
        from .swagger import TurboDRFSwaggerAutoSchema

        swagger_schema = TurboDRFSwaggerAutoSchema
    except ImportError:
        # drf-yasg not installed, skip swagger configuration
        pass

    model = None  # Will be set by the router
    _predicates = []  # Populated by router: within-tenant predicates only
    _tenant_field = None  # Populated by router: mandatory tenant boundary
    _read_only = False  # Set by router for `read_only: True` resources

    # NOTE on @action routes: custom @action methods that call
    # self.get_object() or self.get_queryset() inherit scoping automatically.
    # If you bypass those (e.g. self.model.objects.get(...) directly), scoping
    # is bypassed too — you must apply it manually.

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        # The router leaves CRUD write routes unbound on a `read_only: True`
        # resource; refuse them here too in case the viewset is routed some
        # other way.
        if self._read_only and self.action in _CRUD_WRITE_ACTIONS:
            from rest_framework.exceptions import MethodNotAllowed

            raise MethodNotAllowed(request.method)

    def get_permissions(self):
        """Resolve permission instances per request from current settings.

        Evaluated at request time (not frozen on the class at import) so
        ``TURBODRF_DISABLE_PERMISSIONS`` / ``TURBODRF_USE_DEFAULT_PERMISSIONS``
        and ``override_settings`` take effect without a process restart.

        A custom action declared with ``permission_classes`` (via
        ``@turbodrf_action(..., permission_classes=[...])``) uses exactly
        those, replacing the role check — as DRF's ``@action`` does. Row
        scoping still applies through ``get_object()`` / ``get_queryset()``.
        """
        # DRF passes an action's extra @action kwargs to as_view() as
        # initkwargs, which set them on the instance.
        declared = self.__dict__.get("permission_classes")
        if declared is not None:
            return [permission() for permission in declared]
        if getattr(settings, "TURBODRF_DISABLE_PERMISSIONS", False):
            return []
        if getattr(settings, "TURBODRF_USE_DEFAULT_PERMISSIONS", False):
            return [DefaultDjangoPermission()]
        return [TurboDRFPermission()]

    def _get_base_queryset(self):
        """Override point for subclasses that need a custom base
        queryset. Whatever this returns is passed through the tenant +
        predicate filter steps in `get_queryset` — the access layer
        cannot be skipped from this hook.
        """
        if self.model is not None:
            return self.model._default_manager.all()
        return super().get_queryset()

    def _get_predicate_q(self, request):
        """Build the AND'd Q expression from this viewset's WITHIN-TENANT
        predicates. The mandatory tenant boundary is applied separately by
        _get_tenant_q (it's a setting, not a predicate — kept outside the
        algebra so OR-composition can't escape it).

        If no predicates configured: Q() (no within-tenant restriction).
        If request is missing: _no_match_q() — fail closed.
        """
        from django.db.models import Q

        from .backends import get_user_roles
        from .predicates import _no_match_q

        if not self._predicates:
            return Q()
        if request is None:
            return _no_match_q()
        user_roles = set(get_user_roles(getattr(request, "user", None)))
        q = Q()
        for pred in self._predicates:
            q &= pred.q(request, user_roles)
        return q

    def _get_tenant_q(self, request):
        """Build the mandatory tenant-boundary Q. This is the LAYER 1 filter
        — applied to every queryset, not composable with predicates, not
        bypassable by any role."""
        from django.db.models import Q

        from .predicates import _no_match_q, get_user_tenant

        if not self._tenant_field:
            return Q()  # no tenant configured for this model
        if request is None:
            return _no_match_q()
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return _no_match_q()
        tenant = get_user_tenant(user)
        if tenant is None:
            return _no_match_q()
        return Q(**{self._tenant_field: tenant})

    def _authorized_readable_fields(self, request):
        """Single source for the field-level read allowlist: a set of readable
        base field names, or ``None`` when permissions are bypassed
        (``TURBODRF_DISABLE_PERMISSIONS`` / ``TURBODRF_USE_DEFAULT_PERMISSIONS``).
        Anonymous callers are gated too: on a public_access model their
        snapshot has model-level read, so field rules still apply. The
        compiled / search / ordering field gates derive from this so they
        cannot drift apart."""
        if permissions_bypassed():
            return None
        snapshot = self._caller_snapshot(request)
        if snapshot and snapshot.readable_fields:
            return snapshot.readable_fields
        # Roles present but ZERO readable fields → DENY ALL (empty set), not the
        # `None` pass-through. `None` means "no gating applies"; conflating the
        # two let the compiled path render every column for a model-read-only
        # role whose fields are all individually gated.
        return set()

    def authorize(self, request):
        """THE authorization chokepoint.

        Computes, once for ``request``, the row-level scope (mandatory tenant
        boundary + discretionary within-tenant predicates) and the field-level
        allowlist, returned as an :class:`Authorization`. Every data route —
        list/detail, filter, search, ordering, OPTIONS, compiled — derives its
        access decision from this single result so the pathways cannot drift.
        """
        tenant_q = self._get_tenant_q(request) if self._tenant_field else None
        predicate_q = self._get_predicate_q(request) if self._predicates else None
        return Authorization(
            tenant_q, predicate_q, lambda: self._authorized_readable_fields(request)
        )

    def list(self, request, *args, **kwargs):
        """List action with optional compiled read path.

        If the model has a compiled query plan, bypasses DRF serialization
        and uses .values() + F() annotations for significantly faster reads.
        """
        if self._should_use_compiled_path(request):
            return self._compiled_list(request)
        return super().list(request, *args, **kwargs)

    def _should_use_compiled_path(self, request):
        from .compiler import is_compiled

        if not is_compiled(self.model):
            return False
        # Don't use compiled path for browsable API
        if (
            hasattr(request, "accepted_renderer")
            and getattr(request.accepted_renderer, "format", None) == "api"
        ):
            return False
        return True

    def _compiled_list(self, request):
        from .compiler import get_compiled_plan

        plan = get_compiled_plan(self.model)

        # Get base queryset (preserves tenant scoping, custom managers)
        queryset = self.get_queryset()

        # Apply filter backends (search, filtering, ordering, OR filters)
        # Filters operate on the normal queryset before .values() is applied
        queryset = self.filter_queryset(queryset)

        # F4: a nested value renders only where every JOIN target along its
        # path is visible to the caller through the target's own endpoint, and
        # is NULL elsewhere. The row itself is the parent's: it stays listed
        # (as on detail), so lists, counts and detail agree.
        from .validation import build_traversal_scope_q as _btsq

        fk_visibility = {}
        for _key, _f_expr in plan.fk_annotations.items():
            _q = _btsq(self.model, _f_expr.name, request)
            if _q:
                fk_visibility[_key] = _q

        # Per-nested-field perm gates for the compiled path — both FK
        # and M2M sides. Filtering by BASE field alone would leak nested
        # fields the user shouldn't see.
        allowed_fk_keys = self._filter_compiled_fk_annotations(plan, request)
        allowed_m2m = self._filter_compiled_m2m_subfields(plan, request)

        # Get readable fields from permission snapshot. The snapshot only knows
        # concrete and M2M fields, so add the @property fields the caller may
        # read and the bases of readable nested paths (a reverse one-to-one is
        # no snapshot field) — gated exactly as on the serializer (detail)
        # path, so list and detail agree.
        readable_fields = self._get_compiled_readable_fields(request)
        if readable_fields is not None:
            readable_fields = set(readable_fields)
            if plan.property_fields:
                readable_fields |= self._readable_property_fields(plan, request)
            if allowed_fk_keys:
                readable_fields |= {plan._fk_base_field(k) for k in allowed_fk_keys}

        # Client-driven field selection via ?fields= parameter
        # Only fields already in the model's turbodrf() config are allowed
        requested_fields = self._parse_client_fields(request, plan)
        if requested_fields is not None:
            # Intersect with permission-allowed fields
            if readable_fields is not None:
                readable_fields = readable_fields & requested_fields
            else:
                readable_fields = requested_fields

        # Apply .values() + F() annotations
        compiled_qs, active_plan = plan.apply_to_queryset(
            queryset,
            readable_fields,
            allowed_fk_keys=allowed_fk_keys,
            allowed_m2m_subfields=allowed_m2m,
            fk_visibility=fk_visibility,
        )

        # F4: scope the M2M merge's second query to targets visible via the
        # target's own endpoint (no-op for public M2M targets), and null a
        # sub-field whose further hop is out of scope. active_plan[2] is the
        # active M2M spec map.
        from .validation import scoped_target_queryset as _stq

        _m2m_filters = {}
        _m2m_visibility = {}
        for _m2m_name, _spec in active_plan[2].items():
            _scoped = _stq(_spec["related_model"], request)
            if _scoped is not None:
                _m2m_filters[_m2m_name] = _scoped.values("pk")
            for _sub in _spec["sub_fields"]:
                _q = _btsq(
                    _spec["through_model"], f"{_spec['target_fk']}__{_sub}", request
                )
                if _q:
                    _m2m_visibility.setdefault(_m2m_name, {})[_sub] = _q

        def _render(rows):
            return plan.post_process(
                rows,
                active_plan,
                m2m_target_filters=_m2m_filters,
                request=request,
                m2m_visibility=_m2m_visibility,
            )

        # Paginate the .values() queryset (works because it's still a queryset)
        page = self.paginate_queryset(compiled_qs)
        if page is not None:
            return self.get_paginated_response(_render(list(page)))

        return Response(_render(list(compiled_qs)))

    def _parse_client_fields(self, request, plan):
        """Parse ?fields= parameter and validate against model config.

        Returns a set of allowed field names, or None if no ?fields= param.
        Client can use dot notation (author.name) or underscore (author_name).
        Only fields in the model's turbodrf() config are allowed.
        """
        fields_param = request.query_params.get("fields")
        if not fields_param:
            return None

        # Build the set of all configured field names (output keys)
        allowed = set(plan.simple_fields)
        allowed.update(plan.fk_annotations.keys())  # e.g. 'related_name'
        allowed.update(plan.m2m_specs.keys())  # e.g. 'categories'
        allowed.update(plan.property_fields.keys())

        # Parse client request — accept both dot and underscore notation
        requested = set()
        for field in _requested_client_fields(request):
            if field in allowed:
                requested.add(field)
            # An FK base selects its nested values too ("author" ->
            # author_name, author_email) — not every key sharing the prefix
            # ("char" must not select "char_null").
            for key in plan.fk_annotations:
                if plan._fk_base_field(key) == field:
                    requested.add(key)

        # Include base FK fields for any requested FK annotations so that
        # apply_to_queryset (which filters FK annotations by base field
        # membership) keeps them in the active plan — and renders the raw id,
        # as the serializer path does for a nested field.
        for fk_key in plan.fk_annotations:
            if fk_key in requested:
                base = plan._fk_base_field(fk_key)
                if base:
                    requested.add(base)

        return requested if requested else None

    def _select_client_fields(self, fields):
        """The configured ``fields`` the ``?fields=`` parameter selects, by
        output key (``author_name`` or ``author.name``, an FK base for all its
        nested values, an M2M for all its sub-fields). Unchanged without the
        parameter or when it selects nothing."""
        request = getattr(self, "request", None)
        wanted = set(_requested_client_fields(request)) if request else set()
        if not wanted:
            return fields
        selected = []
        for field in fields:
            base = field.split("__")[0]
            if field.replace("__", "_") in wanted or base in wanted:
                selected.append(field)
        return selected or fields

    def _get_compiled_readable_fields(self, request):
        """Readable BASE fields for the compiled path's filter step.

        Delegates to the single field-allowlist source
        ``_authorized_readable_fields`` so the compiled path and the row
        chokepoint agree.
        """
        return self._authorized_readable_fields(request)

    def _readable_property_fields(self, plan, request):
        """@property fields in the compiled plan that the caller may read.

        Same check the serializer path applies (``is_field_visible_to_user``:
        sensitive deny-list, then an explicit read rule if one exists, else
        model-level read), so compiled list and DRF detail render the same
        property keys.
        """
        from .validation import is_field_visible_to_user

        user = getattr(request, "user", None)
        return {
            name
            for name in plan.property_fields
            if is_field_visible_to_user(self.model, name, user)
        }

    def _filter_compiled_fk_annotations(self, plan, request):
        """Per-nested-FK-path permission check for the compiled path.

        Each FK annotation is checked against its full `__`-joined path.
        Filtering by BASE field only would leak nested fields the user
        shouldn't see. Returns None when permissions are bypassed.
        """
        if permissions_bypassed():
            return None

        from .validation import is_field_visible_to_user

        user = getattr(request, "user", None)
        allowed = set()
        for output_key, f_expr in plan.fk_annotations.items():
            if is_field_visible_to_user(self.model, f_expr.name, user):
                allowed.add(output_key)
        return allowed

    def _filter_compiled_m2m_subfields(self, plan, request):
        """Per-nested-M2M-field permission check for the compiled path.

        Mirror of `_filter_compiled_fk_annotations` for the M2M side. Returns
        a dict {m2m_base: {allowed_subfield, ...}} or None when permissions
        are bypassed.
        """
        if permissions_bypassed():
            return None

        from .validation import is_field_visible_to_user

        user = getattr(request, "user", None)
        allowed_subfields = {}
        for base_name, spec in plan.m2m_specs.items():
            allowed_subfields[base_name] = {
                sub
                for sub in spec["sub_fields"]
                if is_field_visible_to_user(self.model, f"{base_name}__{sub}", user)
            }
        return allowed_subfields

    def get_serializer_class(self):
        """
        Dynamically create a serializer class based on model configuration.

        This method generates a serializer at runtime that respects:
        - The model's field configuration from turbodrf()
        - Different field sets for list vs detail views
        - Nested field relationships using '__' notation
        - User permissions for field visibility

        The method handles both simple field lists and complex configurations
        with separate list/detail field sets. It automatically processes
        nested fields and ensures base fields are included when nested
        fields are requested.

        Returns:
            type: A dynamically created serializer class configured for
                 the current action (list/detail) and model.

        Field Configuration Examples:
            # Simple configuration (same fields for all views)
            'fields': ['id', 'title', 'author__name']

            # Complex configuration (different fields per view)
            'fields': {
                'list': ['id', 'title', 'author__name'],
                'detail': [
                    'id', 'title', 'content',
                    'author__name', 'author__bio'
                ]
            }

        Nested Field Handling:
            - 'author__name' requires 'author' to be included
            - Nested fields are collected and passed to the serializer
            - The serializer handles traversal and flattening
        """
        config = self.model.turbodrf()
        fields = config.get("fields", "__all__")

        # Handle different field configurations
        if isinstance(fields, dict):
            # Different fields for list and detail views. A view type left out
            # of the dict uses the other one — never "__all__", which exposed
            # (and accepted writes to) every column.
            list_fields = fields.get("list", fields.get("detail", []))
            detail_fields = fields.get("detail", list_fields)
            fields_to_use = list_fields if self.action == "list" else detail_fields
        else:
            fields_to_use = fields
        if fields_to_use == "__all__" and hasattr(self.model, "_meta"):
            # Resolve here so the sensitive deny-list below applies to it.
            fields_to_use = [f.name for f in self.model._meta.fields] + [
                f.name for f in self.model._meta.many_to_many
            ]

        # ?fields= selects among the configured fields on reads, as on the
        # compiled list.
        if self.action in ("list", "retrieve") and isinstance(fields_to_use, list):
            fields_to_use = self._select_client_fields(fields_to_use)

        # Store original fields before processing
        original_fields = (
            fields_to_use if isinstance(fields_to_use, list) else fields_to_use
        )

        # Process fields to separate simple and nested fields
        if isinstance(fields_to_use, list):
            simple_fields = []
            nested_fields = {}

            from django.core.exceptions import ValidationError

            from .validation import is_field_path_sensitive, validate_nesting_depth

            for field in fields_to_use:
                # Strip sensitive at every segment of the path (I-2 fix).
                if is_field_path_sensitive(field):
                    continue
                try:
                    validate_nesting_depth(field)
                except ValidationError:
                    continue
                if "__" in field:
                    # This is a nested field
                    base_field = field.split("__")[0]
                    if base_field not in nested_fields:
                        nested_fields[base_field] = []
                    nested_fields[base_field].append(field)
                else:
                    simple_fields.append(field)

            # Add base fields for nested fields if not already present
            for base_field in nested_fields:
                if base_field not in simple_fields:
                    simple_fields.append(base_field)

            fields_to_use = simple_fields

        # Check if we should use the factory for permission-based filtering
        request = getattr(self, "request", None)
        user = getattr(request, "user", None) if request else None

        # Permission-filtered serializer for every action that renders or
        # accepts fields — custom actions included (their get_serializer()
        # otherwise rendered every configured field) — and for every caller:
        # an anonymous caller on a public_access model is gated by its snapshot
        # too. Only bypassed permissions get the plain serializer.
        if not permissions_bypassed() and user and self.action != "destroy":
            from .backends import attach_snapshot_to_request
            from .serializers import TurboDRFSerializerFactory

            snapshot = attach_snapshot_to_request(request, self.model)
            view_type = "list" if self.action == "list" else "detail"
            return TurboDRFSerializerFactory.create_serializer(
                self.model,
                original_fields,
                user,
                view_type=view_type,
                snapshot=snapshot,
            )

        # @property / method fields render through TurboDRFSerializer's
        # sandbox (turbodrf.properties), not as plain ReadOnlyFields reading
        # the full instance.
        property_fields = []
        if isinstance(fields_to_use, list) and hasattr(self.model, "_meta"):
            from .serializers import _model_has_concrete_field

            property_fields = [
                f
                for f in fields_to_use
                if not _model_has_concrete_field(self.model, f)
                and hasattr(self.model, f)
            ]
            fields_to_use = [f for f in fields_to_use if f not in property_fields]

        # Create serializer class dynamically with unique name per action
        action = self.action or "default"
        serializer_name = f"{self.model.__name__}{action.capitalize()}Serializer"

        # Create unique ref_name for swagger
        if hasattr(self.model, "_meta"):
            ref_name = (
                f"{self.model._meta.app_label}_{self.model._meta.model_name}_{action}"
            )
        else:
            # Fallback for non-Django models (e.g., in tests)
            ref_name = f"{self.model.__name__}_{action}"

        serializer_class = type(
            serializer_name,
            (TurboDRFSerializer,),
            {
                "Meta": type(
                    "Meta",
                    (),
                    {
                        "model": self.model,
                        "fields": fields_to_use,
                        "_nested_fields": (
                            nested_fields if isinstance(fields_to_use, list) else {}
                        ),
                        "_property_fields": property_fields,
                        "ref_name": ref_name,  # Unique reference name
                    },
                ),
                "__module__": (
                    f"turbodrf.generated.{self.model._meta.app_label}"
                    if hasattr(self.model, "_meta")
                    else "turbodrf.generated"
                ),
            },
        )

        return serializer_class

    def get_queryset(self):
        """Base queryset with `select_related` for FK fields referenced in
        `turbodrf()` config. Predicate / tenant filters are AND'd onto the
        result by the layered access-control system below.

        SECURITY INVARIANT (must hold across all subclasses): the
        queryset returned by this method MUST have been passed through
        the tenant + predicate filter steps below. Subclasses that
        override `get_queryset` and short-circuit before the
        `_get_tenant_q` / `_get_predicate_q` calls bypass row scoping
        and leak cross-tenant data. If a subclass needs a custom base
        queryset, override `_get_base_queryset()` instead — this
        wrapper still applies the access layer.
        """
        # Unscoped base — see invariant in the docstring. This MUST be
        # passed through the tenant + predicate filter steps further
        # down before being returned to a request.
        unscoped = self._get_base_queryset()

        # Add default ordering by primary key to avoid pagination warnings
        queryset = unscoped if unscoped.ordered else unscoped.order_by("pk")

        # select_related the single-valued relation chains nested fields
        # traverse (an M2M or reverse FK can't be select_related: 500).
        nested = self._nested_relation_paths()
        if nested:
            queryset = queryset.select_related(*nested)

        # Route row-level access through the single authorization chokepoint.
        # `scope()` applies the MANDATORY tenant boundary and the DISCRETIONARY
        # within-tenant predicates as separate filters — tenant outside the
        # algebra, never OR-composable away by an Either().
        request = getattr(self, "request", None)
        queryset = self.authorize(request).scope(queryset)
        return self._annotate_nested_visibility(queryset, request)

    def _configured_nested_paths(self):
        """Every ``__`` field path in the model's config (list and detail)."""
        config = self.model.turbodrf()
        fields = config.get("fields", [])
        if isinstance(fields, dict):
            fields = list(fields.get("list", [])) + list(fields.get("detail", []))
        if not isinstance(fields, (list, tuple)):
            return []
        return [f for f in fields if isinstance(f, str) and "__" in f]

    def _single_valued_prefix(self, path):
        """The longest prefix of ``path`` made of single-valued relations
        (``holding__portfolio__name`` -> ``holding__portfolio``), or ''."""
        from django.core.exceptions import FieldDoesNotExist

        current, hops = self.model, []
        for part in path.split("__"):
            try:
                field = current._meta.get_field(part)
            except FieldDoesNotExist:
                break
            if not field.is_relation or field.many_to_many or field.one_to_many:
                break
            hops.append(part)
            current = field.related_model
        return "__".join(hops)

    def _nested_relation_paths(self):
        """The single-valued relation chains nested fields traverse."""
        paths = {self._single_valued_prefix(p) for p in self._configured_nested_paths()}
        paths.discard("")
        return sorted(paths)

    def _annotate_nested_visibility(self, queryset, request):
        """Annotate, per nested FK path whose JOIN targets are scoped, whether
        the caller can see those targets — the serializer renders the nested
        value as NULL where it can't (the compiled list does the same). One
        CASE per path in the same query: no per-row lookups."""
        from django.db.models import BooleanField, Case, Value, When

        from .validation import build_traversal_scope_q

        self._nested_visibility = {}
        if request is None:
            return queryset
        annotations = {}
        for path in self._configured_nested_paths():
            relation_path = path.rsplit("__", 1)[0]
            if relation_path in self._nested_visibility:
                continue
            # Only paths through single-valued relations: a CASE over an M2M
            # or reverse-FK join would duplicate rows (M2M renders are scoped
            # where they're rendered).
            if not self._single_valued_prefix(path).startswith(relation_path):
                continue
            q = build_traversal_scope_q(self.model, path, request)
            if not q:
                continue
            alias = f"_tdrf_visible_{len(self._nested_visibility)}"
            self._nested_visibility[relation_path] = alias
            annotations[alias] = Case(
                When(q, then=Value(True)),
                default=Value(False),
                output_field=BooleanField(),
            )
        return queryset.annotate(**annotations) if annotations else queryset

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["turbodrf_nested_visibility"] = getattr(self, "_nested_visibility", {})
        return context

    def filter_queryset(self, queryset):
        """Run DRF's filter backends, then close the ordering JOIN-target leak:
        scope nested ``__``-paths used by an active ordering to rows the
        caller can see via the target's own endpoint. (Search scopes its
        nested paths itself, inside its own filter — see
        TurboDRFSearchFilter.)
        """
        queryset = super().filter_queryset(queryset)
        request = getattr(self, "request", None)
        if request is None:
            return queryset

        from django.db.models import Q

        from .validation import build_traversal_scope_q

        paths = set()
        ordering = request.query_params.get("ordering", "") or ""
        if ordering:
            allowed_order = self.ordering_fields
            if isinstance(allowed_order, (list, tuple, set)):
                for token in ordering.split(","):
                    token = token.strip().lstrip("-")
                    if "__" in token and token in allowed_order:
                        paths.add(token)

        scope = Q()
        for path in paths:
            scope &= build_traversal_scope_q(self.model, path, request)
        if scope:
            queryset = queryset.filter(scope)
        return queryset

    @property
    def search_fields(self):
        """Search fields, intersected with the caller's read permissions.

        The raw `searchable_fields` list would leak any sensitive field
        listed in it via substring inference (`?search=guess`). The
        field-permission gate runs against every searchable field — a
        viewer without read permission on a field cannot search by it.

        Without a request attached (unit test / programmatic use), returns
        the raw list. The HTTP layer always has a request.
        """
        from .mixins import get_searchable_fields

        base = get_searchable_fields(self.model)
        if not base:
            return []
        # Drop entries that aren't resolvable model fields. A misconfigured
        # `searchable_fields = ['nonexistent']` would otherwise raise
        # FieldError at SQL-build time → uncaught 500. Operator-mistake
        # triggered, but failing closed (silent drop) is safer than 5xx.
        base = [f for f in base if _is_resolvable_search_path(self.model, f)]
        if not base:
            return []

        from .validation import is_field_path_sensitive

        # The sensitive deny-list applies even with permissions bypassed.
        base = [f for f in base if not is_field_path_sensitive(f.lstrip("^=$@"))]
        if permissions_bypassed():
            return base

        request = getattr(self, "request", None)
        if request is None:
            return base

        from .validation import filter_readable_fields

        return filter_readable_fields(
            self.model, list(base), getattr(request, "user", None)
        )

    @property
    def ordering_fields(self):
        """Fields available for ?ordering=. Restricted to fields the caller
        can read to prevent leaking hidden values via row order (an
        attacker without read perm on `salary` could otherwise sort by it
        and binary-search the value).

        With permissions bypassed: every configured field. Sensitive fields
        are never orderable.
        """
        from .validation import _get_sensitive_fields

        sensitive_fields = _get_sensitive_fields()
        if permissions_bypassed():
            readable = self._configured_base_fields()
        else:
            readable = self._get_filterable_fields() or set()
        return [name for name in readable if name not in sensitive_fields]

    def get_filterset_fields(self):
        """
        Define fields available for filtering with lookup expressions.

        This method dynamically generates filter configurations for all
        model fields with common lookup expressions like gte, lte, exact,
        icontains, etc. It also includes ManyToMany fields.

        Returns:
            dict: Field configurations with lookup expressions.

        API Usage:
            GET /api/articles/?author=1
            GET /api/articles/?created_at__gte=2024-01-01
            GET /api/articles/?title__icontains=django
            GET /api/articles/?price__gte=10&price__lte=100
            GET /api/products/?categories__slug=electronics

        Note:
            JSONField and BinaryField are excluded from automatic filtering
            as they require special handling that django-filter doesn't
            support out of the box.
        """
        from django.db import models

        filterset_fields = {}

        # Helper function to get lookups for a field
        def get_field_lookups(field):
            field_class_name = field.__class__.__name__

            # Skip fields that django-filter doesn't support or that
            # don't make sense to filter
            unsupported_fields = ["JSONField", "BinaryField", "FilePathField"]
            if field_class_name in unsupported_fields:
                return None

            # Also check by importing JSONField classes directly for extra safety
            try:
                from django.db.models import JSONField as ModelsJSONField

                if isinstance(field, ModelsJSONField):
                    return None
            except ImportError:
                pass

            # Check for PostgreSQL JSONField (older Django versions)
            try:
                from django.contrib.postgres.fields import JSONField as PGJSONField

                if isinstance(field, PGJSONField):
                    return None
            except ImportError:
                pass

            # Skip any field that has 'json' in its class name (case insensitive)
            # This catches custom JSONField implementations
            if "json" in field_class_name.lower():
                return None

            # Define lookups based on field type
            if isinstance(
                field, (models.IntegerField, models.DecimalField, models.FloatField)
            ):
                # Numeric fields get comparison lookups
                return ["exact", "gte", "lte", "gt", "lt"]
            elif isinstance(field, (models.DateField, models.DateTimeField)):
                # Date fields get date lookups
                return [
                    "exact",
                    "gte",
                    "lte",
                    "gt",
                    "lt",
                    "year",
                    "month",
                    "day",
                ]
            elif isinstance(field, models.BooleanField):
                # Boolean fields only need exact
                return ["exact"]
            elif isinstance(field, (models.CharField, models.TextField)):
                # Text fields get string lookups
                return [
                    "exact",
                    "icontains",
                    "istartswith",
                    "iendswith",
                ]
            elif isinstance(field, models.ForeignKey):
                # Foreign keys get exact lookup
                return ["exact"]
            elif isinstance(field, (models.FileField, models.ImageField)):
                # Skip FileField and ImageField - django-filter doesn't support them
                # Attempting to filter by these fields causes:
                # "AssertionError: ... resolved field 'X' with 'exact' lookup to an
                # unrecognized field type ImageField"
                return None
            elif isinstance(field, models.UUIDField):
                # UUID fields only support exact matching
                return ["exact", "isnull"]
            elif isinstance(field, models.GenericIPAddressField):
                # IP address fields support exact and startswith
                return ["exact", "istartswith"]
            else:
                # Default to exact lookup
                return ["exact"]

        # Get sensitive fields deny-list
        from .validation import _get_sensitive_fields

        sensitive_fields = _get_sensitive_fields()

        # Configured fields the caller can read (every configured field when
        # permissions are bypassed).
        readable_fields = self._get_filterable_fields()
        if readable_fields is None:
            readable_fields = self._configured_base_fields()

        # Get all regular fields from the model
        for field in self.model._meta.fields:
            if field.name in sensitive_fields:
                continue
            if readable_fields is not None and field.name not in readable_fields:
                continue
            lookups = get_field_lookups(field)
            if lookups:
                filterset_fields[field.name] = lookups

        # Get all ManyToMany fields
        for field in self.model._meta.many_to_many:
            if field.name in sensitive_fields:
                continue
            if readable_fields is not None and field.name not in readable_fields:
                continue
            # ManyToMany fields support filtering by ID and null checks
            # They also support filtering through related model fields via __ notation
            filterset_fields[field.name] = ["exact", "in", "isnull"]

        return filterset_fields

    def _get_filterable_fields(self):
        """Get the set of fields the current user can filter on.

        Returns None if permissions are bypassed (all fields filterable),
        otherwise the set of configured fields the caller can read (possibly
        empty — deny all). Columns left out of ``fields`` aren't filterable or
        orderable: a filter on a column the API doesn't expose is an oracle on
        its value.
        """
        if permissions_bypassed():
            return None

        request = getattr(self, "request", None)
        if not request:
            return None

        snapshot = self._caller_snapshot(request)
        readable = set(snapshot.readable_fields) if snapshot else set()
        return readable & self._configured_base_fields()

    def _caller_snapshot(self, request):
        """The caller's permission snapshot (anonymous when the request has no
        authenticated user attached)."""
        from .backends import attach_snapshot_to_request, build_permission_snapshot

        if getattr(request, "user", None) is None:
            return build_permission_snapshot(None, self.model)
        return attach_snapshot_to_request(request, self.model)

    def _configured_base_fields(self):
        """Base names of every field the model's ``turbodrf()`` config exposes
        (list and detail; ``"__all__"`` means every concrete and M2M field),
        plus the pk."""
        config = self.model.turbodrf() if hasattr(self.model, "turbodrf") else {}
        fields = config.get("fields", "__all__")
        if isinstance(fields, dict):
            groups = [fields.get("list", []), fields.get("detail", [])]
        else:
            groups = [fields]
        names = {self.model._meta.pk.name}
        for group in groups:
            if group == "__all__":
                names.update(f.name for f in self.model._meta.fields)
                names.update(f.name for f in self.model._meta.many_to_many)
            else:
                names.update(f.split("__")[0] for f in group)
        return names

    @property
    def filterset_fields(self):
        """Property wrapper for filterset_fields to work with
        DjangoFilterBackend."""
        return self.get_filterset_fields()

    def create(self, request, *args, **kwargs):
        """Create a model instance.

        We pre-fill tenant/owner FKs into request.data BEFORE serializer
        validation, because those FKs are typically non-null on the model
        and DRF's required-field check would otherwise reject requests that
        omit them. The serializer then runs its own validate_write and
        auto_fill on validated_data — the two stages have different roles:
          - view-level pre-fill: ensure required FKs exist for serializer
            validation (fills only if missing)
          - serializer-level auto_fill: always overwrite tenant FK after
            permission-stripping, so a stripped wrong-tenant value gets
            replaced with the correct one
        Both are needed for security AND ergonomics. See predicates.py.
        """
        data = self._prefill_required_fields(request)
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(
            serializer.data, status=status.HTTP_201_CREATED, headers=headers
        )

    def _prefill_required_fields(self, request):
        """Inject tenant/owner FKs into request.data so the serializer's
        required-field check passes. Only fills missing values — explicit
        user values pass through and are validated by the serializer.
        """
        from .predicates import Owner, get_user_tenant

        if not request.user.is_authenticated:
            return request.data

        # Bulk-array bodies and empty Content-Type produce request.data
        # shapes that aren't dicts (lists, strings). Pass them through
        # unchanged — DRF will reject downstream with a clean 400 instead
        # of crashing here on .copy() / dict() calls.
        if not isinstance(request.data, dict):
            return request.data

        try:
            data = request.data.copy()
        except AttributeError:
            data = dict(request.data)

        # Tenant from the mandatory layer (setting, not predicate)
        if self._tenant_field and "__" not in self._tenant_field:
            if self._tenant_field not in data:
                tenant = get_user_tenant(request.user)
                if tenant is not None:
                    data[self._tenant_field] = getattr(tenant, "pk", tenant)

        # Owner from within-tenant predicates
        for pred in self._predicates:
            if isinstance(pred, Owner) and len(pred.fields) == 1:
                field = pred.fields[0]
                if "__" not in field and field not in data:
                    data[field] = request.user.pk

        return data
