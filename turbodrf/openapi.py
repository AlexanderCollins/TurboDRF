"""
OpenAPI 3 schema for TurboDRF APIs, built on drf-spectacular (the
``openapi3`` extra: ``pip install "turbodrf[openapi3]"``).

The schema is scoped to the caller exactly as the Swagger 2 one is, with
turbodrf.swagger's helpers: only the TurboDRF operations the caller may
perform, request and response bodies built for the caller's own field
permissions, only the components those operations use, never a
``TURBODRF_SENSITIVE_FIELDS`` name, and ``?role=`` narrowing to a role the
caller holds. An anonymous caller gets the reads of ``public_access`` models.

drf-spectacular needs no configuration: TurboDRF's views are documented by
TurboDRF's AutoSchema whatever ``DEFAULT_SCHEMA_CLASS`` says, and
``drf_spectacular`` needn't be in INSTALLED_APPS. drf-spectacular's own
``SpectacularAPIView`` isn't scoped to the caller: serve this one.
"""

import weakref

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from rest_framework import permissions

from .scope import PLATFORM, TENANT, use_scope
from .swagger import (
    _WRITE_ACTIONS,
    _action_serializer,
    _CallerScope,
    _in_document,
    _prune_unreachable,
    _response_serializer,
    _strip_sensitive_properties,
    _turbodrf_model,
    _write_serializer,
)

_COMPONENT_REF_PREFIX = "#/components/schemas/"


def get_openapi3_view(
    scope="tenant", gate=None, views=None, title="API", version="v1", description=""
):
    """
    A view serving the caller-scoped OpenAPI 3 schema: JSON, or YAML for
    ``?format=yaml`` or an ``Accept: application/yaml`` (or
    ``application/vnd.oai.openapi``) request.

    Args:
        scope: ``"tenant"`` (default) documents the tenant scope's endpoints;
            ``"platform"`` the platform scope's, with platform roles.
        gate: Permission classes the view checks first (the router's).
            Required for the platform scope; without one the tenant view
            admits any caller, and the schema is scoped to them.
        views: Returns the view classes to document (a router's registry);
            every TurboDRF view of ``scope`` when None.
            ``TurboDRFRouter.get_openapi3_view()`` passes all three.
        title, version, description: The schema's ``info``.

    Returns:
        A view function, or None with ``TURBODRF_ENABLE_DOCS = False``.

    Raises:
        ImproperlyConfigured: drf-spectacular isn't installed, ``scope`` is
            unknown, or the platform scope has no gate.
    """
    if not getattr(settings, "TURBODRF_ENABLE_DOCS", True):
        return None
    try:
        from drf_spectacular import generators, openapi, renderers
        from drf_spectacular.views import SpectacularAPIView
    except ImportError as exc:
        raise ImproperlyConfigured(
            "The OpenAPI 3 schema needs drf-spectacular: "
            'pip install "turbodrf[openapi3]".'
        ) from exc

    from .documentation import _platform_schema_view, _schema_gate

    gate = _schema_gate("get_openapi3_view", scope, gate)
    enumerator = type(
        "TurboDRFEndpointEnumerator",
        (_Endpoints, generators.EndpointEnumerator),
        {
            "scope": scope,
            "view_classes": None if views is None else staticmethod(views),
        },
    )
    info = {"title": title, "version": version}
    if description:
        info["description"] = description
    generator = type(
        "TurboDRFOpenAPIGenerator",
        (_Generator, generators.SchemaGenerator),
        {
            "scope": scope,
            "endpoint_inspector_cls": enumerator,
            "auto_schema_class": type(
                "TurboDRFAutoSchema", (_AutoSchema, openapi.AutoSchema), {}
            ),
            "info": info,
        },
    )
    view = type(
        "TurboDRFOpenAPIView",
        (SpectacularAPIView,),
        {
            "generator_class": generator,
            "permission_classes": gate or [permissions.AllowAny],
            "serve_public": False,
            # Not itself an endpoint of either schema (it would move the
            # Swagger document's basePath), as drf-yasg's schema view isn't.
            "schema": None,
            # JSON unless YAML is asked for.
            "renderer_classes": [
                renderers.OpenApiJsonRenderer,
                renderers.OpenApiJsonRenderer2,
                renderers.OpenApiYamlRenderer,
                renderers.OpenApiYamlRenderer2,
            ],
        },
    )
    if scope == PLATFORM:
        view = _platform_schema_view(view)
    return view.as_view()


class _Endpoints:
    """drf-spectacular's EndpointEnumerator, narrowed to TurboDRF's views of
    one scope (and router), each route once."""

    scope = TENANT
    view_classes = None

    def should_include_endpoint(self, path, callback):
        from .views import TurboDRFViewSet

        view_class = getattr(callback, "cls", None)
        return (
            isinstance(view_class, type)
            and issubclass(view_class, TurboDRFViewSet)
            and _in_document(view_class, self)
            and super().should_include_endpoint(path, callback)
        )

    def get_api_endpoints(self, patterns=None, prefix=""):
        endpoints = super().get_api_endpoints(patterns, prefix)
        # TurboDRFRouter serves every route with and without its trailing
        # slash, with one callback: document the slashed one only.
        slashed = {(path, method, view) for path, _, method, view in endpoints}
        return [
            (path, regex, method, view)
            for path, regex, method, view in endpoints
            if (path + "/", method, view) not in slashed
        ]


class _Generator(_CallerScope):
    """drf-spectacular's SchemaGenerator, scoped to the caller."""

    auto_schema_class = None
    info = None

    def get_schema(self, request=None, public=False):
        # ``public`` is ignored (drf-spectacular's SERVE_PUBLIC would skip the
        # permission check): the caller's permissions always decide.
        with use_scope(self.scope):
            self._start(request)
            self._auto_schemas = []
            schema = super().get_schema(request=request, public=False)
            components = schema.get("components") or {}
            if components.get("schemas"):
                roots = {k: v for k, v in schema.items() if k != "components"}
                _prune_unreachable(components["schemas"], roots, _COMPONENT_REF_PREFIX)
                if not components["schemas"]:
                    del components["schemas"]
            _strip_sensitive_properties(schema)
        schema["info"] = dict(self.info)
        return schema

    def create_view(self, callback, method, request=None):
        view = super().create_view(callback, method, request)
        # TurboDRF's AutoSchema, whatever DEFAULT_SCHEMA_CLASS or an
        # @extend_schema says. The view holds it weakly, as drf-spectacular
        # does: the view's schema descriptor keys on the view.
        auto_schema = self.auto_schema_class()
        self._auto_schemas.append(auto_schema)
        view.schema = weakref.proxy(auto_schema)
        return view

    def coerce_path(self, path, method, view):
        # DRF names {pk} after view.queryset's model, which TurboDRF's
        # viewsets don't have: it would always be {id}.
        model = _turbodrf_model(view)
        if model is not None and self.coerce_path_pk:
            from rest_framework.schemas.generators import get_pk_name

            path = path.replace("{pk}", "{%s}" % get_pk_name(model))
        return super().coerce_path(path, method, view)

    def has_view_permissions(self, path, method, view):
        # drf-spectacular gives the view the caller's request just before.
        self._scope_view(view)
        return super().has_view_permissions(path, method, view) and self._may_perform(
            view, method
        )


class _AutoSchema:
    """drf-spectacular's AutoSchema with request and response bodies built
    for the caller (turbodrf.swagger's serializers)."""

    def get_request_serializer(self):
        if getattr(self.view, "action", None) in _WRITE_ACTIONS:
            return _write_serializer(self.view)
        return _action_serializer(self.view)

    def get_response_serializers(self):
        serializer = _response_serializer(self.view)
        if serializer is None:
            return super().get_response_serializers()
        return serializer

    def resolve_serializer(self, serializer, direction, bypass_extensions=False):
        from drf_spectacular.drainage import set_override

        turbodrf = _is_turbodrf_serializer(serializer)
        if turbodrf:
            # TurboDRF builds a serializer class per operation. In one schema
            # a ref_name names one field set (the caller's), so classes
            # sharing it aren't a name collision.
            set_override(type(serializer), "suppress_collision_warning", True)
        component = super().resolve_serializer(serializer, direction, bypass_extensions)
        if turbodrf and component.schema:
            # TurboDRFSerializer's docstring: implementation notes, not docs.
            component.schema.pop("description", None)
        return component

    def get_paginated_name(self, serializer_name):
        return f"{serializer_name}_page"


def _is_turbodrf_serializer(serializer):
    from .serializers import TurboDRFSerializer

    return isinstance(serializer, TurboDRFSerializer) and bool(
        getattr(serializer.Meta, "ref_name", None)
    )
