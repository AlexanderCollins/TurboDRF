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
    - Each operation once: the router's no-trailing-slash URL variants are
      not documented separately.

Role selection:
    By default the schema reflects all of the caller's roles. ``?role=<r>``
    narrows it to a single role the caller already holds. Without the query
    parameter, ``request.session['api_role']`` is read the same way; TurboDRF
    never sets that key, so it only applies if your app stores the caller's
    chosen role there. Either way the role can never widen the schema.
    Narrowing is supported in static permission mode; in database mode it is
    ignored.
"""

import hashlib
import logging
from collections import OrderedDict

from drf_yasg import openapi
from drf_yasg.generators import EndpointEnumerator, OpenAPISchemaGenerator
from drf_yasg.inspectors import NotHandled, PaginatorInspector, SwaggerAutoSchema
from drf_yasg.utils import no_body

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
    resolved exactly as TurboDRFViewSet.get_serializer_class does (see
    turbodrf.mixins._resolve_configured_fields)."""
    from .mixins import _resolve_configured_fields

    return _resolve_configured_fields(model, kind)


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


# Suffix TurboDRFRouter.get_urls gives the URL names of its no-trailing-slash
# duplicate routes.
_NO_SLASH_SUFFIX = "_no_slash"


class _NoSlashEndpointEnumerator(EndpointEnumerator):
    """Endpoint enumerator that skips the router's no-slash duplicates.

    TurboDRFRouter registers every route twice, ``items/`` and ``items``, so
    writes work without a trailing slash. Documenting both would list every
    path twice (``/items/``, ``/items``, ``/items/{id}/``, ``/items/{id}``)
    and generate every operation twice. The duplicates' URL names end in
    ``_no_slash``; only the URL name tells them apart, and drf-yasg passes it
    here (not to the generator's later hooks).
    """

    def should_include_endpoint(
        self, path, callback, app_name="", namespace="", url_name=None
    ):
        if isinstance(url_name, str) and url_name.endswith(_NO_SLASH_SUFFIX):
            return False
        return super().should_include_endpoint(
            path, callback, app_name, namespace, url_name
        )


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

    endpoint_enumerator_class = _NoSlashEndpointEnumerator

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
                ``?role=<r>`` (or, without it, ``session['api_role']``, a
                key your app may set; TurboDRF never does) narrows the schema
                to one role, honoured ONLY if the caller holds it.
            public: Ignored for TurboDRF endpoints: their permission check
                always runs for the caller.

        Returns:
            openapi.Swagger: The caller-scoped schema. An anonymous caller, and
            a call with no request at all, gets the anonymous schema: only
            reads of ``public_access`` models, with the fields an anonymous
            caller can read.
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


def _swagger2_schema(node):
    """Convert a DRF (OpenAPI 3) schema dict to a drf-yasg (Swagger 2) one.

    DRF paginators describe their envelope for DRF's OpenAPI 3 generator,
    where a nullable value is ``nullable: true``. Swagger 2 has no
    ``nullable``; drf-yasg's spelling is the ``x-nullable`` extension. Nested
    objects are converted too. drf-yasg schema objects (the page's item
    schema) are kept as they are.
    """
    if isinstance(node, openapi.SwaggerDict) or not isinstance(node, dict):
        return node
    converted = OrderedDict()
    for key, value in node.items():
        if key == "example":
            continue
        if key == "nullable":
            converted["x-nullable"] = value
        elif key == "properties" and isinstance(value, dict):
            converted[key] = OrderedDict(
                (name, _swagger2_schema(prop)) for name, prop in value.items()
            )
        elif key in ("items", "additionalProperties"):
            converted[key] = _swagger2_schema(value)
        else:
            converted[key] = value
    return converted


class TurboDRFPaginatorInspector(PaginatorInspector):
    """Document TurboDRFPagination's real response envelope.

    The envelope is defined once, by the paginator's own
    ``get_paginated_response_schema`` (which DRF's OpenAPI 3 generator also
    uses); this inspector converts it to Swagger 2. drf-yasg's default
    inspector only converts the top level, so the nested
    ``pagination.next`` / ``previous`` kept OpenAPI 3's ``nullable``, which
    isn't valid Swagger 2.
    """

    def get_paginated_response(self, paginator, response_schema):
        from .views import TurboDRFPagination

        if not isinstance(paginator, TurboDRFPagination):
            return NotHandled
        envelope = _swagger2_schema(
            paginator.get_paginated_response_schema(response_schema)
        )
        return openapi.Schema(
            type=envelope["type"],
            required=envelope.get("required"),
            properties=envelope.get("properties"),
        )


class TurboDRFSwaggerAutoSchema(SwaggerAutoSchema):
    """
    Custom SwaggerAutoSchema for TurboDRF ViewSets.

    - Request and response serializers are built for the caller's own
      permissions (see module docstring), never from the unfiltered model
      configuration.
    - Custom actions don't show the model serializer as their request body;
      they document the ``serializer_class`` their ``@action`` declares, if
      any.
    - An explicit ``@swagger_auto_schema(request_body=...)`` is honoured on
      every operation.
    - List responses use TurboDRF's pagination envelope.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.paginator_inspectors = [TurboDRFPaginatorInspector] + [
            inspector
            for inspector in self.paginator_inspectors
            if inspector is not TurboDRFPaginatorInspector
        ]

    def _is_custom_action(self):
        if not hasattr(self.view, "action"):
            return False
        return self.view.action not in _WRITE_ACTIONS + _STANDARD_READ_ACTIONS

    def _declared_action_serializer(self):
        """The serializer_class a custom ``@action(serializer_class=...)``
        declares (the router passes it to the view instance), or None."""
        return self.view.__dict__.get("serializer_class")

    def get_request_body_parameters(self, consumes):
        """
        Get request body parameters for the current operation.

        A custom action (``@action``) documents a request body only when one
        is declared: ``@swagger_auto_schema(request_body=...)`` or the
        action's own ``serializer_class``. Otherwise drf-yasg would document
        the model serializer, which the action doesn't take.

        Standard CRUD operations use the parent implementation, which calls
        get_request_serializer below.
        """
        if (
            self._is_custom_action()
            and self.overrides.get("request_body") is None
            and self._declared_action_serializer() is None
        ):
            return []
        return super().get_request_body_parameters(consumes)

    def get_request_serializer(self):
        """
        Get the request serializer for the current operation.

        - An explicit ``@swagger_auto_schema(request_body=...)`` wins
          (``drf_yasg.utils.no_body`` means no body).
        - Write operations (create, update, partial_update) get a serializer
          with exactly the fields the caller may write.
        - A custom action gets the ``serializer_class`` its ``@action``
          declares, on methods that take a body, else no body.
        """
        override = self._get_request_body_override()
        if override is not None:
            return None if override is no_body else override

        if not hasattr(self.view, "action"):
            return super().get_request_serializer()

        action = self.view.action
        if action in _WRITE_ACTIONS:
            return self._get_write_operation_serializer()
        if action in _STANDARD_READ_ACTIONS:
            return super().get_request_serializer()

        declared = self._declared_action_serializer()
        if declared is not None and self.method in self.implicit_body_methods:
            return declared(context=_serializer_context(self.view))
        return None

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
            declared = self._declared_action_serializer()
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

        # Writes use the detail fields, as the API does ("__all__" includes
        # forward M2M fields).
        fields = []
        for name in _configured_fields(model, "detail"):
            # Strip nested fields (`__` paths) — DRF ModelSerializer can't
            # use them in Meta.fields, and only the base FK is writable.
            base = name.split("__")[0]
            if base not in fields:
                fields.append(base)

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
