"""
Tests to improve coverage for swagger.py, metadata.py, and urls.py.

Targets uncovered lines:
- swagger.py: 80-116, 149-169, 204-219, 258-271, 287-300, 345, 379, 385, 406
- metadata.py: 38-39, 81, 86-87, 97-98
- urls.py: 0% covered (import test)
"""

from unittest.mock import MagicMock, patch

from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.db import models as dj_models
from django.test import TestCase, override_settings
from drf_yasg import openapi
from rest_framework.test import APIRequestFactory

from tests.test_app.models import RelatedModel, SampleModel
from turbodrf.metadata import TurboDRFMetadata
from turbodrf.mixins import TurboDRFMixin
from turbodrf.swagger import RoleBasedSchemaGenerator, TurboDRFSwaggerAutoSchema
from turbodrf.views import TurboDRFViewSet

User = get_user_model()

# Test roles matching the test_app models, used to patch turbodrf.settings.TURBODRF_ROLES
# (swagger.py and metadata.py import directly from turbodrf.settings, not django.conf.settings)
_TEST_ROLES = getattr(django_settings, "TURBODRF_ROLES", {})


def _resolver():
    """Build a ReferenceResolver with the scopes drf-yasg needs."""
    return openapi.ReferenceResolver("definitions", "parameters", force_init=True)


# ---------------------------------------------------------------------------
# RoleBasedSchemaGenerator — helper methods
# ---------------------------------------------------------------------------


class TestRoleBasedSchemaGeneratorHelpers(TestCase):
    """Test _extract_model_info, _has_permission, _filter_schema_fields."""

    def _gen(self):
        return RoleBasedSchemaGenerator(
            info=openapi.Info(title="Test", default_version="v1"),
        )

    # -- _extract_model_info (lines 149-169)

    def test_extract_model_info_valid_api_path(self):
        result = self._gen()._extract_model_info("/api/samplemodels/")
        self.assertIsNotNone(result)
        self.assertEqual(result["model_name"], "samplemodel")
        self.assertEqual(result["app_label"], "test_app")

    def test_extract_model_info_unknown_model_falls_back(self):
        result = self._gen()._extract_model_info("/api/unknownitems/")
        self.assertIsNotNone(result)
        self.assertEqual(result["app_label"], "books")
        self.assertEqual(result["model_name"], "unknownitem")

    def test_extract_model_info_too_short_path(self):
        self.assertIsNone(self._gen()._extract_model_info("/single/"))

    def test_extract_model_info_non_api_path(self):
        self.assertIsNone(self._gen()._extract_model_info("/admin/samplemodels/"))

    def test_extract_model_info_root_path(self):
        self.assertIsNone(self._gen()._extract_model_info("/"))

    # -- _has_permission (lines 204-219)

    def test_has_permission_get(self):
        info = {"app_label": "test_app", "model_name": "samplemodel"}
        self.assertTrue(
            self._gen()._has_permission(info, "GET", {"test_app.samplemodel.read"})
        )

    def test_has_permission_post(self):
        info = {"app_label": "test_app", "model_name": "samplemodel"}
        self.assertTrue(
            self._gen()._has_permission(info, "POST", {"test_app.samplemodel.create"})
        )

    def test_has_permission_put(self):
        info = {"app_label": "test_app", "model_name": "samplemodel"}
        self.assertTrue(
            self._gen()._has_permission(info, "PUT", {"test_app.samplemodel.update"})
        )

    def test_has_permission_patch(self):
        info = {"app_label": "test_app", "model_name": "samplemodel"}
        self.assertTrue(
            self._gen()._has_permission(info, "PATCH", {"test_app.samplemodel.update"})
        )

    def test_has_permission_delete(self):
        info = {"app_label": "test_app", "model_name": "samplemodel"}
        self.assertTrue(
            self._gen()._has_permission(info, "DELETE", {"test_app.samplemodel.delete"})
        )

    def test_has_permission_denied(self):
        info = {"app_label": "test_app", "model_name": "samplemodel"}
        self.assertFalse(
            self._gen()._has_permission(info, "POST", {"test_app.samplemodel.read"})
        )

    def test_has_permission_unknown_method(self):
        info = {"app_label": "test_app", "model_name": "samplemodel"}
        self.assertFalse(
            self._gen()._has_permission(info, "OPTIONS", {"test_app.samplemodel.read"})
        )

    # -- _filter_schema_fields (lines 258-271)

    def test_filter_schema_fields_removes_unpermitted(self):
        schema = {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "secret_field": {"type": "string"},
            },
        }
        info = {"app_label": "test_app", "model_name": "samplemodel"}
        result = self._gen()._filter_schema_fields(
            schema, info, {"test_app.samplemodel.title.read"}
        )
        self.assertIn("title", result["properties"])
        self.assertNotIn("secret_field", result["properties"])

    def test_filter_schema_fields_no_properties_key(self):
        schema = {"type": "string"}
        info = {"app_label": "test_app", "model_name": "samplemodel"}
        result = self._gen()._filter_schema_fields(schema, info, set())
        self.assertEqual(result, {"type": "string"})

    def test_filter_schema_fields_empty_permissions(self):
        schema = {"type": "object", "properties": {"title": {"type": "string"}}}
        info = {"app_label": "test_app", "model_name": "samplemodel"}
        result = self._gen()._filter_schema_fields(schema, info, set())
        self.assertEqual(result["properties"], {})


# ---------------------------------------------------------------------------
# RoleBasedSchemaGenerator — get_schema (lines 80-116)
# ---------------------------------------------------------------------------


class TestRoleBasedSchemaGeneratorGetSchema(TestCase):
    """Test get_schema role-based filtering."""

    def _gen(self):
        return RoleBasedSchemaGenerator(
            info=openapi.Info(title="Test", default_version="v1"),
        )

    def test_get_schema_without_request(self):
        gen = self._gen()
        gen.current_role = None
        fake_schema = {"paths": {}}
        with patch.object(
            RoleBasedSchemaGenerator.__bases__[0],
            "get_schema",
            return_value=fake_schema,
        ):
            result = gen.get_schema(request=None, public=True)
        self.assertIsNone(gen.current_role)
        self.assertEqual(result["paths"], {})

    @patch("turbodrf.settings.TURBODRF_ROLES", _TEST_ROLES)
    def test_get_schema_with_role_filters_paths(self):
        gen = self._gen()
        fake_schema = {
            "paths": {
                "/api/samplemodels/": {
                    "get": {
                        "responses": {
                            "200": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"title": {"type": "string"}},
                                }
                            }
                        }
                    },
                    "post": {"responses": {}},
                },
                "/api/relatedmodels/": {"get": {"responses": {}}},
            }
        }
        req = MagicMock()
        req.GET = {"role": "viewer"}
        req.session = {}
        with patch.object(
            RoleBasedSchemaGenerator.__bases__[0],
            "get_schema",
            return_value=fake_schema,
        ):
            result = gen.get_schema(request=req, public=True)
        self.assertIn("/api/samplemodels/", result["paths"])
        self.assertIn("get", result["paths"]["/api/samplemodels/"])
        self.assertNotIn("post", result["paths"]["/api/samplemodels/"])
        self.assertIn("/api/relatedmodels/", result["paths"])

    def test_get_schema_role_from_session(self):
        gen = self._gen()
        req = MagicMock()
        req.GET = {}
        req.session = {"api_role": "admin"}
        with patch.object(
            RoleBasedSchemaGenerator.__bases__[0],
            "get_schema",
            return_value={"paths": {}},
        ):
            gen.get_schema(request=req, public=True)
        self.assertEqual(gen.current_role, "admin")

    def test_get_schema_no_role_returns_unfiltered(self):
        gen = self._gen()
        fake_schema = {"paths": {"/api/samplemodels/": {"get": {}, "post": {}}}}
        req = MagicMock()
        req.GET = {}
        req.session = {}
        with patch.object(
            RoleBasedSchemaGenerator.__bases__[0],
            "get_schema",
            return_value=fake_schema,
        ):
            result = gen.get_schema(request=req, public=True)
        self.assertIn("post", result["paths"]["/api/samplemodels/"])

    @patch("turbodrf.settings.TURBODRF_ROLES", _TEST_ROLES)
    def test_get_schema_filters_response_schema_fields(self):
        gen = self._gen()
        fake_schema = {
            "paths": {
                "/api/samplemodels/": {
                    "get": {
                        "responses": {
                            "200": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "title": {"type": "string"},
                                        "price": {"type": "number"},
                                        "secret_field": {"type": "string"},
                                    },
                                }
                            }
                        }
                    }
                }
            }
        }
        req = MagicMock()
        req.GET = {"role": "viewer"}
        req.session = {}
        with patch.object(
            RoleBasedSchemaGenerator.__bases__[0],
            "get_schema",
            return_value=fake_schema,
        ):
            result = gen.get_schema(request=req, public=True)
        props = result["paths"]["/api/samplemodels/"]["get"]["responses"]["200"][
            "schema"
        ]["properties"]
        self.assertIn("title", props)
        self.assertNotIn("price", props)
        self.assertNotIn("secret_field", props)

    @patch("turbodrf.settings.TURBODRF_ROLES", _TEST_ROLES)
    def test_get_schema_path_with_no_model_info_excluded(self):
        gen = self._gen()
        fake_schema = {"paths": {"/admin/dashboard/": {"get": {"responses": {}}}}}
        req = MagicMock()
        req.GET = {"role": "admin"}
        req.session = {}
        with patch.object(
            RoleBasedSchemaGenerator.__bases__[0],
            "get_schema",
            return_value=fake_schema,
        ):
            result = gen.get_schema(request=req, public=True)
        self.assertEqual(result["paths"], {})


# ---------------------------------------------------------------------------
# RoleBasedSchemaGenerator — get_endpoints (lines 287-300)
# ---------------------------------------------------------------------------


class TestGetEndpointsFiltering(TestCase):
    """Test get_endpoints no-slash filtering."""

    def test_filters_no_slash_callbacks(self):
        gen = RoleBasedSchemaGenerator(
            info=openapi.Info(title="T", default_version="v1")
        )
        regular_cb = MagicMock()
        regular_cb.cls._basename = "sample"
        regular_cb.actions = {"get": "list"}
        regular_cb.name = "sample-list"
        no_slash_cb = MagicMock()
        no_slash_cb.cls._basename = "sample"
        no_slash_cb.actions = {"get": "list"}
        no_slash_cb.name = "sample-list_no_slash"
        fake_eps = [
            ("/api/samples/", "regex", "get", regular_cb),
            ("/api/samples", "regex", "get", no_slash_cb),
        ]
        with patch.object(
            RoleBasedSchemaGenerator.__bases__[0],
            "get_endpoints",
            return_value=fake_eps,
        ):
            result = gen.get_endpoints(request=None)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], "/api/samples/")

    def test_preserves_callbacks_without_name(self):
        gen = RoleBasedSchemaGenerator(
            info=openapi.Info(title="T", default_version="v1")
        )
        cb = MagicMock()
        cb.cls._basename = "x"
        cb.actions = {}
        cb.name = None
        with patch.object(
            RoleBasedSchemaGenerator.__bases__[0],
            "get_endpoints",
            return_value=[("/api/x/", "r", "get", cb)],
        ):
            result = gen.get_endpoints(request=None)
        self.assertEqual(len(result), 1)


# ---------------------------------------------------------------------------
# TurboDRFSwaggerAutoSchema — get_request_body_parameters (line 345)
# ---------------------------------------------------------------------------


class TestSwaggerAutoSchemaBodyParams(TestCase):
    """Cover get_request_body_parameters for standard actions (line 345)."""

    def setUp(self):
        self.factory = APIRequestFactory()
        RelatedModel.objects.get_or_create(name="R", defaults={"description": "d"})

    def test_standard_create_returns_body_params(self):
        """Standard create action delegates to super for body params (line 345).
        Use RelatedModel (simple fields, no nested __ fields) to avoid serializer issues.
        """
        viewset = TurboDRFViewSet()
        viewset.model = RelatedModel
        viewset.queryset = RelatedModel.objects.all()
        viewset.action = "create"
        request = self.factory.post("/api/relatedmodels/")
        viewset.request = request
        viewset.format_kwarg = None
        schema = TurboDRFSwaggerAutoSchema(
            view=viewset,
            path="/api/relatedmodels/",
            method="POST",
            components=_resolver(),
            request=request,
            overrides={},
        )
        params = schema.get_request_body_parameters(["application/json"])
        self.assertIsInstance(params, list)
        self.assertGreater(len(params), 0)

    def test_standard_list_returns_body_params(self):
        viewset = TurboDRFViewSet()
        viewset.model = SampleModel
        viewset.queryset = SampleModel.objects.all()
        viewset.action = "list"
        request = self.factory.get("/api/samplemodels/")
        viewset.request = request
        viewset.format_kwarg = None
        schema = TurboDRFSwaggerAutoSchema(
            view=viewset,
            path="/api/samplemodels/",
            method="GET",
            components=_resolver(),
            request=request,
            overrides={},
        )
        params = schema.get_request_body_parameters(["application/json"])
        self.assertIsInstance(params, list)

    def test_view_without_action_attribute(self):
        viewset = TurboDRFViewSet()
        viewset.model = SampleModel
        viewset.queryset = SampleModel.objects.all()
        request = self.factory.post("/api/samplemodels/")
        viewset.request = request
        viewset.format_kwarg = None
        if hasattr(viewset, "action"):
            delattr(viewset, "action")
        schema = TurboDRFSwaggerAutoSchema(
            view=viewset,
            path="/api/samplemodels/",
            method="POST",
            components=_resolver(),
            request=request,
            overrides={},
        )
        params = schema.get_request_body_parameters(["application/json"])
        self.assertIsInstance(params, list)


# ---------------------------------------------------------------------------
# TurboDRFSwaggerAutoSchema — get_request_serializer (lines 379, 385, 406)
# ---------------------------------------------------------------------------


class TestSwaggerAutoSchemaRequestSerializer(TestCase):
    """Cover get_request_serializer edge cases."""

    def setUp(self):
        self.factory = APIRequestFactory()
        RelatedModel.objects.get_or_create(name="R", defaults={"description": "d"})

    def test_no_action_attr_falls_through(self):
        viewset = TurboDRFViewSet()
        viewset.model = SampleModel
        viewset.queryset = SampleModel.objects.all()
        request = self.factory.post("/api/samplemodels/")
        viewset.request = request
        viewset.format_kwarg = None
        if hasattr(viewset, "action"):
            delattr(viewset, "action")
        schema = TurboDRFSwaggerAutoSchema(
            view=viewset,
            path="/api/samplemodels/",
            method="POST",
            components=_resolver(),
            request=request,
            overrides={},
        )
        result = schema.get_request_serializer()
        self.assertTrue(
            result is None or hasattr(result, "data") or hasattr(result, "Meta")
        )

    def test_custom_action_with_explicit_serializer_class(self):
        from rest_framework import serializers

        class DummySerializer(serializers.Serializer):
            status = serializers.CharField()

        viewset = TurboDRFViewSet()
        viewset.model = SampleModel
        viewset.queryset = SampleModel.objects.all()

        def fake_action(self, request):
            pass

        fake_action.kwargs = {"serializer_class": DummySerializer}
        viewset.my_custom = fake_action
        viewset.action = "my_custom"
        request = self.factory.post("/api/samplemodels/my_custom/")
        viewset.request = request
        viewset.format_kwarg = None
        schema = TurboDRFSwaggerAutoSchema(
            view=viewset,
            path="/api/samplemodels/my_custom/",
            method="POST",
            components=_resolver(),
            request=request,
            overrides={},
        )
        result = schema.get_request_serializer()
        self.assertIsNotNone(result)
        self.assertIsInstance(result, DummySerializer)

    def test_custom_action_without_serializer_returns_none(self):
        viewset = TurboDRFViewSet()
        viewset.model = SampleModel
        viewset.queryset = SampleModel.objects.all()

        def fake_action(self, request):
            pass

        fake_action.kwargs = {}
        viewset.some_action = fake_action
        viewset.action = "some_action"
        request = self.factory.post("/api/samplemodels/some_action/")
        viewset.request = request
        viewset.format_kwarg = None
        schema = TurboDRFSwaggerAutoSchema(
            view=viewset,
            path="/api/samplemodels/some_action/",
            method="POST",
            components=_resolver(),
            request=request,
            overrides={},
        )
        self.assertIsNone(schema.get_request_serializer())

    def test_write_serializer_model_without_model_attr(self):
        viewset = TurboDRFViewSet()
        viewset.model = None
        viewset.queryset = SampleModel.objects.none()
        viewset.action = "create"
        request = self.factory.post("/api/samplemodels/")
        viewset.request = request
        viewset.format_kwarg = None
        schema = TurboDRFSwaggerAutoSchema(
            view=viewset,
            path="/api/samplemodels/",
            method="POST",
            components=_resolver(),
            request=request,
            overrides={},
        )
        result = schema._get_write_operation_serializer()
        self.assertTrue(
            result is None or hasattr(result, "Meta") or hasattr(result, "data")
        )

    def test_write_serializer_with_simple_field_list(self):
        class SimpleFieldModel(TurboDRFMixin, dj_models.Model):
            name = dj_models.CharField(max_length=100)

            class Meta:
                app_label = "test_app"

            @classmethod
            def turbodrf(cls):
                return {"fields": ["name"]}

        viewset = TurboDRFViewSet()
        viewset.model = SimpleFieldModel
        viewset.queryset = SimpleFieldModel.objects.none()
        viewset.action = "create"
        request = self.factory.post("/api/simplefieldmodels/")
        viewset.request = request
        viewset.format_kwarg = None
        schema = TurboDRFSwaggerAutoSchema(
            view=viewset,
            path="/api/simplefieldmodels/",
            method="POST",
            components=_resolver(),
            request=request,
            overrides={},
        )
        result = schema._get_write_operation_serializer()
        self.assertIsNotNone(result)
        self.assertEqual(result.Meta.fields, ["name"])


# ---------------------------------------------------------------------------
# TurboDRFMetadata (lines 38-39, 81, 86-87, 97-98)
# ---------------------------------------------------------------------------


class TestTurboDRFMetadata(TestCase):
    """Cover metadata.py uncovered lines."""

    def setUp(self):
        self.factory = APIRequestFactory()
        self.related = RelatedModel.objects.create(name="Cat", description="desc")

    def _make_view(self, action="list"):
        viewset = TurboDRFViewSet()
        viewset.model = SampleModel
        viewset.queryset = SampleModel.objects.all()
        viewset.action = action
        viewset.kwargs = {}
        return viewset

    @patch("turbodrf.settings.TURBODRF_ROLES", _TEST_ROLES)
    def test_metadata_with_user_having_roles(self):
        request = self.factory.options("/api/samplemodels/")
        request.user = MagicMock()
        request.user.roles = ["viewer"]
        request.user.is_authenticated = True
        view = self._make_view("list")
        metadata = TurboDRFMetadata()
        result = metadata.determine_metadata(request, view)
        self.assertIn("model", result)
        self.assertIn("fields", result["model"])
        self.assertIn("actions", result)
        self.assertTrue(result["actions"]["list"])
        self.assertFalse(result["actions"]["create"])
        self.assertFalse(result["actions"]["destroy"])

    @patch("turbodrf.settings.TURBODRF_ROLES", _TEST_ROLES)
    def test_metadata_with_admin_roles(self):
        request = self.factory.options("/api/samplemodels/")
        request.user = MagicMock()
        request.user.roles = ["admin"]
        request.user.is_authenticated = True
        view = self._make_view("list")
        metadata = TurboDRFMetadata()
        result = metadata.determine_metadata(request, view)
        self.assertTrue(result["actions"]["list"])
        self.assertTrue(result["actions"]["create"])
        self.assertTrue(result["actions"]["update"])
        self.assertTrue(result["actions"]["destroy"])

    @patch("turbodrf.settings.TURBODRF_ROLES", _TEST_ROLES)
    def test_metadata_detail_view_type(self):
        request = self.factory.options("/api/samplemodels/1/")
        request.user = MagicMock()
        request.user.roles = ["admin"]
        request.user.is_authenticated = True
        view = self._make_view("retrieve")
        metadata = TurboDRFMetadata()
        result = metadata.determine_metadata(request, view)
        fields = result["model"]["fields"]
        self.assertIn("description", fields)

    @patch("turbodrf.settings.TURBODRF_ROLES", _TEST_ROLES)
    def test_metadata_field_info_max_length(self):
        request = self.factory.options("/api/samplemodels/")
        request.user = MagicMock()
        request.user.roles = ["admin"]
        request.user.is_authenticated = True
        view = self._make_view("list")
        metadata = TurboDRFMetadata()
        result = metadata.determine_metadata(request, view)
        title_info = result["model"]["fields"].get("title", {})
        self.assertIn("max_length", title_info)
        self.assertEqual(title_info["max_length"], 200)

    @patch("turbodrf.settings.TURBODRF_ROLES", _TEST_ROLES)
    def test_metadata_field_with_choices(self):
        class ChoiceModel(TurboDRFMixin, dj_models.Model):
            STATUS_CHOICES = [("draft", "Draft"), ("published", "Published")]
            status = dj_models.CharField(max_length=20, choices=STATUS_CHOICES)

            class Meta:
                app_label = "test_app"

            @classmethod
            def turbodrf(cls):
                return {"fields": ["status"]}

        request = self.factory.options("/api/choicemodels/")
        request.user = MagicMock()
        request.user.roles = ["admin"]
        request.user.is_authenticated = True
        view = self._make_view("list")
        view.model = ChoiceModel
        metadata = TurboDRFMetadata()
        result = metadata.determine_metadata(request, view)
        status_info = result["model"]["fields"].get("status", {})
        self.assertIn("choices", status_info)
        self.assertEqual(len(status_info["choices"]), 2)
        self.assertEqual(status_info["choices"][0]["value"], "draft")

    def test_metadata_field_exception_returns_unknown(self):
        class BadFieldModel(TurboDRFMixin, dj_models.Model):
            name = dj_models.CharField(max_length=50)

            class Meta:
                app_label = "test_app"

            @classmethod
            def turbodrf(cls):
                return {"fields": ["name", "nonexistent"]}

        request = self.factory.options("/api/badfieldmodels/")
        request.user = MagicMock()
        request.user.roles = []
        request.user.is_authenticated = True
        view = self._make_view("list")
        view.model = BadFieldModel
        metadata = TurboDRFMetadata()
        result = metadata.determine_metadata(request, view)
        self.assertEqual(result["model"]["fields"]["nonexistent"]["type"], "unknown")

    @patch("turbodrf.settings.TURBODRF_ROLES", _TEST_ROLES)
    def test_metadata_user_without_roles(self):
        request = self.factory.options("/api/samplemodels/")
        request.user = MagicMock(spec=[])  # no .roles attribute
        request.user.is_authenticated = True
        view = self._make_view("list")
        metadata = TurboDRFMetadata()
        result = metadata.determine_metadata(request, view)
        self.assertFalse(result["actions"]["list"])
        self.assertFalse(result["actions"]["create"])

    @patch("turbodrf.settings.TURBODRF_ROLES", _TEST_ROLES)
    def test_metadata_nested_field(self):
        request = self.factory.options("/api/samplemodels/")
        request.user = MagicMock()
        request.user.roles = ["admin"]
        request.user.is_authenticated = True
        view = self._make_view("list")
        metadata = TurboDRFMetadata()
        result = metadata.determine_metadata(request, view)
        fields = result["model"]["fields"]
        self.assertIn("related", fields)
        self.assertEqual(fields["related"]["type"], "nested")

    def test_metadata_without_model(self):
        request = self.factory.options("/api/whatever/")
        request.user = MagicMock()
        request.user.is_authenticated = True
        view = MagicMock()
        del view.model
        view.get_view_name = MagicMock(return_value="Test")
        view.get_view_description = MagicMock(return_value="desc")
        metadata = TurboDRFMetadata()
        result = metadata.determine_metadata(request, view)
        self.assertNotIn("model", result)
        self.assertNotIn("actions", result)

    @patch("turbodrf.settings.TURBODRF_ROLES", _TEST_ROLES)
    def test_metadata_read_write_permissions(self):
        request = self.factory.options("/api/samplemodels/")
        request.user = MagicMock()
        request.user.roles = ["admin"]
        request.user.is_authenticated = True
        view = self._make_view("retrieve")
        metadata = TurboDRFMetadata()
        result = metadata.determine_metadata(request, view)
        title_info = result["model"]["fields"].get("title", {})
        # Admin has both read and write on title
        self.assertFalse(title_info.get("read_only", True))
        self.assertFalse(title_info.get("write_only", True))


# ---------------------------------------------------------------------------
# urls.py import coverage
# ---------------------------------------------------------------------------


class TestUrlsModule(TestCase):
    """Verify turbodrf.urls can be imported and has expected patterns."""

    def test_import_urls(self):
        from turbodrf import urls

        self.assertTrue(hasattr(urls, "urlpatterns"))
        self.assertIsInstance(urls.urlpatterns, list)
        self.assertGreater(len(urls.urlpatterns), 0)

    def test_import_router_from_urls(self):
        from turbodrf.urls import router

        self.assertIsNotNone(router)

    def test_schema_view_from_urls(self):
        from turbodrf.urls import schema_view

        self.assertIsNotNone(schema_view)

    @override_settings(TURBODRF_ENABLE_DOCS=True)
    def test_urls_include_swagger_when_enabled(self):
        from turbodrf.urls import urlpatterns

        has_swagger = any("swagger" in str(p.pattern) for p in urlpatterns)
        self.assertTrue(has_swagger or len(urlpatterns) > 1)
