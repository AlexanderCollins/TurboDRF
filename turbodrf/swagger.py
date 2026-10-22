"""
TurboDRF Swagger/OpenAPI Schema Generation

This module provides caller-scoped OpenAPI schema generation for TurboDRF
APIs. It extends drf-yasg so the schema a caller receives describes only the
part of the API that caller can actually use.

What a caller sees:
    - Only TurboDRF endpoints. An operation is documented when the endpoint's
      own permission classes admit the caller (the same check the API runs)
      AND the caller's permission snapshot grants the action the HTTP method
      maps to (GET -> read, POST -> create, PUT/PATCH -> update,
      DELETE -> delete).
    - Request and response bodies built for the caller's own permissions
      (``TurboDRFSerializerFactory`` with the caller's snapshot): fields the
      caller can't read are absent, request bodies list only fields the
      caller can write, and ``TURBODRF_SENSITIVE_FIELDS`` never appear.
    - ``definitions`` pruned to the ones reachable from the remaining paths,
      so serializers of models the caller can't use are not published.
    - List responses documented with TurboDRF's real pagination envelope
      (``{"pagination": {...}, "data": [...]}``).

Role selection:
    By default the schema reflects all of the caller's roles. ``?role=<r>``
    (or ``session['api_role']``) narrows it to a single role the caller
    already holds. It can never widen the schema. Narrowing is supported in
    static permission mode; in database mode the parameter is ignored.
"""

import hashlib
import logging
from collections import OrderedDict

from drf_yasg import openapi
from drf_yasg.generators import OpenAPISchemaGenerator
from drf_yasg.inspectors import NotHandled, PaginatorInspector, SwaggerAutoSchema

logger = logging.getLogger(__name__)

# HTTP method -> TurboDRF model action (mirrors TurboDRFPermission).
_METHOD_ACTIONS = {
    "get": "read",
    "head": "read",
    "post": "create",
    "put": "update",
    "patch": "update",
    "delete": "delete",
}

_WRITE_ACTIONS = ("create", "update", "partial_update")
_STANDARD_READ_ACTIONS = ("list", "retrieve")

_DEFINITION_REF_PREFIX = "#/" + openapi.SCHEMA_DEFINITIONS + "/"

# Attribute set on schema-generation view instances when the caller narrowed
# the schema with ?role=. Read by TurboDRFSwaggerAutoSchema.
_SCHEMA_USER_ATTR = "_turbodrf_schema_user"
_UNSET = object()


class _RoleScopedUser:
    """Permission-only identity carrying a subset of the caller's roles.

    Used to build the schema for ``?role=<r>``. It holds ONLY a role list:
    no primary key (so permission snapshots for it are never cached) and it
    is only ever handed to the permission snapshot builder and the serializer
    factory's field check — never to the ORM. The real request user still
    drives the endpoint permission check and FK queryset scoping.
    """

    is_authenticated = True
    is_anonymous = False
    pk = None

    def __init__(self, roles):
        self.roles = list(roles)


def _turbodrf_model(view):
    """Return the model behind a TurboDRF viewset instance, else None."""
    from .views import TurboDRFViewSet

    if not isinstance(view, TurboDRFViewSet):
        return None
    model = getattr(view, "model", None)
    if model is None or not callable(getattr(model, "turbodrf", None)):
        return None
    return model


def _schema_user(view, request=None):
    """The identity whose permissions the schema for ``view`` describes."""
    user = getattr(view, _SCHEMA_USER_ATTR, _UNSET)
    if user is not _UNSET:
        return user
    req = getattr(view, "request", None)
    if req is None:
        req = request
    if req is None:
        return None
    return getattr(req, "user", None)


def _snapshot_for(user, model):
    from .backends import build_permission_snapshot

    return build_permission_snapshot(
        user, model, use_cache=not isinstance(user, _RoleScopedUser)
    )


def _request_snapshot_key(model):
    # Same key backends.attach_snapshot_to_request uses for its per-request
    # snapshot cache.
    return f"{model._meta.app_label}.{model._meta.model_name}"


def _configured_fields(model, kind):
    """The model's configured fields for ``kind`` ('list' or 'detail'),
    resolved exactly as TurboDRFViewSet.get_serializer_class does."""
    fields = model.turbodrf().get("fields", "__all__")
    if isinstance(fields, dict):
        fields = fields.get(kind, "__all__")
    return fields


def _serializer_context(view):
    return {"request": getattr(view, "request", None), "view": view, "format": None}


def _iter_definition_refs(node):
    """Yield every definition name referenced by a ``$ref`` under ``node``."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            ref = current.get("$ref")
            if isinstance(ref, str) and ref.startswith(_DEFINITION_REF_PREFIX):
                yield ref[len(_DEFINITION_REF_PREFIX) :]
            stack.extend(current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend(current)


def _prune_unreachable_definitions(schema):
    """Drop every definition not reachable from the remaining paths.

    drf-yasg registers a definition for every serializer it inspects. Without
    this pass, definitions for operations the caller can't use (or that were
    dropped later) would still describe those serializers' fields.
    """
    definitions = schema.get(openapi.SCHEMA_DEFINITIONS)
    if not definitions:
        return
    reachable = set()
    pending = list(_iter_definition_refs(schema.get("paths") or {}))
    while pending:
        name = pending.pop()
        if name in reachable or name not in definitions:
            continue
        reachable.add(name)
        pending.extend(_iter_definition_refs(definitions[name]))
    for name in [n for n in definitions if n not in reachable]:
        del definitions[name]
    if not definitions:
        del schema[openapi.SCHEMA_DEFINITIONS]


def _strip_sensitive_properties(node):
    """Remove TURBODRF_SENSITIVE_FIELDS names from every object schema.

    The serializers are already built without them; this is a last line of
    defence so a sensitive field name can never be published in the schema.
    """
    from .validation import is_field_path_sensitive

    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            properties = current.get("properties")
            if isinstance(properties, dict):
                dropped = [
                    name
                    for name in properties
                    if isinstance(name, str) and is_field_path_sensitive(name)
                ]
                for name in dropped:
                    del properties[name]
                required = current.get("required")
                if dropped and isinstance(required, list):
                    current["required"] = [r for r in required if r not in dropped]
            stack.extend(current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend(current)


class RoleBasedSchemaGenerator(OpenAPISchemaGenerator):
    """
    OpenAPI schema generator that scopes the schema to the caller.

    Only TurboDRF endpoints are documented, and only the operations the
    caller may perform (see the module docstring). Bodies are built for the
    caller's own field permissions by TurboDRFSwaggerAutoSchema, and
    ``definitions`` are pruned to those the remaining paths reference.

    Attributes:
        current_role (str | None): The single role the schema was narrowed to
            via ``?role=`` / ``session['api_role']``, or None when the schema
            reflects all of the caller's roles.
    """

    def __init__(self, info, version="", url=None, patterns=None, urlconf=None):
        super().__init__(info, version, url, patterns, urlconf)
        self.current_role = None
        self._scoped_user = None
        self._snapshots = {}

    def get_schema(self, request=None, public=False):
        """
        Generate the OpenAPI schema for the caller of ``request``.

        Args:
            request: The schema request. Its user decides what is documented.
                ``?role=<r>`` (or ``session['api_role']``) narrows the schema
                to one role, honoured ONLY if the caller holds it.
            public: Ignored for TurboDRF endpoints: their permission check
                always runs for the caller.

        Returns:
            openapi.Swagger: The caller-scoped schema. With no request it has no
            paths and no definitions; an anonymous caller sees only reads of
            ``public_access`` models.
        """
        from .backends import get_permission_mode, get_user_roles

        self.current_role = None
        self._scoped_user = None
        self._snapshots = {}

        if request is not None:
            requested = self._requested_role(request)
            if requested:
                user = getattr(request, "user", None)
                held = set(get_user_roles(user) or [])
                # A client-supplied role is NEVER trusted for privilege: it is
                # adopted only when the caller already holds it, so it can
                # only narrow the schema.
                if requested in held:
                    if get_permission_mode() == "database":
                        logger.info(
                            "Ignoring ?role=%s for the schema: role narrowing "
                            "is not supported in database permission mode.",
                            requested,
                        )
                    else:
                        self.current_role = requested
                        if held != {requested}:
                            self._scoped_user = _RoleScopedUser([requested])

        schema = super().get_schema(request, public)
        _prune_unreachable_definitions(schema)
        _strip_sensitive_properties(schema)
        return schema

    @staticmethod
    def _requested_role(request):
        """The role named by ``?role=`` or ``session['api_role']``, if any.

        Works without SessionMiddleware (no ``request.session``).
        """
        params = getattr(request, "GET", None)
        role = params.get("role") if params is not None else None
        if not role:
            session = getattr(request, "session", None)
            if session is not None:
                try:
                    role = session.get("api_role")
                except Exception:
                    role = None
        if isinstance(role, str) and role:
            return role
        return None

    def create_view(self, callback, method, request=None):
        view = super().create_view(callback, method, request)
        if self._scoped_user is not None:
            setattr(view, _SCHEMA_USER_ATTR, self._scoped_user)
            model = _turbodrf_model(view)
            view_request = getattr(view, "request", None)
            if model is not None and view_request is not None:
                # Seed the view's per-request snapshot cache with the narrowed
                # snapshot so the view's own filter/ordering parameter lists
                # match the selected role too.
                view_request._turbodrf_snapshots = {
                    _request_snapshot_key(model): self._snapshot(view, model)
                }
        return view

    def _snapshot(self, view, model):
        user = _schema_user(view)
        key = (id(user), _request_snapshot_key(model))
        if key not in self._snapshots:
            # Keep the user referenced so its id can't be reused meanwhile.
            self._snapshots[key] = (user, _snapshot_for(user, model))
        return self._snapshots[key][1]

    def should_include_endpoint(self, path, method, view, public):
        """Document an operation only if the caller may perform it.

        ``public`` never short-circuits this: the endpoint's permission
        classes (what the API itself enforces) must admit the caller, and
        the caller's snapshot must grant the method's action.
        """
        model = _turbodrf_model(view)
        if model is None:
            # Only TurboDRF endpoints are documented by this generator.
            return False

        if not self._gen.has_view_permissions(path, method, view):
            return False

        from .permissions import permissions_bypassed

        if permissions_bypassed():
            # TURBODRF_DISABLE_PERMISSIONS / TURBODRF_USE_DEFAULT_PERMISSIONS:
            # the view's own permission classes above are the API's gate.
            return True

        action = _METHOD_ACTIONS.get(method.lower())
        if action is None:
            return False
        return self._snapshot(view, model).can_perform_action(action)

    def get_endpoints(self, request=None):
        """
        Get API endpoints, filtering out duplicate no-slash variants.

        This override filters out the duplicate URL patterns created for
        trailing slash handling, preventing duplicate entries in the
        Swagger documentation.
        """
        endpoints = super().get_endpoints(request)

        # drf-yasg's get_endpoints returns a list of tuples on older
        # releases and a dict {path: (callback, methods)} on newer ones.
        # Both shapes are handled to avoid a 500 against newer drf-yasg.
        if isinstance(endpoints, dict):
            return self._filter_endpoint_dict(endpoints)

        return self._filter_endpoint_tuples(endpoints)

    def _filter_endpoint_dict(self, endpoints_dict):
        """drf-yasg dict form: {path: (callback, methods)} or similar."""
        filtered = {}
        for path, value in endpoints_dict.items():
            callback = value[0] if isinstance(value, tuple) and value else value
            if self._is_no_slash_duplicate(callback):
                continue
            filtered[path] = value
        return filtered

    def _filter_endpoint_tuples(self, endpoints):
        """drf-yasg tuple form: list of (path, path_regex, method, callback)."""
        filtered_endpoints = []
        for entry in endpoints:
            # Defensively handle 4-tuple AND 5-tuple shapes
            if len(entry) < 4:
                # Unknown shape — pass through unfiltered
                filtered_endpoints.append(entry)
                continue
            if self._is_no_slash_duplicate(entry[3]):
                continue
            filtered_endpoints.append(entry)
        return filtered_endpoints

    def _is_no_slash_duplicate(self, callback):
        """True if this callback is a duplicate no-slash variant."""
        if not (hasattr(callback, "cls") and hasattr(callback.cls, "_basename")):
            return False
        return (
            hasattr(callback, "actions")
            and hasattr(callback, "name")
            and callback.name
            and callback.name.endswith("_no_slash")
        )


class TurboDRFPaginatorInspector(PaginatorInspector):
    """Document TurboDRFPagination's real response envelope.

    drf-yasg's default inspector would describe TurboDRFPagination as a stock
    PageNumberPagination (``count``/``next``/``previous``/``results``), which
    is not what the API returns.
    """

    def get_paginated_response(self, paginator, response_schema):
        from rest_framework.pagination import PageNumberPagination

        from .views import TurboDRFPagination

        if not isinstance(paginator, TurboDRFPagination):
            return NotHandled
        paginator_cls = type(paginator)
        if (
            paginator_cls.get_paginated_response_schema
            is not PageNumberPagination.get_paginated_response_schema
        ):
            # The paginator describes its own envelope; let drf-yasg use it.
            return NotHandled
        if (
            paginator_cls.get_paginated_response
            is not TurboDRFPagination.get_paginated_response
        ):
            # A subclass changed the envelope; we don't know its shape.
            return NotHandled

        def link():
            return openapi.Schema(
                type=openapi.TYPE_STRING, format=openapi.FORMAT_URI, x_nullable=True
            )

        pagination = openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=[
                "next",
                "previous",
                "current_page",
                "total_pages",
                "total_items",
            ],
            properties=OrderedDict(
                (
                    ("next", link()),
                    ("previous", link()),
                    ("current_page", openapi.Schema(type=openapi.TYPE_INTEGER)),
                    ("total_pages", openapi.Schema(type=openapi.TYPE_INTEGER)),
                    ("total_items", openapi.Schema(type=openapi.TYPE_INTEGER)),
                )
            ),
        )
        return openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=["pagination", "data"],
            properties=OrderedDict(
                (("pagination", pagination), ("data", response_schema))
            ),
        )


class TurboDRFSwaggerAutoSchema(SwaggerAutoSchema):
    """
    Custom SwaggerAutoSchema for TurboDRF ViewSets.

    - Request and response serializers are built for the caller's own
      permissions (see module docstring), never from the unfiltered model
      configuration.
    - Custom actions don't show the model serializer as their request body.
    - List responses use TurboDRF's pagination envelope.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.paginator_inspectors = [TurboDRFPaginatorInspector] + [
            inspector
            for inspector in self.paginator_inspectors
            if inspector is not TurboDRFPaginatorInspector
        ]

    def get_request_body_parameters(self, consumes):
        """
        Get request body parameters for the current operation.

        For custom actions (methods decorated with @action), this method
        returns an empty list to prevent drf-yasg from including all model
        fields in the request schema.

        For standard CRUD operations (create, update, etc.), it delegates
        to the parent implementation which correctly includes model fields.
        """
        # Check if this is a custom action
        if hasattr(self.view, "action"):
            action = self.view.action

            # If it's a custom action (not a standard one), don't include
            # model fields unless explicitly defined
            if action not in _WRITE_ACTIONS + _STANDARD_READ_ACTIONS:
                return []

        # For standard actions, use the default behavior
        return super().get_request_body_parameters(consumes)

    def get_request_serializer(self):
        """
        Get the request serializer for the current operation.

        For write operations (create, update, partial_update), returns a
        serializer with exactly the fields the caller may write.

        For custom actions, returns None to prevent automatic serializer
        field inclusion in the request schema.
        """
        if hasattr(self.view, "action"):
            action = self.view.action

            if action in _WRITE_ACTIONS:
                return self._get_write_operation_serializer()

            if action in _STANDARD_READ_ACTIONS:
                return super().get_request_serializer()

            # Custom actions - check for explicit serializer
            action_method = getattr(self.view, action, None)
            if action_method and hasattr(action_method, "kwargs"):
                serializer_class = action_method.kwargs.get("serializer_class")
                if serializer_class:
                    return serializer_class()

            # No serializer for request body
            return None

        # For standard actions, use the default behavior
        return super().get_request_serializer()

    def get_view_serializer(self):
        """Response serializer for TurboDRF views, built for the caller.

        drf-yasg's default calls ``view.get_serializer()``. For a role-narrowed
        schema that would use the caller's full roles, and for custom actions
        it returned the unfiltered model serializer, so TurboDRF views get a
        serializer built by TurboDRFSerializerFactory for the schema user.
        """
        model = _turbodrf_model(self.view)
        if model is None:
            return super().get_view_serializer()

        from .permissions import permissions_bypassed

        if permissions_bypassed():
            # No TurboDRF field permissions: the view's own serializer is
            # exactly what the API uses.
            return super().get_view_serializer()

        action = getattr(self.view, "action", None)
        if action not in _WRITE_ACTIONS + _STANDARD_READ_ACTIONS:
            declared = self.view.__dict__.get("serializer_class")
            if declared is not None:
                return declared(context=_serializer_context(self.view))

        from .serializers import TurboDRFSerializerFactory

        if action == "list":
            fields, view_type = _configured_fields(model, "list"), "list"
        elif action in _WRITE_ACTIONS:
            fields, view_type = _configured_fields(model, "detail"), "detail"
        else:
            fields, view_type = _configured_fields(model, "detail"), "retrieve"

        user = _schema_user(self.view, self.request)
        serializer_class = TurboDRFSerializerFactory.create_serializer(
            model,
            fields,
            user,
            view_type=view_type,
            snapshot=_snapshot_for(user, model),
        )
        return serializer_class(context=_serializer_context(self.view))

    def _get_write_operation_serializer(self):
        """
        Serializer for a write operation's request body.

        Built by TurboDRFSerializerFactory for the schema user (the caller,
        or the role they narrowed to), so it contains only fields the caller
        can read AND write. ``TURBODRF_SENSITIVE_FIELDS`` are always dropped.
        Nested ``a__b`` paths collapse to their writable base FK ``a``.

        Returns:
            Serializer instance whose fields are exactly the caller's
            writable fields (possibly none).
        """
        model = _turbodrf_model(self.view)
        if model is None:
            return super().get_request_serializer()

        from .permissions import permissions_bypassed
        from .serializers import TurboDRFSerializer, TurboDRFSerializerFactory
        from .validation import is_field_path_sensitive

        fields = _configured_fields(model, "detail")
        if fields == "__all__":
            fields = [f.name for f in model._meta.fields]
        if isinstance(fields, (list, tuple)):
            # Strip nested fields (`__` paths) — DRF ModelSerializer can't
            # use them in Meta.fields, and only the base FK is writable.
            seen = set()
            cleaned = []
            for f in fields:
                base = f.split("__")[0]
                if base not in seen:
                    seen.add(base)
                    cleaned.append(base)
            fields = cleaned

        if permissions_bypassed():
            # Parity with the API: no TurboDRF field permissions apply.
            base_class = type(
                f"{model.__name__}WriteBaseSerializer",
                (TurboDRFSerializer,),
                {
                    "Meta": type(
                        "Meta",
                        (),
                        {
                            "model": model,
                            "fields": [
                                f for f in fields if not is_field_path_sensitive(f)
                            ],
                        },
                    )
                },
            )
        else:
            user = _schema_user(self.view, self.request)
            base_class = TurboDRFSerializerFactory.create_serializer(
                model,
                fields,
                user,
                view_type="write",
                snapshot=_snapshot_for(user, model),
            )

        base_meta = base_class.Meta
        writable = sorted(
            set(base_meta.fields) - set(getattr(base_meta, "read_only_fields", ()))
        )
        digest = hashlib.sha256(",".join(writable).encode()).hexdigest()[:8]
        ref_name_value = (
            f"{model._meta.app_label}_{model._meta.model_name}_{digest}_write"
        )

        class WriteOperationSerializer(base_class):
            class Meta(base_meta):
                ref_name = ref_name_value

            def get_fields(self):
                # A request body lists only what the caller can write.
                return OrderedDict(
                    (name, field)
                    for name, field in super().get_fields().items()
                    if not field.read_only
                )

        return WriteOperationSerializer(context=_serializer_context(self.view))
