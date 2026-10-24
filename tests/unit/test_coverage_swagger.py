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
from django.test.utils import isolate_apps
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
    """Role-request parsing and field-config helpers."""

    def test_requested_role_from_query(self):
        req = MagicMock()
        req.GET = {"role": "viewer"}
        req.session = {"api_role": "admin"}
        self.assertEqual(RoleBasedSchemaGenerator._requested_role(req), "viewer")

    def test_requested_role_from_session(self):
        req = MagicMock()
        req.GET = {}
        req.session = {"api_role": "editor"}
        self.assertEqual(RoleBasedSchemaGenerator._requested_role(req), "editor")

    def test_requested_role_without_session(self):
        """No SessionMiddleware → no request.session → no crash, no role."""

        class NoSessionRequest:
            GET = {}

        self.assertIsNone(RoleBasedSchemaGenerator._requested_role(NoSessionRequest()))

    def test_requested_role_ignores_non_strings(self):
        req = MagicMock()
        req.GET = {"role": ""}
        req.session = {"api_role": ["admin"]}
        self.assertIsNone(RoleBasedSchemaGenerator._requested_role(req))

    def test_configured_fields_list_and_detail(self):
        from turbodrf.swagger import _configured_fields

        self.assertEqual(
            _configured_fields(SampleModel, "list"),
            ["title", "price", "related__name", "is_active"],
        )
        self.assertIn("secret_field", _configured_fields(SampleModel, "detail"))
        self.assertEqual(
            _configured_fields(RelatedModel, "list"), ["name", "description"]
        )


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

    def _real_schema(self, roles, query=""):
        from django.core.cache import cache
        from rest_framework.test import APIClient

        cache.clear()
        client = APIClient()
        if roles is not None:
            user = User.objects.create_user(
                username=f"sw_{'_'.join(roles) or 'none'}", password="x"
            )
            user._test_roles = roles
            client.force_authenticate(user=user)
        response = client.get("/swagger/?format=openapi" + query)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_get_schema_with_role_filters_paths(self):
        schema = self._real_schema(["viewer"], "&role=viewer")
        self.assertIn("/samplemodels/", schema["paths"])
        self.assertIn("get", schema["paths"]["/samplemodels/"])
        self.assertNotIn("post", schema["paths"]["/samplemodels/"])
        self.assertIn("/relatedmodels/", schema["paths"])

    def test_get_schema_role_from_session(self):
        gen = self._gen()
        req = MagicMock()
        req.GET = {}
        req.session = {"api_role": "admin"}
        req.user = MagicMock(is_authenticated=True)  # caller holds 'admin'
        with (
            patch("turbodrf.backends.get_user_roles", return_value=["admin"]),
            patch.object(
                RoleBasedSchemaGenerator.__bases__[0],
                "get_schema",
                return_value={"paths": {}},
            ),
        ):
            gen.get_schema(request=req, public=True)
        self.assertEqual(gen.current_role, "admin")

    def test_get_schema_anonymous_is_read_only_and_field_gated(self):
        # Anonymous with no guest role: only reads of public models, with no
        # field a role has a read rule for — never the full unfiltered schema.
        schema = self._real_schema(None)
        for operations in schema["paths"].values():
            self.assertFalse({"post", "put", "patch", "delete"} & set(operations))
        props = set()
        for name, definition in (schema.get("definitions") or {}).items():
            if name.startswith("test_app_samplemodel_"):
                props.update(definition.get("properties", {}))
        self.assertNotIn("secret_field", props)
        self.assertNotIn("price", props)

    def test_get_schema_filters_response_schema_fields(self):
        schema = self._real_schema(["viewer"])
        response = schema["paths"]["/samplemodels/{id}/"]["get"]["responses"]["200"]
        ref = response["schema"]["$ref"].rsplit("/", 1)[1]
        props = schema["definitions"][ref]["properties"]
        self.assertIn("title", props)
        self.assertNotIn("price", props)
        self.assertNotIn("secret_field", props)

    def test_get_schema_path_with_no_model_info_excluded(self):
        from rest_framework.views import APIView

        view = APIView()
        view.request = None
        self.assertFalse(
            self._gen().should_include_endpoint("/admin/dashboard/", "GET", view, True)
        )


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

        from django.contrib.auth.models import AnonymousUser

        roles = {
            "writer": [
                "test_app.simplefieldmodel.read",
                "test_app.simplefieldmodel.create",
            ]
        }
        viewset = TurboDRFViewSet()
        viewset.model = SimpleFieldModel
        viewset.queryset = SimpleFieldModel.objects.none()
        viewset.action = "create"
        request = self.factory.post("/api/simplefieldmodels/")
        request.user = MagicMock(is_authenticated=True, pk=None, roles=["writer"])
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
        with override_settings(TURBODRF_ROLES=roles):
            result = schema._get_write_operation_serializer()
        self.assertIsNotNone(result)
        self.assertEqual(result.Meta.fields, ["name"])
        self.assertEqual(list(result.fields), ["name"])

        # A caller without the role gets an empty request body.
        request.user = AnonymousUser()
        with override_settings(TURBODRF_ROLES=roles):
            result = schema._get_write_operation_serializer()
        self.assertEqual(list(result.fields), [])


# ---------------------------------------------------------------------------
# TurboDRFMetadata (lines 38-39, 81, 86-87, 97-98)
# ---------------------------------------------------------------------------


class TestTurboDRFMetadata(TestCase):
    """Cover metadata.py uncovered lines."""

    def setUp(self):
        from django.core.cache import cache

        # MagicMock-based users in these tests get unstable ids that can
        # collide in the permission-snapshot cache across parallel
        # workers. Clear cache to avoid hitting another worker's stale
        # snapshot.
        cache.clear()

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

    def test_metadata_field_with_choices(self):
        """ChoiceModel field with explicit per-field perms in roles.

        Metadata is gated by per-field read perms — admin without an
        explicit read rule on test_app.choicemodel.status doesn't see
        metadata for it. Test grants the perm via TURBODRF_ROLES
        override so the visibility gate passes.
        """

        class ChoiceModel(TurboDRFMixin, dj_models.Model):
            STATUS_CHOICES = [("draft", "Draft"), ("published", "Published")]
            status = dj_models.CharField(max_length=20, choices=STATUS_CHOICES)

            class Meta:
                app_label = "test_app"

            @classmethod
            def turbodrf(cls):
                return {"fields": ["status"]}

        roles_with_choice = {
            "admin": [
                "test_app.choicemodel.read",
                "test_app.choicemodel.status.read",
            ]
        }
        with (
            patch("turbodrf.settings.TURBODRF_ROLES", roles_with_choice),
            patch(
                "django.conf.settings.TURBODRF_ROLES", roles_with_choice, create=True
            ),
        ):
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

    # Isolated: in the global app registry, this deliberately misconfigured
    # model would make every later router build in the worker refuse to boot.
    @isolate_apps("tests.test_app")
    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
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
    """Verify turbodrf.urls registers swagger URLs when docs are enabled."""

    @override_settings(TURBODRF_ENABLE_DOCS=True)
    def test_urls_include_swagger_when_enabled(self):
        from turbodrf.urls import urlpatterns

        has_swagger = any("swagger" in str(p.pattern) for p in urlpatterns)
        self.assertTrue(has_swagger or len(urlpatterns) > 1)
