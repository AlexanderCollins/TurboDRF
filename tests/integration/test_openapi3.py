"""
The OpenAPI 3 schema (``turbodrf.openapi``, the ``openapi3`` extra) is scoped
to the caller exactly as the Swagger 2 one is. These mirror
test_swagger_scoping.py, test_review_schema.py and the platform schema tests
in test_platform_scope.py, against ``/openapi/`` and ``/console-openapi/``.
Every schema fetched here is also checked to be valid OpenAPI 3 and generated
without drf-spectacular warnings.
"""

import json
import sys
from decimal import Decimal
from importlib.util import find_spec
from unittest import mock, skipUnless

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.authentication import BaseAuthentication
from rest_framework.test import APIClient, APIRequestFactory

from tests.test_app.models import ArticleWithCategories, RelatedModel, SampleModel
from turbodrf.openapi import get_openapi3_view
from turbodrf.settings import TURBODRF_SENSITIVE_FIELDS

User = get_user_model()

OPENAPI = "/openapi/"
REF = "#/components/schemas/"
SAMPLES = "/api/samplemodels/"
SAMPLE = "/api/samplemodels/{id}/"

needs_spectacular = skipUnless(
    find_spec("drf_spectacular"), "drf-spectacular is not installed (openapi3 extra)"
)


class _HeaderAuthentication(BaseAuthentication):
    """``Authorization: Test <username>``, as a token or JWT would be."""

    def authenticate(self, request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.startswith("Test "):
            return None
        return User.objects.get(username=header[len("Test ") :]), None


@needs_spectacular
class _OpenAPIBase(TestCase):
    def setUp(self):
        cache.clear()

    def _user(self, username, roles=(), platform_roles=(), is_staff=False):
        user = User.objects.create_user(
            username=username, password="x", is_staff=is_staff
        )
        user._test_roles = list(roles)
        user._test_platform_roles = list(platform_roles)
        return user

    def _client(self, user=None):
        client = APIClient()
        if user is not None:
            client.force_authenticate(user=user)
        return client

    def _schema(self, user=None, query="", url=OPENAPI):
        from drf_spectacular.drainage import GENERATOR_STATS
        from drf_spectacular.validation import validate_schema

        response = self._client(user).get(url + query)
        self.assertEqual(response.status_code, 200, response.content[:500])
        schema = json.loads(response.content)
        validate_schema(schema)
        self.assertFalse(
            GENERATOR_STATS,
            (dict(GENERATOR_STATS._warn_cache), dict(GENERATOR_STATS._error_cache)),
        )
        return schema

    @staticmethod
    def _components(schema):
        return schema.get("components", {}).get("schemas", {})

    def _resolve(self, schema, node):
        ref = node.get("$ref")
        return self._components(schema)[ref[len(REF) :]] if ref else node

    def _content(self, schema, node):
        return self._resolve(schema, node["content"]["application/json"]["schema"])

    def _body_properties(self, schema, path, method):
        body = schema["paths"][path][method]["requestBody"]
        return set(self._content(schema, body).get("properties", {}))

    def _response_properties(self, schema, path, method="get", code="200"):
        response = schema["paths"][path][method]["responses"][code]
        return set(self._content(schema, response).get("properties", {}))

    def _all_component_properties(self, schema):
        names = set()
        for component in self._components(schema).values():
            names.update(component.get("properties") or {})
        return names

    def _sample_model_names(self, schema):
        """Every property and parameter name published for SampleModel."""
        names = set()
        for name, component in self._components(schema).items():
            if name.startswith("test_app_samplemodel_"):
                names.update(component.get("properties") or {})
        for path in (SAMPLES, SAMPLE):
            for operation in schema["paths"][path].values():
                names.update(p["name"] for p in operation.get("parameters", []))
        return names


class TestScopedToTheCaller(_OpenAPIBase):
    def test_anonymous_schema_shows_only_public_reads(self):
        from tests.test_app.models import Transaction

        schema = self._schema()
        self.assertTrue(schema["paths"])
        for path, item in schema["paths"].items():
            self.assertEqual(set(item), {"get"}, path)
        self.assertNotIn("/api/transactions/", schema["paths"])
        self.assertFalse(Transaction.turbodrf().get("public_access"))
        self.assertTrue(SampleModel.turbodrf().get("public_access"))
        # Anonymous holds no read rule: neither the bodies nor the filter
        # parameters name a field a role has one for.
        published = self._sample_model_names(schema)
        self.assertIn("page", published)
        for hidden in ("secret_field", "price", "title"):
            self.assertFalse({n for n in published if n.startswith(hidden)}, hidden)

    def test_components_only_for_models_the_caller_can_use(self):
        schema = self._schema(self._user("viewer_defs", ["viewer"]))
        self.assertTrue(self._components(schema))
        for name in self._components(schema):
            self.assertTrue(
                name.startswith(("test_app_samplemodel_", "test_app_relatedmodel_")),
                f"viewer schema publishes unrelated component {name!r}",
            )
        self.assertEqual(
            sorted(schema["paths"]),
            ["/api/relatedmodels/", "/api/relatedmodels/{id}/", SAMPLES, SAMPLE],
        )

    def test_viewer_components_hide_fields_viewer_cannot_read(self):
        schema = self._schema(self._user("viewer_fields", ["viewer"]))
        properties = self._all_component_properties(schema)
        self.assertIn("title", properties)
        self.assertNotIn("price", properties)
        self.assertNotIn("secret_field", json.dumps(schema))

    def test_write_body_lists_only_fields_the_caller_can_write(self):
        # editor: price is read-only, secret_field unreadable.
        schema = self._schema(self._user("editor_body", ["editor"]))
        for method in ("put", "patch"):
            body = self._body_properties(schema, SAMPLE, method)
            self.assertIn("title", body)
            self.assertIn("quantity", body)
            self.assertNotIn("price", body)
            self.assertNotIn("secret_field", body)
        # editor can't create or delete: neither is documented.
        self.assertNotIn("post", schema["paths"][SAMPLES])
        self.assertNotIn("delete", schema["paths"][SAMPLE])

    def test_sensitive_fields_never_published(self):
        sensitive = list(TURBODRF_SENSITIVE_FIELDS) + ["secret_field"]
        with override_settings(TURBODRF_SENSITIVE_FIELDS=sensitive):
            schema = self._schema(self._user("admin_sensitive", ["admin"]))
        body = self._body_properties(schema, SAMPLES, "post")
        self.assertIn("title", body)
        self.assertNotIn("secret_field", body)
        # Nor in a response, a filter parameter or anywhere else.
        self.assertNotIn("secret_field", json.dumps(schema))

    def test_show_all_fields_flag_does_not_widen_schema(self):
        with override_settings(DEBUG=True, TURBODRF_SWAGGER_SHOW_ALL_FIELDS=True):
            schema = self._schema(self._user("viewer_show_all", ["viewer"]))
        properties = self._all_component_properties(schema)
        self.assertNotIn("price", properties)
        self.assertNotIn("secret_field", properties)

    def test_spectacular_schema_class_does_not_widen_schema(self):
        # drf-spectacular's install docs set DEFAULT_SCHEMA_CLASS; TurboDRF's
        # views keep TurboDRF's AutoSchema.
        from django.conf import settings

        rest = {
            **settings.REST_FRAMEWORK,
            "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
        }
        with override_settings(REST_FRAMEWORK=rest):
            schema = self._schema(self._user("viewer_spectacular", ["viewer"]))
        self.assertNotIn("post", schema["paths"][SAMPLES])
        self.assertNotIn("price", self._all_component_properties(schema))

    def test_no_request_and_public_give_the_anonymous_schema(self):
        # drf-spectacular's SERVE_PUBLIC (True by default) passes public=True:
        # it must not skip the permission check.
        generator = get_openapi3_view().cls.generator_class()
        schema = generator.get_schema(request=None, public=True)
        self.assertIn(SAMPLES, schema["paths"])
        self.assertNotIn("/api/transactions/", schema["paths"])
        for item in schema["paths"].values():
            self.assertEqual(set(item), {"get"})


class TestRoleSelection(_OpenAPIBase):
    def test_logged_in_user_sees_own_schema_without_role_param(self):
        schema = self._schema(self._user("viewer_default", ["viewer"]))
        self.assertIn("get", schema["paths"][SAMPLES])

    def test_role_param_narrows_to_a_held_role(self):
        user = self._user("multi_role", ["admin", "viewer"])

        full = self._schema(user)
        self.assertIn("post", full["paths"][SAMPLES])
        self.assertIn("price", self._all_component_properties(full))

        narrowed = self._schema(user, "?role=viewer")
        self.assertIn("get", narrowed["paths"][SAMPLES])
        self.assertNotIn("post", narrowed["paths"][SAMPLES])
        self.assertNotIn("price", json.dumps(narrowed))

    def test_role_param_never_widens(self):
        schema = self._schema(self._user("viewer_escalate", ["viewer"]), "?role=admin")
        self.assertNotIn("post", schema["paths"][SAMPLES])
        self.assertNotIn("secret_field", json.dumps(schema))

    @override_settings(TURBODRF_PERMISSION_MODE="database")
    def test_database_mode_uses_callers_db_roles_and_ignores_role_param(self):
        from turbodrf.models import RolePermission, TurboDRFRole, UserRole

        role = TurboDRFRole.objects.create(name="db_reader")
        RolePermission.objects.create(
            role=role, app_label="test_app", model_name="samplemodel", action="read"
        )
        user = User.objects.create_user(username="db_user", password="x")
        UserRole.objects.create(user=user, role=role)

        schema = self._schema(user, "?role=db_reader")
        self.assertEqual(set(schema["paths"][SAMPLES]), {"get"})
        self.assertNotIn("/api/deals/", schema["paths"])

    def test_caller_authenticated_by_header_gets_their_schema(self):
        # drf-spectacular builds each operation's request without the
        # Authorization header; the caller it authenticated is kept.
        User.objects.create_user("header_admin", password="x", is_superuser=True)
        view = type(
            "HeaderAuthOpenAPIView",
            (get_openapi3_view().cls,),
            {"authentication_classes": [_HeaderAuthentication]},
        ).as_view()
        request = APIRequestFactory().get(
            OPENAPI, HTTP_AUTHORIZATION="Test header_admin"
        )
        response = view(request)
        response.render()
        schema = json.loads(response.content)
        self.assertIn("post", schema["paths"][SAMPLES])  # admin, not anonymous
        self.assertNotIn("/api/transactions/", schema["paths"])

    @override_settings(MIDDLEWARE=["django.middleware.common.CommonMiddleware"])
    def test_schema_works_without_session_middleware(self):
        schema = self._schema(self._user("viewer_nosession", ["viewer"]))
        self.assertIn(SAMPLES, schema["paths"])


class TestSchemaShape(_OpenAPIBase):
    def test_list_response_documents_pagination_envelope(self):
        schema = self._schema(self._user("admin_list", ["admin"]))
        response = schema["paths"][SAMPLES]["get"]["responses"]["200"]
        node = response["content"]["application/json"]["schema"]
        self.assertEqual(node, {"$ref": REF + "test_app_samplemodel_list_page"})
        envelope = self._resolve(schema, node)
        self.assertEqual(envelope["required"], ["pagination", "data"])
        pagination = envelope["properties"]["pagination"]["properties"]
        self.assertEqual(
            set(pagination),
            {"next", "previous", "current_page", "total_pages", "total_items"},
        )
        for link in ("next", "previous"):
            self.assertIs(pagination[link]["nullable"], True)
            self.assertEqual(pagination[link]["format"], "uri")
        data = envelope["properties"]["data"]
        self.assertEqual(data["type"], "array")
        self.assertEqual(data["items"], {"$ref": REF + "test_app_samplemodel_list"})
        self.assertIn("title", self._resolve(schema, data["items"])["properties"])

    def test_bodies_and_component_names(self):
        schema = self._schema(self._user("admin_names", ["admin"]))
        operations = schema["paths"][SAMPLE]
        detail = {"$ref": REF + "test_app_samplemodel_detail"}
        for method in ("get", "put", "patch"):
            content = operations[method]["responses"]["200"]["content"]
            self.assertEqual(content["application/json"]["schema"], detail)
        created = schema["paths"][SAMPLES]["post"]["responses"]["201"]["content"]
        self.assertEqual(created["application/json"]["schema"], detail)
        self.assertEqual(set(operations["delete"]["responses"]), {"204"})
        body = schema["paths"][SAMPLES]["post"]["requestBody"]["content"]
        self.assertEqual(
            body["application/json"]["schema"],
            {"$ref": REF + "test_app_samplemodel_write"},
        )
        self.assertTrue(
            {
                "test_app_samplemodel_list",
                "test_app_samplemodel_list_page",
                "test_app_samplemodel_detail",
                "test_app_samplemodel_write",
            }
            <= set(self._components(schema))
        )
        # TurboDRFSerializer's docstring isn't a component description.
        self.assertNotIn("Base serializer", json.dumps(schema))

    def test_each_operation_documented_once(self):
        schema = self._schema(self._user("admin_paths_once", ["admin"]))
        paths = schema["paths"]
        self.assertIn(SAMPLES, paths)
        self.assertIn(SAMPLE, paths)
        self.assertEqual([p for p in paths if not p.endswith("/")], [])
        operation_ids = [
            op["operationId"] for item in paths.values() for op in item.values()
        ]
        self.assertEqual(len(operation_ids), len(set(operation_ids)))
        self.assertEqual(paths[SAMPLES]["get"]["operationId"], "samplemodels_list")

    def test_models_resolved_from_registered_viewsets(self):
        schema = self._schema(self._user("admin_paths", ["admin"]))
        for path in (
            "/api/categorys/",
            "/api/articlewithcategoriess/",
            "/api/custom-items/",
        ):
            self.assertIn(path, schema["paths"], f"{path} missing for admin")

    def test_path_parameter_named_after_the_primary_key(self):
        schema = self._schema()
        self.assertIn("/api/paritycodepks/{code}/", schema["paths"])

    def test_json_by_default_yaml_when_asked(self):
        import yaml

        client = self._client(self._user("viewer_formats", ["viewer"]))
        response = client.get(OPENAPI)
        self.assertEqual(response["Content-Type"], "application/vnd.oai.openapi+json")
        as_json = json.loads(response.content)
        for kwargs in (
            {"data": {"format": "yaml"}},
            {"HTTP_ACCEPT": "application/yaml"},
        ):
            response = client.get(OPENAPI, **kwargs)
            self.assertNotIn("json", response["Content-Type"])
            self.assertFalse(response.content.lstrip().startswith(b"{"))
            self.assertEqual(yaml.safe_load(response.content), as_json)


class TestFieldsConfig(_OpenAPIBase):
    def _patch_config(self, model, config):
        patcher = mock.patch.object(model, "turbodrf", classmethod(lambda cls: config))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_single_key_fields_dict_matches_the_api(self):
        self._patch_config(
            SampleModel,
            {"public_access": True, "fields": {"list": ["title", "is_active"]}},
        )
        related = RelatedModel.objects.create(name="r")
        obj = SampleModel.objects.create(
            title="t", price=Decimal("1.00"), related=related, secret_field="s3cr3t"
        )
        admin = self._user("adm_single_key", ["admin"])
        detail = self._client(admin).get(f"/api/samplemodels/{obj.pk}/").json()
        self.assertEqual(set(detail) - {"id"}, {"title", "is_active"})

        schema = self._schema(admin)
        self.assertEqual(
            self._response_properties(schema, SAMPLE) - {"id"}, {"title", "is_active"}
        )
        for method in ("put", "patch"):
            body = self._body_properties(schema, SAMPLE, method)
            self.assertTrue(body <= {"title", "is_active"}, (method, body))
        self.assertTrue(
            self._body_properties(schema, SAMPLES, "post") <= {"title", "is_active"}
        )

    # No field rules: every column is readable, so only the configured
    # fields keep the others out.
    @override_settings(TURBODRF_ROLES={"guest": ["test_app.samplemodel.read"]})
    def test_anonymous_schema_publishes_no_unconfigured_column(self):
        self._patch_config(
            SampleModel,
            {"public_access": True, "fields": {"list": ["title", "is_active"]}},
        )
        schema = self._schema()
        published = self._sample_model_names(schema)
        self.assertIn("title", published)
        for column in ("secret_field", "quantity", "description", "price"):
            self.assertFalse({n for n in published if n.startswith(column)}, column)

    def test_write_body_includes_m2m(self):
        self._patch_config(
            ArticleWithCategories, {"public_access": True, "fields": "__all__"}
        )
        schema = self._schema(self._user("adm_all_m2m", ["admin"]))
        path = "/api/articlewithcategoriess/{id}/"
        self.assertIn("categories", self._body_properties(schema, path, "put"))
        self.assertIn("categories", self._response_properties(schema, path))


@override_settings(
    ROOT_URLCONF="tests.platform_urls", TURBODRF_TENANT_MODEL="test_app.Brokerage"
)
class TestPlatformSchema(_OpenAPIBase):
    CONSOLE = "/console-openapi/"

    def _platform(self, user, query=""):
        return self._schema(user, query, url=self.CONSOLE)

    def test_schema_view_is_gated(self):
        outsider = self._user("outsider_doc", platform_roles=["admin"])
        self.assertEqual(self._client(outsider).get(self.CONSOLE).status_code, 403)
        self.assertEqual(self._client().get(self.CONSOLE).status_code, 403)
        admin = self._user("admin_gate_doc", platform_roles=["admin"], is_staff=True)
        self.assertEqual(self._client(admin).get(self.CONSOLE).status_code, 200)

    def test_documents_this_routers_platform_routes_only(self):
        admin = self._user("admin_doc", platform_roles=["admin"], is_staff=True)
        schema = self._platform(admin)
        self.assertEqual(schema["info"]["title"], "Console API")
        self.assertEqual(
            sorted(schema["paths"]),
            [
                "/console-api/accounts/",
                "/console-api/accounts/{id}/",
                "/console-api/jobs/",
                "/console-api/jobs/failed/",
                "/console-api/jobs/ping/",
                "/console-api/jobs/{id}/",
                "/console-api/jobs/{id}/retry/",
                "/console-api/tickets/",
                "/console-api/tickets/{id}/",
            ],
        )
        self.assertIn("staff_notes", json.dumps(schema))
        for name in self._components(schema):
            self.assertIn("platform_test_app_", name)

    def test_scoped_to_platform_roles(self):
        support = self._user("support_doc", platform_roles=["support"], is_staff=True)
        schema = self._platform(support)
        self.assertEqual(set(schema["paths"]["/console-api/accounts/"]), {"get"})
        self.assertNotIn("payload", json.dumps(schema))
        # Tenant roles document nothing here.
        tenant_admin = self._user(
            "tenant_doc", roles=["account_admin", "admin"], is_staff=True
        )
        self.assertEqual(self._platform(tenant_admin)["paths"], {})

    def test_role_narrowing_uses_platform_roles(self):
        user = self._user(
            "narrow_doc", platform_roles=["admin", "auditor"], is_staff=True
        )
        paths = self._platform(user, "?role=auditor")["paths"]
        self.assertEqual(
            sorted(paths),
            [
                "/console-api/jobs/",
                "/console-api/jobs/failed/",
                "/console-api/jobs/ping/",
                "/console-api/jobs/{id}/",
            ],
        )

    def test_tenant_schema_has_no_platform_routes_or_fields(self):
        admin = self._user("tenant_admin_doc", roles=["account_admin", "admin"])
        schema = self._schema(admin)
        self.assertIn("/api/platform-accounts/", schema["paths"])
        dumped = json.dumps(schema)
        for platform_only in ("staff_notes", "internal_note", "/jobs/", "console"):
            self.assertNotIn(platform_only, dumped)


class TestOpenAPIView(SimpleTestCase):
    def test_missing_drf_spectacular_raises_with_install_hint(self):
        with mock.patch.dict(sys.modules, {"drf_spectacular": None}):
            with self.assertRaisesMessage(ImproperlyConfigured, "turbodrf[openapi3]"):
                get_openapi3_view()

    @override_settings(TURBODRF_ENABLE_DOCS=False)
    def test_none_when_docs_are_disabled(self):
        self.assertIsNone(get_openapi3_view())

    @needs_spectacular
    def test_platform_scope_requires_a_gate(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "requires a gate"):
            get_openapi3_view(scope="platform")
        with self.assertRaisesMessage(ImproperlyConfigured, "scope must be one of"):
            get_openapi3_view(scope="console")

    @needs_spectacular
    def test_turbodrf_urls_serves_openapi(self):
        from turbodrf import urls

        names = [getattr(pattern, "name", None) for pattern in urls.urlpatterns]
        self.assertIn("turbodrf-openapi", names)
