"""
Regression tests: the OpenAPI schema is scoped to the caller.

Before the fix only ``schema["paths"]`` was role-filtered: ``definitions``
still described every serializer of every TurboDRF model (to anonymous
callers too), the ``<model>_write`` request body came from an unfiltered
serializer (fields the role can't write or read, plus sensitive fields),
``request.session`` was read unconditionally, list responses lost their
pagination envelope, models were resolved by singularizing the URL, and a
logged-in user got an empty schema unless they passed ``?role=``.
"""

import json

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from turbodrf.settings import TURBODRF_SENSITIVE_FIELDS

User = get_user_model()

SWAGGER = "/swagger/?format=openapi"


class SwaggerScopingTestBase(TestCase):
    def setUp(self):
        cache.clear()

    def _user(self, username, roles):
        user = User.objects.create_user(username=username, password="x")
        user._test_roles = list(roles)
        return user

    def _schema(self, user=None, query=""):
        client = APIClient()
        if user is not None:
            client.force_authenticate(user=user)
        response = client.get(SWAGGER + query)
        self.assertEqual(response.status_code, 200, response.content[:500])
        return json.loads(response.content)

    @staticmethod
    def _path(schema, suffix):
        matches = [p for p in schema.get("paths", {}) if p.endswith(suffix)]
        return matches[0] if matches else None

    @staticmethod
    def _resolve(schema, node):
        ref = node.get("$ref")
        if ref:
            return schema["definitions"][ref.rsplit("/", 1)[1]]
        return node

    def _body_properties(self, schema, suffix, method):
        operation = schema["paths"][self._path(schema, suffix)][method]
        body = next(p for p in operation["parameters"] if p["in"] == "body")
        return set(self._resolve(schema, body["schema"]).get("properties", {}))

    @staticmethod
    def _all_definition_properties(schema):
        names = set()
        for definition in (schema.get("definitions") or {}).values():
            names.update((definition.get("properties") or {}).keys())
        return names


class TestDefinitionsAreScoped(SwaggerScopingTestBase):
    def test_anonymous_schema_shows_only_public_reads(self):
        # public_access gives anonymous callers model-level read: the schema
        # documents those reads — only on public models, never a write, and
        # never a field a role has a read rule for (anonymous holds none).
        from tests.test_app.models import SampleModel, Transaction

        schema = self._schema()
        self.assertTrue(schema.get("paths"))
        for path, operations in schema["paths"].items():
            for method in operations:
                if method == "parameters":
                    continue
                self.assertIn(method, {"get", "head", "options"}, path)
        self.assertIsNone(self._path(schema, "/transactions/"))
        self.assertFalse(Transaction.turbodrf().get("public_access"))
        self.assertTrue(SampleModel.turbodrf().get("public_access"))
        sample_props = set()
        for name, definition in schema.get("definitions", {}).items():
            if name.startswith("test_app_samplemodel_"):
                sample_props.update(definition.get("properties", {}))
        for hidden in ("secret_field", "price", "title"):
            self.assertNotIn(hidden, sample_props)

    def test_definitions_only_for_models_the_caller_can_use(self):
        schema = self._schema(self._user("viewer_defs", ["viewer"]))
        self.assertTrue(schema.get("definitions"))
        for name in schema["definitions"]:
            self.assertTrue(
                name.startswith("test_app_samplemodel_")
                or name.startswith("test_app_relatedmodel_"),
                f"viewer schema publishes unrelated definition {name!r}",
            )

    def test_viewer_definitions_hide_fields_viewer_cannot_read(self):
        schema = self._schema(self._user("viewer_fields", ["viewer"]))
        properties = self._all_definition_properties(schema)
        self.assertIn("title", properties)
        self.assertNotIn("price", properties)
        self.assertNotIn("secret_field", properties)

    def test_write_body_lists_only_fields_the_caller_can_write(self):
        # editor: price is read-only, secret_field unreadable.
        schema = self._schema(self._user("editor_body", ["editor"]))
        body = self._body_properties(schema, "/samplemodels/{id}/", "put")
        self.assertIn("title", body)
        self.assertIn("quantity", body)
        self.assertNotIn("price", body)
        self.assertNotIn("secret_field", body)
        # editor can't create → no POST documented at all.
        self.assertNotIn("post", schema["paths"][self._path(schema, "/samplemodels/")])

    def test_sensitive_fields_never_published(self):
        sensitive = list(TURBODRF_SENSITIVE_FIELDS) + ["secret_field"]
        with override_settings(TURBODRF_SENSITIVE_FIELDS=sensitive):
            schema = self._schema(self._user("admin_sensitive", ["admin"]))
            body = self._body_properties(schema, "/samplemodels/", "post")
        self.assertIn("title", body)
        self.assertNotIn("secret_field", body)
        self.assertNotIn("secret_field", json.dumps(schema))

    def test_show_all_fields_flag_does_not_widen_schema(self):
        with override_settings(DEBUG=True, TURBODRF_SWAGGER_SHOW_ALL_FIELDS=True):
            schema = self._schema(self._user("viewer_show_all", ["viewer"]))
        properties = self._all_definition_properties(schema)
        self.assertNotIn("price", properties)
        self.assertNotIn("secret_field", properties)


class TestRoleSelection(SwaggerScopingTestBase):
    def test_logged_in_user_sees_own_schema_without_role_param(self):
        schema = self._schema(self._user("viewer_default", ["viewer"]))
        path = self._path(schema, "/samplemodels/")
        self.assertIsNotNone(path)
        self.assertIn("get", schema["paths"][path])

    def test_role_param_narrows_to_a_held_role(self):
        user = self._user("multi_role", ["admin", "viewer"])

        full = self._schema(user)
        self.assertIn("post", full["paths"][self._path(full, "/samplemodels/")])
        self.assertIn("price", self._all_definition_properties(full))

        narrowed = self._schema(user, "&role=viewer")
        path = self._path(narrowed, "/samplemodels/")
        self.assertIn("get", narrowed["paths"][path])
        self.assertNotIn("post", narrowed["paths"][path])
        self.assertNotIn("price", self._all_definition_properties(narrowed))

    def test_role_param_never_widens(self):
        schema = self._schema(self._user("viewer_escalate", ["viewer"]), "&role=admin")
        path = self._path(schema, "/samplemodels/")
        self.assertNotIn("post", schema["paths"][path])
        self.assertNotIn("secret_field", self._all_definition_properties(schema))

    @override_settings(TURBODRF_PERMISSION_MODE="database")
    def test_database_mode_uses_callers_db_roles_and_ignores_role_param(self):
        from turbodrf.models import RolePermission, TurboDRFRole, UserRole

        role = TurboDRFRole.objects.create(name="db_reader")
        RolePermission.objects.create(
            role=role, app_label="test_app", model_name="samplemodel", action="read"
        )
        user = User.objects.create_user(username="db_user", password="x")
        UserRole.objects.create(user=user, role=role)

        schema = self._schema(user, "&role=db_reader")
        path = self._path(schema, "/samplemodels/")
        self.assertIn("get", schema["paths"][path])
        self.assertNotIn("post", schema["paths"][path])
        self.assertIsNone(self._path(schema, "/deals/"))

    @override_settings(MIDDLEWARE=["django.middleware.common.CommonMiddleware"])
    def test_schema_works_without_session_middleware(self):
        schema = self._schema(self._user("viewer_nosession", ["viewer"]))
        self.assertIsNotNone(self._path(schema, "/samplemodels/"))


class TestResponseShapeAndModelResolution(SwaggerScopingTestBase):
    def test_list_response_documents_pagination_envelope(self):
        schema = self._schema(self._user("admin_list", ["admin"]))
        path = self._path(schema, "/samplemodels/")
        response = schema["paths"][path]["get"]["responses"]["200"]["schema"]
        self.assertEqual(set(response["properties"]), {"pagination", "data"})
        self.assertEqual(
            set(response["properties"]["pagination"]["properties"]),
            {"next", "previous", "current_page", "total_pages", "total_items"},
        )
        data = response["properties"]["data"]
        self.assertEqual(data["type"], "array")
        item = self._resolve(schema, data["items"])
        self.assertIn("title", item["properties"])

    def test_models_resolved_from_registered_viewsets(self):
        # 'categorys' / 'articlewithcategoriess' don't singularize with
        # rstrip("s"), and 'custom-items' isn't a model name at all.
        schema = self._schema(self._user("admin_paths", ["admin"]))
        for suffix in ("/categorys/", "/articlewithcategoriess/", "/custom-items/"):
            self.assertIsNotNone(
                self._path(schema, suffix), f"{suffix} missing for admin"
            )
