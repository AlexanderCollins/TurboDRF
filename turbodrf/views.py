from django.conf import settings
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import status
from rest_framework.filters import OrderingFilter
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from .filter_backends import ORFilterBackend, TurboDRFSearchFilter
from .metadata import TurboDRFMetadata
from .permissions import (
    DefaultDjangoPermission,
    TurboDRFPermission,
    permissions_bypassed,
)
from .serializers import TurboDRFSerializer
from .tracking import get_viewset_base_classes

_UNSET = object()


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
        original two-layer code)."""
        if self.tenant_q is not None:
            queryset = queryset.filter(self.tenant_q)
        if self.predicate_q is not None:
            queryset = queryset.filter(self.predicate_q)
        return queryset


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
        DjangoFilterBackend,
        TurboDRFSearchFilter,
        OrderingFilter,
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

        return [
            TurboDRFRenderer() if type(r) is JSONRenderer else r for r in renderers
        ]

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

    # NOTE on @action routes: custom @action methods that call
    # self.get_object() or self.get_queryset() inherit scoping automatically.
    # If you bypass those (e.g. self.model.objects.get(...) directly), scoping
    # is bypassed too — you must apply it manually.

    def get_permissions(self):
        """Resolve permission instances per request from current settings.

        Evaluated at request time (not frozen on the class at import) so
        ``TURBODRF_DISABLE_PERMISSIONS`` / ``TURBODRF_USE_DEFAULT_PERMISSIONS``
        and ``override_settings`` take effect without a process restart.
        """
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
            return self.model.objects.all()
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
        base field names, or ``None`` when no role system applies (permissions
        disabled, or an anonymous caller on a public_access model with no
        ``guest`` role — the legacy pass-through). The compiled / search /
        ordering field gates derive from this so they cannot drift apart."""
        if permissions_bypassed():
            return None
        from .backends import attach_snapshot_to_request, get_user_roles

        user = getattr(request, "user", None)
        if not get_user_roles(user):
            return None
        snapshot = attach_snapshot_to_request(request, self.model)
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

        # F4: scope compiled FK-annotation JOINs to rows visible via the
        # target's own endpoint. No-op unless an unsafe-compiled bypass flag
        # exposed a predicate-bearing FK target (those are refused at startup).
        from django.db.models import Q as _Q

        from .validation import build_traversal_scope_q as _btsq

        _fk_scope = _Q()
        for _f_expr in plan.fk_annotations.values():
            _fk_scope &= _btsq(self.model, _f_expr.name, request)
        if _fk_scope:
            queryset = queryset.filter(_fk_scope)

        # Get readable fields from permission snapshot
        readable_fields = self._get_compiled_readable_fields(request)

        # Client-driven field selection via ?fields= parameter
        # Only fields already in the model's turbodrf() config are allowed
        requested_fields = self._parse_client_fields(request, plan)
        if requested_fields is not None:
            # Intersect with permission-allowed fields
            if readable_fields is not None:
                readable_fields = readable_fields & requested_fields
            else:
                readable_fields = requested_fields

        # Per-nested-field perm gates for the compiled path — both FK
        # and M2M sides. Filtering by BASE field alone would leak nested
        # fields the user shouldn't see.
        allowed_fk_keys = self._filter_compiled_fk_annotations(plan, request)
        allowed_m2m = self._filter_compiled_m2m_subfields(plan, request)

        # Apply .values() + F() annotations
        compiled_qs, active_plan = plan.apply_to_queryset(
            queryset,
            readable_fields,
            allowed_fk_keys=allowed_fk_keys,
            allowed_m2m_subfields=allowed_m2m,
        )

        # F4: scope the M2M merge's second query to targets visible via the
        # target's own endpoint (no-op for public M2M targets). active_plan[2]
        # is the active M2M spec map.
        from .validation import scoped_target_queryset as _stq

        _m2m_filters = {}
        for _m2m_name, _spec in active_plan[2].items():
            _scoped = _stq(_spec["related_model"], request)
            if _scoped is not None:
                _m2m_filters[_m2m_name] = _scoped.values("pk")

        # Paginate the .values() queryset (works because it's still a queryset)
        page = self.paginate_queryset(compiled_qs)
        if page is not None:
            data = plan.post_process(
                list(page), active_plan, m2m_target_filters=_m2m_filters
            )
            return self.get_paginated_response(data)

        data = plan.post_process(
            list(compiled_qs), active_plan, m2m_target_filters=_m2m_filters
        )
        return Response(data)

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
        for field in fields_param.split(","):
            field = field.strip()
            # Convert dot notation to underscore: author.name → author_name
            field = field.replace(".", "_")
            if field in allowed:
                requested.add(field)
            # Also check if it's a base field for FK/M2M
            # e.g. "author" should include the FK ID field
            for key in allowed:
                if key == field or key.startswith(field + "_"):
                    requested.add(key)

        # Include base FK fields for any requested FK annotations so that
        # apply_to_queryset (which filters FK annotations by base field
        # membership) keeps them in the active plan.
        for fk_key in plan.fk_annotations:
            if fk_key in requested:
                base = plan._fk_base_field(fk_key)
                if base:
                    requested.add(base)

        # Always include PK if M2M fields are requested (needed for merge)
        if any(f in plan.m2m_specs for f in requested):
            requested.add(plan.pk_field)

        return requested if requested else None

    def _get_compiled_readable_fields(self, request):
        """Readable BASE fields for the compiled path's filter step.

        Delegates to the single field-allowlist source
        ``_authorized_readable_fields`` so the compiled path and the row
        chokepoint agree.
        """
        return self._authorized_readable_fields(request)

    def _filter_compiled_fk_annotations(self, plan, request):
        """Per-nested-FK-path permission check for the compiled path.

        Each FK annotation is checked against its full `__`-joined path.
        Filtering by BASE field only would leak nested fields the user
        shouldn't see. Returns None when no permission system applies
        (anon without guest role configured).
        """
        if permissions_bypassed():
            return None

        from .backends import get_user_roles
        from .validation import is_field_visible_to_user

        user = getattr(request, "user", None)
        if not get_user_roles(user):
            return None  # legacy: no role system → no nested-FK filter

        allowed = set()
        for output_key, f_expr in plan.fk_annotations.items():
            if is_field_visible_to_user(self.model, f_expr.name, user):
                allowed.add(output_key)
        return allowed

    def _filter_compiled_m2m_subfields(self, plan, request):
        """Per-nested-M2M-field permission check for the compiled path.

        Mirror of `_filter_compiled_fk_annotations` for the M2M side. Returns
        a dict {m2m_base: {allowed_subfield, ...}} or None when no role
        system applies.
        """
        if permissions_bypassed():
            return None

        from .backends import get_user_roles
        from .validation import is_field_visible_to_user

        user = getattr(request, "user", None)
        if not get_user_roles(user):
            return None

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
            # Different fields for list and detail views
            if self.action == "list":
                fields_to_use = fields.get("list", "__all__")
            elif self.action in ["create", "update", "partial_update"]:
                # For write operations, use detail fields which
                # typically include all fields
                fields_to_use = fields.get("detail", "__all__")
            else:
                fields_to_use = fields.get("detail", "__all__")
        else:
            fields_to_use = fields

        # Store original fields before processing
        original_fields = (
            fields_to_use if isinstance(fields_to_use, list) else fields_to_use
        )

        # Process fields to separate simple and nested fields
        if isinstance(fields_to_use, list):
            simple_fields = []
            nested_fields = {}

            from .validation import is_field_path_sensitive

            for field in fields_to_use:
                # Strip sensitive at every segment of the path (I-2 fix).
                if is_field_path_sensitive(field):
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

        # Use permission-based field filtering for both read and write operations
        # This prevents validation errors from leaking information about fields
        # the user doesn't have permission to access
        use_default_perms = getattr(settings, "TURBODRF_USE_DEFAULT_PERMISSIONS", False)

        if (
            not use_default_perms
            and user
            and self.action
            in ["list", "retrieve", "create", "update", "partial_update"]
        ):
            from .backends import attach_snapshot_to_request
            from .serializers import TurboDRFSerializerFactory

            # Always build snapshot for permission checking
            # This works for all modes: static (via .roles or _test_roles),
            # database (via UserRole), and guest users
            snapshot = attach_snapshot_to_request(request, self.model)

            # Use factory if snapshot has any permissions
            # (This handles all modes including database without requiring
            # .roles property)
            if snapshot and (snapshot.allowed_actions or snapshot.readable_fields):
                # For write operations, pass appropriate view_type
                view_type = (
                    "detail"
                    if self.action in ["create", "update", "partial_update"]
                    else self.action
                )
                return TurboDRFSerializerFactory.create_serializer(
                    self.model,
                    original_fields,
                    user,
                    view_type=view_type,
                    snapshot=snapshot,
                )

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

        # Add select_related and prefetch_related optimizations
        # This is a simple implementation - could be enhanced
        config = self.model.turbodrf()
        fields = config.get("fields", [])

        if isinstance(fields, dict):
            fields = fields.get("list", []) + fields.get("detail", [])

        # Extract foreign key fields for select_related
        select_related_fields = []
        for field in fields:
            if "__" in field:
                # This is a related field
                base_field = field.split("__")[0]
                if base_field not in select_related_fields:
                    select_related_fields.append(base_field)

        if select_related_fields:
            queryset = queryset.select_related(*select_related_fields)

        # Route row-level access through the single authorization chokepoint.
        # `scope()` applies the MANDATORY tenant boundary and the DISCRETIONARY
        # within-tenant predicates as separate filters — tenant outside the
        # algebra, never OR-composable away by an Either().
        request = getattr(self, "request", None)
        return self.authorize(request).scope(queryset)

    def filter_queryset(self, queryset):
        """Run DRF's filter backends, then close the search/ordering JOIN-target
        leak: scope nested ``__``-paths used by an active search or ordering to
        rows the caller can see via the target's own endpoint.

        In any config that passed the startup safety gates this is a NO-OP (no
        searchable/orderable nested path reaches a predicate-bearing target, so
        ``build_traversal_scope_q`` returns an empty ``Q``). It bites only when
        an ``ALLOW_UNSAFE_*`` bypass flag exposed such a path — making those
        flags safe at REQUEST time, not just at boot.
        """
        queryset = super().filter_queryset(queryset)
        request = getattr(self, "request", None)
        if request is None:
            return queryset

        from django.db.models import Q

        from .validation import build_traversal_scope_q

        paths = set()
        if request.query_params.get("search"):
            for f in self.search_fields or []:
                base = f.lstrip("^=$@")  # strip DRF search-field prefixes
                if "__" in base:
                    paths.add(base)
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

        if permissions_bypassed():
            return list(base)

        request = getattr(self, "request", None)
        if request is None:
            return list(base)

        from .backends import get_user_roles
        from .validation import filter_readable_fields

        user = getattr(request, "user", None)
        # No roles → no permission system applies. Legacy: public_access
        # models with no guest role configured allow anon to search by
        # configured fields.
        if not get_user_roles(user):
            return list(base)
        return filter_readable_fields(self.model, list(base), user)

    @property
    def ordering_fields(self):
        """Fields available for ?ordering=. Restricted to fields the caller
        can read to prevent leaking hidden values via row order (an
        attacker without read perm on `salary` could otherwise sort by it
        and binary-search the value).

        Returns '__all__' only when permissions are disabled.
        """
        if permissions_bypassed():
            return "__all__"

        from .settings import TURBODRF_SENSITIVE_FIELDS as default_sensitive

        sensitive_fields = set(
            getattr(settings, "TURBODRF_SENSITIVE_FIELDS", default_sensitive)
        )

        readable = self._get_filterable_fields()
        if readable is None:
            # Anonymous / no snapshot — allow only non-sensitive concrete fields
            return [
                f.name
                for f in self.model._meta.fields
                if f.name not in sensitive_fields
            ]

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
        from .settings import TURBODRF_SENSITIVE_FIELDS as default_sensitive

        sensitive_fields = set(
            getattr(settings, "TURBODRF_SENSITIVE_FIELDS", default_sensitive)
        )

        # Get readable fields from permission snapshot (if available)
        readable_fields = self._get_filterable_fields()

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

        Returns None if permissions are disabled (all fields filterable),
        or a set of field names the user has read access to.
        """
        if permissions_bypassed():
            return None

        request = getattr(self, "request", None)
        if not request:
            return None

        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            # Unauthenticated users — check if guest role has field restrictions
            from .backends import build_permission_snapshot

            snapshot = build_permission_snapshot(user, self.model)
            if snapshot and snapshot.readable_fields:
                return snapshot.readable_fields
            return None

        from .backends import attach_snapshot_to_request

        snapshot = attach_snapshot_to_request(request, self.model)
        if snapshot and snapshot.readable_fields:
            return snapshot.readable_fields
        # Authenticated caller (already past has_permission, so they hold a role)
        # with zero readable fields → DENY ALL for ordering/filtering, not the
        # `None` "all non-sensitive fields" fallback.
        return set()

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
