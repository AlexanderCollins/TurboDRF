import copy

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.db.models import Subquery
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from .backends import _permission_memo
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
from .scope import PLATFORM, TENANT, use_scope
from .serializers import TurboDRFSerializer
from .tracking import get_viewset_base_classes

# CRUD actions that write. Refused on a `read_only: True` resource.
_CRUD_WRITE_ACTIONS = frozenset({"create", "update", "partial_update", "destroy"})

# DRF SearchFilter prefixes TurboDRF accepts on a `searchable_fields` entry:
# istartswith, iexact and (Postgres) full-text search. `$` (regex) isn't: a
# client-supplied pattern can backtrack catastrophically.
_SEARCH_PREFIXES = "^=@"


class Authorization:
    """The row-level authorization decision for one request — the chokepoint
    every data route routes through.

    Produced once by ``TurboDRFViewSet.authorize()``. It carries the row-level
    scope (tenant + within-tenant predicate ``Q`` objects), applied by
    ``scope()``. The tenant ``Q`` is applied as a SEPARATE filter, outside the
    predicate algebra, so an ``Either()`` can never OR-compose it away. The
    field-level allowlist is the permission snapshot's
    (``TurboDRFViewSet._authorized_readable_fields``).
    """

    __slots__ = ("tenant_q", "predicate_q")

    def __init__(self, tenant_q, predicate_q):
        self.tenant_q = tenant_q
        self.predicate_q = predicate_q

    def scope(self, queryset):
        """Apply row-level access — the one place row scoping happens. Tenant
        then predicate, as two separate filters (behaviour-identical to the
        original two-layer code).

        A predicate that crosses a multi-valued relation (``Members``, or an
        ``Either`` ORing one with another column) can't be a plain filter: a
        direct JOIN returns a row once per matching related row, duplicating
        list rows, inflating counts and making ``get_object()`` raise
        MultipleObjectsReturned. Each such lookup becomes ``pk__in`` the rows
        it matches (``without_to_many_joins``); when that can't be done
        without changing the predicate's meaning, the whole predicate is
        applied as a ``pk__in`` subquery of the tenant-filtered queryset."""
        if self.tenant_q is not None:
            queryset = queryset.filter(self.tenant_q)
        if self.predicate_q is not None:
            model = queryset.model
            predicate_q = self.predicate_q
            if predicate_q and _q_spans_multivalued(model, predicate_q):
                flat = without_to_many_joins(model, predicate_q)
                if flat is not None and not _q_spans_multivalued(model, flat):
                    queryset = queryset.filter(flat)
                else:
                    # What's left still JOINs a to-many relation: match by pk.
                    rest = predicate_q if flat is None else flat
                    queryset = queryset.filter(
                        pk__in=queryset.filter(rest).values("pk")
                    )
            else:
                queryset = queryset.filter(predicate_q)
        return queryset


def _requested_client_fields(request):
    """Output keys named in ``?fields=`` (``author.name`` -> ``author_name``)."""
    params = getattr(request, "query_params", None) or getattr(request, "GET", {})
    raw = params.get("fields") or ""
    return [f.strip().replace(".", "_") for f in raw.split(",") if f.strip()]


def _select_configured_fields(fields, wanted):
    """The entries of a configured ``fields`` list that the ``?fields=`` output
    keys ``wanted`` select: by output key (``author_name``, sent as
    ``author_name`` or ``author.name``), and an FK / M2M base for all its
    nested entries. Empty when it selects nothing."""
    return [
        f
        for f in fields
        if f.replace("__", "_") in wanted or f.split("__")[0] in wanted
    ]


def _expression_inside(value):
    """True for a list, tuple or set (an ``__in`` or ``__range`` value) that
    holds an expression, such as ``F()``: it reads columns like one."""
    return isinstance(value, (list, tuple, set, frozenset)) and any(
        hasattr(item, "resolve_expression") for item in value
    )


class RowsMatching(Subquery):
    """An ``__in`` value: the pks of the rows one lookup matches, built from a
    plain lookup and value, so it refers to nothing outside itself."""


def independent_q(model, q):
    """``q`` as a condition that joins no M2M or reverse relation, so ANDed
    with another it shares no JOIN: its to-many lookups made subqueries
    (``without_to_many_joins``), else ``pk__in`` the rows it matches."""
    from django.db.models import Q

    if not _q_spans_multivalued(model, q):
        return q
    flat = without_to_many_joins(model, q)
    if flat is not None and not _q_spans_multivalued(model, flat):
        return flat
    return Q(pk__in=RowsMatching(model._base_manager.filter(q).order_by().values("pk")))


def _q_spans_multivalued(model, q):
    """True if any lookup in ``q`` may traverse an M2M or reverse relation.

    A child that isn't a ``(lookup, value)`` pair (an ``Exists()`` or other
    expression, say from a ``Custom`` predicate), or whose value is an
    expression other than a plain ``F()`` (whose path is checked like the
    lookup's) or a ``RowsMatching``, can't be inspected, so it counts as
    multi-valued: the subquery form is correct either way."""
    from django.db.models import F, Q

    for child in q.children:
        if isinstance(child, Q):
            if _q_spans_multivalued(model, child):
                return True
            continue
        if not isinstance(child, tuple) or not isinstance(child[0], str):
            return True
        value = child[1]
        if type(value) is F:
            if _to_many_prefix(model, value.name):
                return True
        elif _expression_inside(value) or (
            hasattr(value, "resolve_expression") and not isinstance(value, RowsMatching)
        ):
            return True
        if _to_many_prefix(model, child[0]):
            return True
    return False


def _to_many_prefix(model, path):
    """The part of lookup ``path`` up to and including its first M2M or
    reverse relation (``team__members`` of ``team__members__name``), or
    ``None`` when it crosses none."""
    from django.core.exceptions import FieldDoesNotExist

    current, walked = model, []
    for part in path.split("__"):
        if current is None:
            break
        try:
            field = current._meta.get_field(part)
        except FieldDoesNotExist:
            break
        walked.append(part)
        if field.many_to_many or field.one_to_many:
            return "__".join(walked)
        current = field.related_model if field.is_relation else None
    return None


def _leaves(q):
    """The ``(lookup, value)`` children of ``q`` at any depth, or ``None`` if
    one isn't such a pair (an expression) or a node is an XOR."""
    from django.db.models import Q

    if q.connector == "XOR":
        return None  # its meaning changes when its operands move apart
    leaves = []
    for child in q.children:
        if isinstance(child, Q):
            nested = _leaves(child)
            if nested is None:
                return None
            leaves.extend(nested)
        elif isinstance(child, tuple) and isinstance(child[0], str):
            leaves.append(child)
        else:
            return None
    return leaves


def _rebuilt(q, rewrite):
    """A copy of ``q`` with each ``(lookup, value)`` child replaced by
    ``rewrite(child)``."""
    from django.db.models import Q

    children = [
        _rebuilt(child, rewrite) if isinstance(child, Q) else rewrite(child)
        for child in q.children
    ]
    return Q(*children, _connector=q.connector, _negated=q.negated)


def without_to_many_joins(model, q):
    """``q`` with its lookups across M2M or reverse relations replaced by
    ``pk__in`` the rows each matches (``RowsMatching``), where that keeps
    ``q``'s meaning. The outer query then has no to-many JOIN for them:
    nothing repeats, and no ``pk__in`` wrap of the whole predicate (an OR
    across a JOIN, which defeats indexes) is needed.

    - A lookup under a NOT: Django matches each one on its own, in a subquery
      of its own (as ``exclude()`` does), so this is the same rows. It also
      keeps Django's subquery from reusing another JOIN to the same table by
      mistake (its ``split_exclude`` alias reuse returned rows a rule hid).
    - Any other lookup only when no other lookup runs through its relation:
      in one ``filter()`` they'd share a JOIN, one related row matching both.

    Lookups it can't rewrite are left as they are: the caller checks the
    result with ``_q_spans_multivalued``. ``None`` when ``q`` holds an XOR (its
    meaning changes when its operands move apart) or an expression it can't
    inspect (one inside a list included); only a plain ``F()`` on a
    single-valued path, or a ``RowsMatching``, is kept."""
    from collections import Counter

    from django.db.models import F, Q

    entries = []  # ((lookup, value), under a NOT)

    def walk(node, negated):
        if node.connector == "XOR":
            return False
        negated = negated or node.negated
        for child in node.children:
            if isinstance(child, Q):
                if not walk(child, negated):
                    return False
            elif isinstance(child, tuple) and isinstance(child[0], str):
                entries.append((child, negated))
            else:
                return False
        return True

    if not walk(q, False):
        return None
    positive, negative = Counter(), set()
    for (lookup, value), negated in entries:
        prefix = _to_many_prefix(model, lookup)
        if type(value) is F:
            if prefix or _to_many_prefix(model, value.name):
                return None
        elif _expression_inside(value) or (
            hasattr(value, "resolve_expression") and not isinstance(value, RowsMatching)
        ):
            return None
        if prefix:
            if negated:
                negative.add(prefix)
            else:
                positive[prefix] += 1

    def rewrite(child, negated):
        lookup, value = child
        prefix = _to_many_prefix(model, lookup)
        if not prefix:
            return child
        if negated and positive[prefix]:
            return child  # tied to a positive lookup's JOIN: left to Django
        if not negated and (positive[prefix] > 1 or prefix in negative):
            return child  # shares a JOIN with another lookup
        rows = model._base_manager.filter(**{lookup: value}).order_by()
        return ("pk__in", RowsMatching(rows.values("pk")))

    def rebuild(node, negated):
        negated = negated or node.negated
        children = [
            rebuild(child, negated) if isinstance(child, Q) else rewrite(child, negated)
            for child in node.children
        ]
        return Q(*children, _connector=node.connector, _negated=node.negated)

    return rebuild(q, False)


def prefixed(q, prefix):
    """``q`` (a related model's scope) moved onto relation ``prefix`` of
    another model: each lookup and plain ``F()`` starts with ``prefix__``.
    ``None`` if a child can't be moved: an XOR, an expression, or a value
    other than a plain ``F()`` or a ``RowsMatching`` that holds one."""
    from django.db.models import F

    leaves = _leaves(q)
    if leaves is None:
        return None
    for _lookup, value in leaves:
        if _expression_inside(value) or (
            hasattr(value, "resolve_expression")
            and not (type(value) is F or isinstance(value, RowsMatching))
        ):
            return None

    def move(child):
        lookup, value = child
        if type(value) is F:
            value = F(f"{prefix}__{value.name}")
        return (f"{prefix}__{lookup}", value)

    return _rebuilt(q, move)


def _resolve_row_scoping(model):
    """``(tenant_field, predicates)`` for ``model`` from its registration:
    ``ready()`` registers every TurboDRF model. ``None`` for anything else:
    the caller fails closed."""
    from .predicates import (
        _model_predicates,
        _model_tenant_fields,
        get_predicates,
        get_tenant_field,
    )

    if model in _model_predicates or model in _model_tenant_fields:
        return get_tenant_field(model), list(get_predicates(model))
    return None


def _filter_lookups(field):
    """The django-filter lookups offered on a concrete model field, or None
    for a field django-filter can't filter (JSON, binary, file, file path)."""
    if isinstance(
        field,
        (
            models.JSONField,
            models.BinaryField,
            models.FilePathField,
            models.FileField,
        ),
    ):
        return None
    # A custom JSON field that isn't a JSONField subclass.
    if "json" in field.__class__.__name__.lower():
        return None
    if isinstance(field, (models.IntegerField, models.DecimalField, models.FloatField)):
        return ["exact", "gte", "lte", "gt", "lt"]
    if isinstance(field, (models.DateField, models.DateTimeField)):
        return ["exact", "gte", "lte", "gt", "lt", "year", "month", "day"]
    if isinstance(field, (models.CharField, models.TextField)):
        return ["exact", "icontains", "istartswith", "iendswith"]
    if isinstance(field, models.UUIDField):
        return ["exact", "isnull"]
    if isinstance(field, models.GenericIPAddressField):
        return ["exact", "istartswith"]
    # Booleans, foreign keys, everything else: exact.
    return ["exact"]


def configured_pagination_class():
    """TURBODRF_PAGINATION_CLASS (a dotted path or a class), else
    TurboDRFPagination."""
    from django.utils.module_loading import import_string

    setting = getattr(settings, "TURBODRF_PAGINATION_CLASS", None)
    if setting is None:
        return TurboDRFPagination
    if not isinstance(setting, str):
        return setting
    try:
        return import_string(setting)
    except ImportError as exc:
        raise ImproperlyConfigured(
            f"TURBODRF_PAGINATION_CLASS={setting!r} can't be imported: {exc}"
        ) from exc


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


def _split_search_prefix(entry):
    """``'^title'`` -> ``('^', 'title')``; ``'title'`` -> ``('', 'title')``.
    The prefix is any one DRF SearchFilter prefix character (``^ = @ $``)."""
    if isinstance(entry, str) and entry[:1] in "^=@$":
        return entry[0], entry[1:]
    return "", entry


def _is_resolvable_search_path(model, path):
    """True if ``path`` (a ``searchable_fields`` entry) can be searched.

    It may carry one DRF SearchFilter prefix TurboDRF accepts (``^``, ``=``,
    ``@``; not ``$``, a regex). The rest must walk resolvable fields on
    ``model``: a concrete column, or a ``__``-traversed chain of relations
    ending on one, so SearchFilter can build a valid lookup. A path ending on a
    relation (``author``) has no column to match text against, and an
    unresolvable one (a typo) raises FieldError at SQL-build time.
    """
    from django.core.exceptions import FieldDoesNotExist

    prefix, path = _split_search_prefix(path)
    if prefix and prefix not in _SEARCH_PREFIXES:
        return False
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
        if i == len(parts) - 1:
            return not field.is_relation
        if not field.is_relation or field.related_model is None:
            return False
        current = field.related_model
    return False


# Get base classes with optional tracking mixin
_viewset_bases = get_viewset_base_classes()


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

    @property
    def paginator(self):
        """DRF's paginator. TURBODRF_PAGINATION_CLASS (read per request, as
        the permission settings are) replaces the default class; a
        ``pagination_class`` a subclass or an action sets is kept."""
        if not hasattr(self, "_paginator"):
            cls = self.pagination_class
            if cls is TurboDRFPagination:
                cls = configured_pagination_class()
            self._paginator = cls() if cls is not None else None
            ordering = getattr(self._paginator, "ordering", None)
            if ordering:
                # A cursor's links carry its ordering column's values: only a
                # column the caller may order by, as _default_ordering.
                from .filter_backends import readable_ordering

                terms = [ordering] if isinstance(ordering, str) else list(ordering)
                self._paginator.ordering = readable_ordering(
                    self.model, terms, set(self.ordering_fields)
                )
        return self._paginator

    filter_backends = [
        TurboDRFFilterBackend,
        TurboDRFSearchFilter,
        TurboDRFOrderingFilter,
        ORFilterBackend,
    ]

    def get_renderers(self):
        """Resolve renderers per request, upgrading stock JSONRenderer to the
        fast msgspec/orjson-backed TurboDRFRenderer when available, and the
        stock BrowsableAPIRenderer to one with the raw-data form only
        (``TurboDRFBrowsableAPIRenderer``).

        Unlike a class-level ``renderer_classes`` override, this respects the
        project's ``DEFAULT_RENDERER_CLASSES`` — e.g. a production config that
        removes BrowsableAPIRenderer keeps working, and a project's custom
        JSONRenderer subclass is left untouched (only the exact stock class
        is swapped).
        """
        from rest_framework.renderers import BrowsableAPIRenderer, JSONRenderer

        from .renderers import (
            FAST_JSON_AVAILABLE,
            TurboDRFBrowsableAPIRenderer,
            TurboDRFRenderer,
        )

        swaps = {BrowsableAPIRenderer: TurboDRFBrowsableAPIRenderer}
        if FAST_JSON_AVAILABLE:
            swaps[JSONRenderer] = TurboDRFRenderer
        return [
            swaps[type(r)]() if type(r) in swaps else r for r in super().get_renderers()
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
    # Populated by the router: within-tenant predicates only, and the
    # mandatory tenant boundary. A hand-written subclass that sets neither
    # gets its model's (see _ensure_row_scoping).
    _predicates = []
    _tenant_field = None
    _read_only = False  # Set by router for `read_only: True` resources

    # The scope this viewset serves ("tenant" or "platform", see
    # turbodrf.scope), and the router's gate: permission classes checked
    # before every other permission on every route and action. Set by the
    # router; a mixin can read them (e.g. to audit platform reads).
    turbodrf_scope = TENANT
    turbodrf_gate = ()

    # NOTE on @action routes: custom @action methods that call
    # self.get_object() or self.get_queryset() inherit scoping automatically.
    # If you bypass those (e.g. self.model.objects.get(...) directly), scoping
    # is bypassed too — you must apply it manually.

    def dispatch(self, request, *args, **kwargs):
        # Roles and permission snapshots are looked up once per request (see
        # backends._permission_memo), not once per field / filter / hop.
        # Everything runs in the viewset's scope; a platform response is also
        # rendered in it (a renderer, such as the browsable API's forms, calls
        # back into the viewset), not later by Django outside the scope.
        with use_scope(self.turbodrf_scope), _permission_memo():
            response = super().dispatch(request, *args, **kwargs)
            if self.turbodrf_scope != TENANT:
                render = getattr(response, "render", None)
                if callable(render) and not getattr(response, "is_rendered", True):
                    render()
            return response

    def check_permissions(self, request):
        """The gate first, then the route's own permissions. The gate is
        checked here rather than added to ``get_permissions()``, so it holds
        on every route and action (OPTIONS and the browsable API's forms
        included) however a subclass or an action's ``permission_classes``
        builds that list."""
        self._check_gate(request)
        super().check_permissions(request)

    def check_object_permissions(self, request, obj):
        for permission in self._gate_permissions():
            if not permission.has_object_permission(request, self, obj):
                self.permission_denied(
                    request,
                    message=getattr(permission, "message", None),
                    code=getattr(permission, "code", None),
                )
        super().check_object_permissions(request, obj)

    def _gate_permissions(self):
        return [permission() for permission in self.turbodrf_gate]

    def _check_gate(self, request):
        from django.core.exceptions import ImproperlyConfigured

        if self.turbodrf_scope == PLATFORM and not self.turbodrf_gate:
            # A platform viewset built outside TurboDRFRouter(scope="platform",
            # gate=[...]): refuse rather than serve every tenant's rows.
            raise ImproperlyConfigured(
                f"{type(self).__name__} serves the platform scope without a "
                f"gate. Set turbodrf_gate (TurboDRFRouter(scope='platform', "
                f"gate=[...]) does)."
            )
        for permission in self._gate_permissions():
            if not permission.has_permission(request, self):
                self.permission_denied(
                    request,
                    message=getattr(permission, "message", None),
                    code=getattr(permission, "code", None),
                )

    def handle_exception(self, exc):
        from django.core.exceptions import RequestDataTooBig, TooManyFieldsSent
        from django.http import Http404
        from rest_framework import exceptions

        if isinstance(exc, Http404):
            # get_object_or_404's message names the model ("No Deal matches
            # the given query."): every 404 gets the generic one.
            exc = exceptions.NotFound()
        elif isinstance(exc, OverflowError):
            # Django < 5.0 on SQLite: an integer lookup past the column's range
            # raises instead of matching nothing.
            if (self.lookup_url_kwarg or self.lookup_field) in self.kwargs:
                exc = exceptions.NotFound()
            else:
                exc = exceptions.ValidationError({"detail": "Value out of range."})
        elif isinstance(exc, (TooManyFieldsSent, RequestDataTooBig)):
            # Raised while DRF reads the request; a 400, not a 500.
            exc = exceptions.ParseError("Request has too many fields or is too big.")
        elif isinstance(exc, RecursionError):
            # A JSON body nested thousands deep, raised while DRF parses it.
            exc = exceptions.ParseError("Request body is nested too deeply.")
        return super().handle_exception(exc)

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        # The router leaves CRUD write routes unbound on a `read_only: True`
        # resource; refuse them here too in case the viewset is routed some
        # other way.
        if self._read_only and self.action in _CRUD_WRITE_ACTIONS:
            from rest_framework.exceptions import MethodNotAllowed

            raise MethodNotAllowed(request.method)
        self._check_lookup_readable(request)

    def _check_lookup_readable(self, request):
        """A detail URL by a ``lookup_field`` other than the pk is a match on
        that column's value: a caller who can't read the column gets a 404,
        or the URL would confirm guesses of it. A value with a NUL is a 404
        too (on PostgreSQL, a database error)."""
        from rest_framework.exceptions import NotFound

        lookup = self.lookup_field
        if "\x00" in str(self.kwargs.get(self.lookup_url_kwarg or lookup, "")):
            raise NotFound()
        if (
            lookup in ("pk", self.model._meta.pk.name)
            or (self.lookup_url_kwarg or lookup) not in self.kwargs
            or permissions_bypassed()
        ):
            return
        snapshot = self._caller_snapshot(request)
        if snapshot.has_read_rule(lookup) and not snapshot.can_read_field(lookup):
            raise NotFound()

    def perform_destroy(self, instance):
        """A delete passes the write checks a write would (see
        ``serializers._delete_errors``)."""
        from rest_framework.exceptions import PermissionDenied

        from .serializers import _delete_errors

        errors = _delete_errors(self.model, instance, self.request)
        if errors:
            raise PermissionDenied(errors[0])
        super().perform_destroy(instance)

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
        if self.turbodrf_scope == PLATFORM:
            # The bypass settings are the tenant scope's (permissions_bypassed).
            return [TurboDRFPermission()]
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
        """The AND of this viewset's WITHIN-TENANT predicates. The mandatory
        tenant boundary is applied separately (``_get_tenant_q``): it's a
        setting, not a predicate, kept outside the algebra so OR-composition
        can't escape it."""
        from .predicates import predicates_q

        return predicates_q(self._predicates, request, self.model)

    def _get_tenant_q(self, request):
        """The mandatory tenant boundary: applied to every queryset, not
        composable with predicates, not bypassable by any role."""
        from .predicates import tenant_q

        return tenant_q(self._tenant_field, request)

    def _authorized_readable_fields(self, request):
        """Single source for the field-level read allowlist: a set of readable
        base field names, or ``None`` when permissions are bypassed
        (``TURBODRF_DISABLE_PERMISSIONS`` / ``TURBODRF_USE_DEFAULT_PERMISSIONS``).
        Anonymous callers are gated too: on a public_access model their
        snapshot has model-level read, so field rules still apply. The
        search and ordering field gates derive from this so they cannot drift
        apart."""
        if permissions_bypassed():
            return None
        snapshot = self._caller_snapshot(request)
        if snapshot and snapshot.readable_fields:
            return snapshot.readable_fields
        # Roles present but ZERO readable fields → DENY ALL (empty set), not the
        # `None` pass-through. `None` means "no gating applies"; conflating the
        # two would render every column for a model-read-only role whose
        # fields are all individually gated.
        return set()

    def authorize(self, request):
        """THE row-level authorization chokepoint.

        Computes, once for ``request``, the row-level scope (mandatory tenant
        boundary + discretionary within-tenant predicates), returned as an
        :class:`Authorization`. Every data route — list/detail, filter,
        search, ordering, OPTIONS — gets its rows through it, so the
        pathways cannot drift. Fails closed (no rows) when the viewset's row
        scoping can't be resolved.
        """
        if not self._ensure_row_scoping():
            from .predicates import _no_match_q

            return Authorization(_no_match_q(), None)
        tenant_q = self._get_tenant_q(request) if self._tenant_field else None
        predicate_q = self._get_predicate_q(request) if self._predicates else None
        return Authorization(tenant_q, predicate_q)

    def _ensure_row_scoping(self):
        """Make ``_tenant_field`` / ``_predicates`` hold this viewset's row
        scoping. The router sets them on the viewsets it builds; a hand-written
        ``TurboDRFViewSet`` subclass that sets neither gets its model's, from
        the model's registration. False when they can't be resolved."""
        names = ("_tenant_field", "_predicates")
        if any(name in self.__dict__ for name in names):
            return True
        for klass in type(self).__mro__:
            if klass is TurboDRFViewSet:
                break
            if any(name in vars(klass) for name in names):
                return True
        resolved = _resolve_row_scoping(self.model)
        if resolved is None:
            return False
        self._tenant_field, self._predicates = resolved
        return True

    def _select_client_fields(self, fields):
        """The configured ``fields`` the ``?fields=`` parameter selects (see
        _select_configured_fields). Unchanged without the parameter or when it
        selects nothing."""
        request = getattr(self, "request", None)
        wanted = set(_requested_client_fields(request)) if request else set()
        if not wanted:
            return fields
        return _select_configured_fields(fields, wanted) or fields

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
        fields_to_use = self._configured_view_fields(
            "list" if self.action == "list" else "detail"
        )

        if isinstance(fields_to_use, list):
            from django.core.exceptions import ValidationError

            from .validation import is_field_path_sensitive, validate_nesting_depth

            # Strip sensitive at every segment of the path (I-2 fix), and
            # paths deeper than TURBODRF_MAX_NESTING_DEPTH.
            renderable = []
            for field in fields_to_use:
                if is_field_path_sensitive(field):
                    continue
                try:
                    validate_nesting_depth(field)
                except ValidationError:
                    continue
                renderable.append(field)
            fields_to_use = renderable

            # ?fields= selects among the configured fields on reads.
            if self.action in ("list", "retrieve"):
                fields_to_use = self._select_client_fields(fields_to_use)

        configured_fields = fields_to_use

        # Process fields to separate simple and nested fields
        if isinstance(fields_to_use, list):
            simple_fields = []
            nested_fields = {}

            for field in fields_to_use:
                if "__" in field:
                    # This is a nested field
                    base_field = field.split("__")[0]
                    if base_field not in nested_fields:
                        nested_fields[base_field] = []
                    nested_fields[base_field].append(field)
                else:
                    simple_fields.append(field)

            # Add base fields for nested fields if not already present
            from .validation import is_reverse_one_to_one

            for base_field in nested_fields:
                if base_field not in simple_fields and not is_reverse_one_to_one(
                    self.model, base_field
                ):
                    simple_fields.append(base_field)

            fields_to_use = simple_fields

        # Check if we should use the factory for permission-based filtering
        request = getattr(self, "request", None)

        # Permission-filtered serializer for every action that renders or
        # accepts fields — custom actions included (their get_serializer()
        # otherwise rendered every configured field) — and for every caller:
        # an anonymous caller is gated by its snapshot too, whether DRF
        # represents it as AnonymousUser or None (UNAUTHENTICATED_USER: None).
        # Only bypassed permissions (or no request at all: programmatic use)
        # get the plain serializer.
        if (
            not permissions_bypassed()
            and request is not None
            and self.action != "destroy"
        ):
            from .serializers import TurboDRFSerializerFactory

            snapshot = self._caller_snapshot(request)
            view_type = "list" if self.action == "list" else "detail"
            return TurboDRFSerializerFactory.create_serializer(
                self.model,
                configured_fields,
                getattr(request, "user", None),
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

    def _configured_view_fields(self, view_type):
        """The model's configured fields for ``view_type`` (``'list'`` or
        ``'detail'``), as a list (``mixins._resolve_configured_fields``, the
        one resolution the serializer, OPTIONS, schema and commands share)."""
        from .mixins import _resolve_configured_fields

        return _resolve_configured_fields(self.model, view_type)

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
        if getattr(self, "swagger_fake_view", False):
            # drf_yasg reads only the model while building the schema (once
            # per operation): no rows, no scope to build.
            return self.model._default_manager.none()

        # Unscoped base — see invariant in the docstring. This MUST be
        # passed through the tenant + predicate filter steps further
        # down before being returned to a request.
        unscoped = self._get_base_queryset()

        queryset = unscoped.order_by(*self._default_ordering(unscoped))

        # A delete renders nothing: it needs no JOINs or visibility flags.
        renders_instances = getattr(self, "action", None) != "destroy"

        # select_related the single-valued relation chains nested fields
        # traverse (an M2M or reverse FK can't be select_related: 500).
        nested = self._nested_relation_paths() if renders_instances else ()
        if nested:
            queryset = queryset.select_related(*nested)

        # Route row-level access through the single authorization chokepoint.
        # `scope()` applies the MANDATORY tenant boundary and the DISCRETIONARY
        # within-tenant predicates as separate filters — tenant outside the
        # algebra, never OR-composable away by an Either().
        request = getattr(self, "request", None)
        queryset = self.authorize(request).scope(queryset)
        if not renders_instances:
            self._nested_visibility = {}
            return queryset
        return self._annotate_nested_visibility(queryset, request)

    def _configured_nested_paths(self):
        """Every ``__`` field path in the model's config (list and detail)."""
        fields = self._configured_view_fields("list") + self._configured_view_fields(
            "detail"
        )
        return [f for f in dict.fromkeys(fields) if isinstance(f, str) and "__" in f]

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
        value as NULL where it can't. One
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
            q = build_traversal_scope_q(self.model, path, request, correlated=True)
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
        from .validation import filter_readable_fields, is_field_path_sensitive

        # Drop entries that can't be searched — a typo, a path ending on a
        # relation, a `$` (regex) prefix. They would raise FieldError at
        # SQL-build time → uncaught 500. Operator-mistake triggered, but
        # failing closed (silent drop) is safer than 5xx. A DRF prefix (`^`,
        # `=`, `@`) is kept on the entry; the permission checks use the path.
        prefixes = {}
        for entry in get_searchable_fields(self.model):
            if _is_resolvable_search_path(self.model, entry):
                prefix, path = _split_search_prefix(entry)
                prefixes.setdefault(path, prefix)

        # The sensitive deny-list applies even with permissions bypassed.
        paths = [p for p in prefixes if not is_field_path_sensitive(p)]
        if self.turbodrf_scope == PLATFORM:
            # Only configured paths (the router refuses others at startup).
            configured = self._configured_paths()
            paths = [p for p in paths if p in configured]
        request = getattr(self, "request", None)
        if paths and not permissions_bypassed() and request is not None:
            paths = filter_readable_fields(
                self.model, paths, getattr(request, "user", None)
            )
        return [prefixes[path] + path for path in paths]

    def _default_ordering(self, queryset):
        """The base queryset's order (its ``order_by``, else ``Meta.ordering``)
        if the caller may order by it, else the pk: pagination needs a stable
        order, and the order of the rows mustn't follow a column the caller
        can't read (see ``readable_ordering``)."""
        from .filter_backends import readable_ordering

        query = queryset.query
        ordering = query.order_by or (
            self.model._meta.ordering if query.default_ordering else ()
        )
        if not ordering:
            return ["pk"]
        return readable_ordering(self.model, ordering, set(self.ordering_fields))

    @property
    def ordering_fields(self):
        """Fields available for ?ordering=. Restricted to fields the caller
        can read to prevent leaking hidden values via row order (an
        attacker without read perm on `salary` could otherwise sort by it
        and binary-search the value).

        Only concrete, single-valued columns are orderable. Ordering by an
        M2M or reverse relation JOINs the related rows unscoped: it repeats
        rows (and the paginator then drops others) and leaks the order and
        link count of rows the caller can't see; a @property isn't a column.
        An FK orders by its stored column (TurboDRFOrderingFilter).

        With permissions bypassed: every configured field. Sensitive fields
        are never orderable.
        """
        from django.core.exceptions import FieldDoesNotExist

        from .validation import _get_sensitive_fields

        sensitive_fields = _get_sensitive_fields()
        if permissions_bypassed():
            readable = self._configured_base_fields()
        else:
            readable = self._get_filterable_fields() or set()
        orderable = []
        for name in sorted(readable):
            if name in sensitive_fields:
                continue
            try:
                field = self.model._meta.get_field(name)
            except FieldDoesNotExist:
                continue
            if field.concrete and not field.many_to_many:
                orderable.append(name)
        return orderable

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
            GET /api/articles/?categories=3

        Note:
            JSONField and BinaryField are excluded from automatic filtering
            as they require special handling that django-filter doesn't
            support out of the box.

        Computed once per request: django-filter, the OR filter backend and
        OPTIONS all read it.
        """
        request = getattr(self, "request", None)
        cached = self.__dict__.get("_filterset_fields")
        if cached is not None and request is not None and cached[0] is request:
            return cached[1]

        from .validation import _get_sensitive_fields

        sensitive_fields = _get_sensitive_fields()

        # Configured fields the caller can read (every configured field when
        # permissions are bypassed).
        readable_fields = self._get_filterable_fields()
        if readable_fields is None:
            readable_fields = self._configured_base_fields()

        filterset_fields = {}
        for field in self.model._meta.fields:
            if field.name in sensitive_fields or field.name not in readable_fields:
                continue
            lookups = _filter_lookups(field)
            if lookups:
                filterset_fields[field.name] = lookups

        # An M2M filters by a target pk (`?categories=3`, matched against the
        # targets the caller can see). Its `in` / `isnull` lookups aren't
        # offered: django-filter's `in` never matched, and `isnull` JOINed the
        # link table unscoped, repeating rows and counting hidden targets.
        for field in self.model._meta.many_to_many:
            if field.name in sensitive_fields or field.name not in readable_fields:
                continue
            filterset_fields[field.name] = ["exact"]

        if request is not None:
            self._filterset_fields = (request, filterset_fields)
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

        from .backends import build_permission_snapshot
        from .validation import fk_value_readable

        snapshot = self._caller_snapshot(request)
        readable = set(snapshot.readable_fields) if snapshot else set()
        user = getattr(request, "user", None)
        return {
            name
            for name in readable & self._configured_base_fields()
            if fk_value_readable(
                self.model,
                name,
                lambda m: (
                    snapshot if m is self.model else build_permission_snapshot(user, m)
                ),
            )
        }

    def _caller_snapshot(self, request):
        """The caller's permission snapshot (anonymous when the request has no
        authenticated user attached)."""
        from .backends import attach_snapshot_to_request, build_permission_snapshot

        if getattr(request, "user", None) is None:
            return build_permission_snapshot(None, self.model)
        return attach_snapshot_to_request(request, self.model)

    def _configured_base_fields(self):
        """Base names of every configured field (list and detail), plus the
        pk."""
        paths = self._configured_paths()
        return {self.model._meta.pk.name} | {f.split("__")[0] for f in paths}

    def _configured_paths(self):
        """Every configured field path (list and detail)."""
        return set(self._configured_view_fields("list")) | set(
            self._configured_view_fields("detail")
        )

    @property
    def filterset_fields(self):
        """Property wrapper for filterset_fields to work with
        DjangoFilterBackend."""
        return self.get_filterset_fields()

    def create(self, request, *args, **kwargs):
        # (Comments, not a docstring: the schema shows a view method's
        # docstring as the operation's description.)
        # We pre-fill tenant/owner FKs into request.data BEFORE serializer
        # validation, because those FKs are typically non-null on the model
        # and DRF's required-field check would otherwise reject requests that
        # omit them. The serializer then runs its own validate_write and
        # auto_fill on validated_data — the two stages have different roles:
        #   - view-level pre-fill: ensure required FKs exist for serializer
        #     validation (fills only if missing)
        #   - serializer-level auto_fill: always overwrite tenant FK after
        #     permission-stripping, so a stripped wrong-tenant value gets
        #     replaced with the correct one
        # Both are needed for security AND ergonomics. See predicates.py.
        data = self._prefill_required_fields(request)
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(
            serializer.data, status=status.HTTP_201_CREATED, headers=headers
        )

    def update(self, request, *args, **kwargs):
        # A full update (PUT) that leaves out the tenant FK gets it
        # pre-filled, as on create: it's required, and the row is in the
        # caller's tenant anyway. The owner isn't: an update leaves it as is.
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        data = request.data
        if not partial:
            data = self._prefill_required_fields(request, owner=False)
        serializer = self.get_serializer(instance, data=data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        if getattr(instance, "_prefetched_objects_cache", None):
            # As DRF's UpdateModelMixin: drop a stale prefetch cache.
            instance._prefetched_objects_cache = {}
        return Response(serializer.data)

    def _prefill_required_fields(self, request, owner=True):
        """Inject tenant/owner FKs into request.data so the serializer's
        required-field check passes. Only fills missing values — explicit
        user values pass through and are validated by the serializer.
        ``owner=False`` fills the tenant only.
        """
        from .predicates import Owner, get_user_tenant

        if self.turbodrf_scope == PLATFORM:
            # Nothing is filled in from a staff member: a platform create
            # names the row's tenant itself (serializers._apply_platform_writes).
            return request.data
        if not request.user.is_authenticated:
            return request.data

        # Bulk-array bodies and empty Content-Type produce request.data
        # shapes that aren't dicts (lists, strings). Pass them through
        # unchanged — DRF will reject downstream with a clean 400 instead
        # of crashing here on .copy() / dict() calls.
        if not isinstance(request.data, dict):
            return request.data
        if not self._ensure_row_scoping():
            return request.data  # the serializer's own checks refuse the write

        fill = {}
        # Tenant from the mandatory layer (setting, not predicate)
        if self._tenant_field and "__" not in self._tenant_field:
            if self._tenant_field not in request.data:
                tenant = get_user_tenant(request.user)
                if tenant is not None:
                    fill[self._tenant_field] = getattr(tenant, "pk", tenant)

        # Owner from within-tenant predicates
        for pred in self._predicates if owner else ():
            if isinstance(pred, Owner) and len(pred.fields) == 1:
                field = pred.fields[0]
                if "__" not in field and field not in request.data:
                    fill[field] = request.user.pk

        if not fill:
            return request.data
        # A shallow copy: QueryDict.copy() deep-copies, and an uploaded file
        # spooled to disk (over FILE_UPLOAD_MAX_MEMORY_SIZE) can't be copied.
        data = copy.copy(request.data)
        for field, value in fill.items():
            data[field] = value
        return data
