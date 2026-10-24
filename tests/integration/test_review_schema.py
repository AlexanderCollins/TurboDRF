"""
Regression tests: the OpenAPI schema matches what the API does.

Each test failed before its fix:

- A ``fields`` dict with only one key documented every column (``"__all__"``)
  for the missing view type, while the API uses the other key; columns with
  no read rule (``secret_field``, ``quantity``) were published to every
  reader, anonymous callers included.
- ``"__all__"`` left forward M2M fields out of the write body and OPTIONS,
  though the API reads and writes them.
- The router's no-trailing-slash duplicates were documented as separate
  paths, so every operation appeared twice.
- The list envelope used OpenAPI 3's ``nullable`` in a Swagger 2 document.
- ``@swagger_auto_schema(request_body=...)`` was ignored on writes and
  custom actions, and an action's declared ``serializer_class`` never
  became its request body.
- The schema ``info`` published example.com terms and contact placeholders.
"""

import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from drf_yasg import openapi
from drf_yasg.utils import no_body
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.test import APIClient, APIRequestFactory

from tests.test_app.models import ArticleWithCategories, RelatedModel, SampleModel
from turbodrf.swagger import TurboDRFSwaggerAutoSchema
from turbodrf.views import TurboDRFViewSet

User = get_user_model()

SWAGGER = "/swagger/?format=openapi"


class _SchemaBase(TestCase):
    def setUp(self):
        cache.clear()

    def _user(self, username, roles):
        user = User.objects.create_user(username=username, password="x")
        user._test_roles = list(roles)
        return user

    def _client(self, user=None):
        client = APIClient()
        if user is not None:
            client.force_authenticate(user=user)
        return client

    def _schema(self, user=None):
        response = self._client(user).get(SWAGGER)
        self.assertEqual(response.status_code, 200, response.content[:500])
        return json.loads(response.content)

    @staticmethod
    def _resolve(schema, node):
        ref = node.get("$ref")
        if ref:
            return schema["definitions"][ref.rsplit("/", 1)[1]]
        return node

    def _response_properties(self, schema, path, method="get"):
        node = schema["paths"][path][method]["responses"]["200"]["schema"]
        return set(self._resolve(schema, node).get("properties", {}))

    def _body_properties(self, schema, path, method):
        operation = schema["paths"][path][method]
        body = next(p for p in operation["parameters"] if p["in"] == "body")
        return set(self._resolve(schema, body["schema"]).get("properties", {}))


class TestSingleKeyFieldsDict(_SchemaBase):
    CONFIG = {"public_access": True, "fields": {"list": ["title", "is_active"]}}

    def setUp(self):
        super().setUp()
        related = RelatedModel.objects.create(name="r")
        self.obj = SampleModel.objects.create(
            title="t", price=Decimal("1.00"), related=related, secret_field="s3cr3t"
        )
        patcher = mock.patch.object(
            SampleModel, "turbodrf", classmethod(lambda cls: self.CONFIG)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_detail_schema_matches_the_api(self):
        admin = self._user("adm_single_key", ["admin"])
        detail = self._client(admin).get(f"/api/samplemodels/{self.obj.pk}/").json()
        self.assertEqual(set(detail) - {"id"}, {"title", "is_active"})

        schema = self._schema(admin)
        retrieve = self._response_properties(schema, "/samplemodels/{id}/")
        self.assertEqual(retrieve - {"id"}, {"title", "is_active"})
        for method in ("put", "patch"):
            body = self._body_properties(schema, "/samplemodels/{id}/", method)
            self.assertTrue(body <= {"title", "is_active"}, (method, body))
        self.assertTrue(
            self._body_properties(schema, "/samplemodels/", "post")
            <= {"title", "is_active"}
        )

    # No field rules: every column is readable, so only the configured
    # fields keep the others out.
    @override_settings(TURBODRF_ROLES={"guest": ["test_app.samplemodel.read"]})
    def test_anonymous_schema_publishes_no_unconfigured_column(self):
        schema = self._schema()
        published = set()
        for name, definition in (schema.get("definitions") or {}).items():
            if name.startswith("test_app_samplemodel_"):
                published.update(definition.get("properties") or {})
        self.assertIn("title", published)
        for column in ("secret_field", "quantity", "description", "price"):
            self.assertNotIn(column, published)


class TestAllIncludesManyToMany(_SchemaBase):
    CONFIG = {"public_access": True, "fields": "__all__"}

    def test_write_body_and_options_include_m2m(self):
        admin = self._user("adm_all_m2m", ["admin"])
        with mock.patch.object(
            ArticleWithCategories, "turbodrf", classmethod(lambda cls: self.CONFIG)
        ):
            self.assertIn("categories", ArticleWithCategories.get_api_fields("detail"))
            schema = self._schema(admin)
            path = "/articlewithcategoriess/{id}/"
            self.assertIn("categories", self._body_properties(schema, path, "put"))
            self.assertIn("categories", self._response_properties(schema, path, "get"))
            options = self._client(admin).options("/api/articlewithcategoriess/")
        self.assertIn("categories", options.json()["model"]["fields"])


class TestEachOperationDocumentedOnce(_SchemaBase):
    def test_no_slash_duplicates_are_not_documented(self):
        schema = self._schema(self._user("adm_paths_once", ["admin"]))
        paths = schema["paths"]
        self.assertIn("/samplemodels/", paths)
        self.assertIn("/samplemodels/{id}/", paths)
        self.assertEqual([p for p in paths if not p.endswith("/")], [])

        operation_ids = [
            op["operationId"]
            for item in paths.values()
            for method, op in item.items()
            if method != "parameters"
        ]
        self.assertEqual(len(operation_ids), len(set(operation_ids)))


class TestPaginationEnvelope(_SchemaBase):
    def test_list_envelope_is_valid_swagger2(self):
        schema = self._schema(self._user("adm_envelope", ["admin"]))
        response = schema["paths"]["/samplemodels/"]["get"]["responses"]["200"]
        envelope = response["schema"]
        self.assertEqual(envelope["required"], ["pagination", "data"])
        pagination = envelope["properties"]["pagination"]
        self.assertEqual(
            set(pagination["properties"]),
            {"next", "previous", "current_page", "total_pages", "total_items"},
        )
        for link in ("next", "previous"):
            self.assertIs(pagination["properties"][link]["x-nullable"], True)
            self.assertEqual(pagination["properties"][link]["format"], "uri")
        # Swagger 2 has no `nullable`; it must not appear anywhere.
        self.assertNotIn('"nullable"', json.dumps(schema))


class _ActionBody(serializers.Serializer):
    reason = serializers.CharField()


class _OverrideBody(serializers.Serializer):
    note = serializers.CharField()


class _ActionViewSet(TurboDRFViewSet):
    model = SampleModel
    queryset = SampleModel.objects.all()

    @action(detail=True, methods=["post"], serializer_class=_ActionBody)
    def archive(self, request, pk=None):
        return Response({})


class TestRequestBodyOverrides(_SchemaBase):
    def _schema_for(self, action_name, method, overrides, view_kwargs=None):
        request = APIRequestFactory().generic(method, "/api/samplemodels/1/x/")
        request.user = self._user(f"adm_{action_name}_{method}", ["admin"])
        view = _ActionViewSet(**(view_kwargs or {}))
        view.action = action_name
        view.request = request
        view.format_kwarg = None
        return TurboDRFSwaggerAutoSchema(
            view=view,
            path="/api/samplemodels/{id}/x/",
            method=method,
            components=openapi.ReferenceResolver(
                "definitions", "parameters", force_init=True
            ),
            request=request,
            overrides=overrides,
        )

    def test_explicit_request_body_wins_on_writes(self):
        schema = self._schema_for("create", "POST", {"request_body": _OverrideBody})
        self.assertIsInstance(schema.get_request_serializer(), _OverrideBody)

    def test_no_body_override_on_writes(self):
        schema = self._schema_for("update", "PUT", {"request_body": no_body})
        self.assertIsNone(schema.get_request_serializer())
        self.assertEqual(schema.get_request_body_parameters(["application/json"]), [])

    def test_explicit_request_body_on_custom_action(self):
        schema = self._schema_for("archive", "POST", {"request_body": _OverrideBody})
        params = schema.get_request_body_parameters(["application/json"])
        self.assertEqual(len(params), 1)
        self.assertIsInstance(schema.get_request_serializer(), _OverrideBody)

    def test_action_serializer_class_documents_its_body(self):
        # DRF hands the action's serializer_class to the view instance.
        schema = self._schema_for(
            "archive", "POST", {}, view_kwargs={"serializer_class": _ActionBody}
        )
        self.assertIsInstance(schema.get_request_serializer(), _ActionBody)
        params = schema.get_request_body_parameters(["application/json"])
        self.assertEqual(len(params), 1)


class TestSchemaInfo(TestCase):
    def _info(self, **kwargs):
        from turbodrf.documentation import get_turbodrf_schema_view

        view = get_turbodrf_schema_view(**kwargs).without_ui(cache_timeout=0)
        response = view(APIRequestFactory().get("/swagger.json"))
        self.assertEqual(response.status_code, 200)
        return response.data["info"]

    def test_no_placeholder_contact_or_terms(self):
        info = self._info()
        self.assertEqual(info["title"], "TurboDRF API")
        self.assertNotIn("termsOfService", info)
        self.assertNotIn("contact", info)
        self.assertNotIn("example.com", json.dumps(info))

    def test_contact_and_terms_when_given(self):
        info = self._info(
            terms_of_service="https://acme.test/terms", contact_email="api@acme.test"
        )
        self.assertEqual(info["termsOfService"], "https://acme.test/terms")
        self.assertEqual(info["contact"]["email"], "api@acme.test")


class TestSchemaWithoutRequest(TestCase):
    def test_no_request_gives_the_anonymous_schema(self):
        from turbodrf.swagger import RoleBasedSchemaGenerator

        cache.clear()
        generator = RoleBasedSchemaGenerator(
            openapi.Info(title="T", default_version="v1")
        )
        schema = generator.get_schema(request=None, public=False)
        self.assertIn("/samplemodels/", schema["paths"])
        self.assertNotIn("/transactions/", schema["paths"])
        for item in schema["paths"].values():
            self.assertEqual(
                {m for m in item if m != "parameters"} - {"get", "head"}, set()
            )
