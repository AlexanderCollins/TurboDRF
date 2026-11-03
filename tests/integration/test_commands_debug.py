"""
The debugging commands: ``turbodrf_explain`` (why can user X see row Y /
field Z?) and ``turbodrf_schema`` (the docs view's schema for given roles,
offline). Each answer is checked against what the API itself does for the
same caller.
"""

import json
import os
import tempfile
from io import StringIO
from unittest import mock

import yaml
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from tests.test_app.apps import _test_user_brokerages, set_test_brokerage
from tests.test_app.models import (
    Brokerage,
    Deal,
    PlatformJob,
    RelatedModel,
    SampleModel,
    ScopeBoard,
)
from turbodrf.models import RolePermission, TurboDRFRole, UserRole

User = get_user_model()

PLATFORM = dict(
    ROOT_URLCONF="tests.platform_urls",
    TURBODRF_TENANT_MODEL="test_app.Brokerage",
)


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.acme = Brokerage.objects.create(name="Acme")
        cls.globex = Brokerage.objects.create(name="Globex")
        cls.related = RelatedModel.objects.create(name="r", description="d")
        cls.sample = SampleModel.objects.create(
            title="t", price=5, related=cls.related, secret_field="s"
        )

    def setUp(self):
        cache.clear()
        # The commands load the user from the database, where _test_roles
        # doesn't survive: roles here are keyed by username.
        self.roles, self.platform_roles = {}, {}
        for attr, table in (
            ("roles", self.roles),
            ("platform_roles", self.platform_roles),
        ):
            getter = property(lambda u, table=table: list(table.get(u.username, ())))
            patcher = mock.patch.object(User, attr, getter)
            patcher.start()
            self.addCleanup(patcher.stop)

    def user(self, username, roles=(), brokerage=None, platform_roles=()):
        user = User.objects.create_user(username=username, password="x")
        self.roles[username] = list(roles)
        self.platform_roles[username] = list(platform_roles)
        _test_user_brokerages.pop(user.pk, None)
        self.addCleanup(_test_user_brokerages.pop, user.pk, None)
        if brokerage is not None:
            set_test_brokerage(user, brokerage)
        return user

    def get(self, user, url):
        client = APIClient()
        client.force_authenticate(user=user)
        return client.get(url)

    @staticmethod
    def call(command, *args):
        out = StringIO()
        call_command(command, *args, stdout=out, stderr=StringIO())
        return out.getvalue()

    def explain(self, *args):
        return self.call("turbodrf_explain", *args)


def _field(output, name):
    """A field's read / write lines in the Fields section."""
    lines = output.splitlines()
    i = next(i for i, line in enumerate(lines) if line.startswith(f"  {name} ["))
    return lines[i + 1].split(":", 1)[1].strip(), lines[i + 2].split(":", 1)[1].strip()


class TestExplainCaller(_Base):
    def test_roles_tenant_and_actions(self):
        alice = self.user("alice", ["underwriter"], self.acme)
        out = self.explain("--user", "alice", "--model", "test_app.Deal")
        self.assertIn("Roles: underwriter", out)
        self.assertIn(f"Tenant: Acme (pk {self.acme.pk}, from user.brokerage)", out)
        self.assertIn("Route: deals/", out)
        self.assertIn(
            "Rows: tenant_field 'brokerage'; "
            "Owner('assigned_broker', bypass=['admin', 'manager'])",
            out,
        )
        self.assertIn(
            "read    allowed  (test_app.deal.read: granted by underwriter)", out
        )
        self.assertEqual(self.get(alice, "/api/deals/").status_code, 200)

    def test_no_roles_and_no_tenant(self):
        nobody = self.user("nobody")
        out = self.explain("--user", str(nobody.pk), "--model", "Deal")
        self.assertIn("Roles: NONE: every TurboDRF request is a 403", out)
        self.assertIn("Tenant: NONE (user.brokerage is None): sees no tenant rows", out)
        self.assertIn(
            "read    refused  (test_app.deal.read: none of the caller's roles has it)",
            out,
        )
        self.assertEqual(self.get(nobody, "/api/deals/").status_code, 403)

    @override_settings(
        TURBODRF_ROLES={
            "board": ["test_app.scopeboard.read", "test_app.scopeboard.create"]
        }
    )
    def test_read_only_resource_refuses_writes(self):
        self.user("writer", ["board"], self.acme)
        out = self.explain("--user", "writer", "--model", "ScopeBoard")
        self.assertIn("read    allowed", out)
        self.assertIn(
            "create  refused  (test_app.scopeboard.create: granted by board, but "
            "the route doesn't serve it (405))",
            out,
        )

    @override_settings(TURBODRF_PERMISSION_MODE="database")
    def test_database_roles(self):
        user = self.user("db_user")
        role = TurboDRFRole.objects.create(name="db_reader")
        RolePermission.objects.create(
            role=role, app_label="test_app", model_name="samplemodel", action="read"
        )
        UserRole.objects.create(user=user, role=role)
        out = self.explain("--user", "db_user", "--model", "SampleModel")
        self.assertIn("Roles: db_reader", out)
        self.assertIn(
            "read    allowed  (test_app.samplemodel.read: granted by db_reader)", out
        )
        self.assertIn("update  refused", out)

    def test_bypassed_permissions_are_reported(self):
        self.user("bypass", brokerage=self.acme)
        with override_settings(TURBODRF_DISABLE_PERMISSIONS=True):
            out = self.explain("--user", "bypass", "--model", "SampleModel")
        self.assertIn("Permissions are bypassed (TURBODRF_DISABLE_PERMISSIONS)", out)
        self.assertIn("delete  allowed  (TURBODRF_DISABLE_PERMISSIONS)", out)
        self.assertEqual(
            _field(out, "secret_field")[0],
            "yes (permissions bypassed (TURBODRF_DISABLE_PERMISSIONS))",
        )

    def test_unknown_user_model_or_pk(self):
        self.user("alice", ["underwriter"], self.acme)
        for args, message in (
            (["--user", "ghost", "--model", "Deal"], "No user"),
            (["--user", "alice", "--model", "Nope"], "No TurboDRF model"),
            (["--user", "alice", "--model", "auth.User"], "No TurboDRF model"),
            (["--user", "alice", "--model", "Deal", "--pk", "999"], "No test_app.Deal"),
            (["--user", "alice", "--model", "Deal", "--pk", "x"], "No test_app.Deal"),
        ):
            with (
                self.subTest(args=args),
                self.assertRaisesMessage(CommandError, message),
            ):
                self.explain(*args)


class TestExplainFields(_Base):
    def test_field_rules_match_the_response(self):
        editor = self.user("ed", ["editor"])
        out = self.explain("--user", "ed", "--model", "SampleModel")
        self.assertEqual(
            _field(out, "price"),
            (
                "yes (field rule test_app.samplemodel.price.read, granted)",
                "no (field rule test_app.samplemodel.price.write, not granted)",
            ),
        )
        self.assertEqual(
            _field(out, "secret_field"),
            (
                "no (field rule test_app.samplemodel.secret_field.read, not granted)",
                "no (not readable, so not in the serializer)",
            ),
        )
        self.assertEqual(
            _field(out, "related__name"),
            ("yes (nested path: every hop readable)", "no (nested path: read-only)"),
        )
        self.assertEqual(_field(out, "created_at")[1], "no (read-only column)")
        body = self.get(editor, f"/api/samplemodels/{self.sample.pk}/").json()
        self.assertIn("price", body)
        self.assertNotIn("secret_field", body)

    def test_model_level_read_and_nested_hop(self):
        self.user("under", ["underwriter"], self.acme)
        out = self.explain("--user", "under", "--model", "Deal")
        self.assertEqual(
            _field(out, "title"),
            ("yes (model-level read)", "yes (model-level create and update)"),
        )
        self.assertEqual(_field(out, "id")[1], "no (read-only column)")

    @override_settings(
        TURBODRF_ROLES={
            "reader": [
                "test_app.samplemodel.read",
                "test_app.samplemodel.related.read",
                "test_app.relatedmodel.read",
            ],
            "namer": ["test_app.relatedmodel.name.read"],
        }
    )
    def test_nested_path_names_the_hop_it_cannot_read(self):
        reader = self.user("reader", ["reader"])
        out = self.explain("--user", "reader", "--model", "SampleModel")
        self.assertEqual(
            _field(out, "related__name")[0],
            "no (nested path: 'related__name' isn't readable)",
        )
        self.assertEqual(
            _field(out, "related__description")[0],
            "yes (nested path: every hop readable)",
        )
        body = self.get(reader, f"/api/samplemodels/{self.sample.pk}/").json()
        self.assertNotIn("related_name", body)
        self.assertEqual(body["related_description"], "d")

    def test_model_sensitive_fields_need_an_explicit_grant(self):
        alice = self.user("alice", ["underwriter"], self.acme)
        Deal.objects.create(title="x", brokerage=self.acme, assigned_broker=alice)
        with mock.patch.object(Deal, "sensitive_fields", ["title"], create=True):
            out = self.explain("--user", "alice", "--model", "Deal")
            body = self.get(alice, "/api/deals/").json()
        self.assertEqual(
            _field(out, "title"),
            (
                "no (in the model's sensitive_fields: needs "
                "test_app.deal.title.read, not granted)",
                "no (not readable, so not in the serializer)",
            ),
        )
        self.assertNotIn("title", body["data"][0])

    @override_settings(TURBODRF_SENSITIVE_FIELDS=["secret_field"])
    def test_sensitive_deny_list(self):
        self.user("adm", ["admin"])
        out = self.explain("--user", "adm", "--model", "SampleModel")
        self.assertEqual(
            _field(out, "secret_field")[0],
            "no (TURBODRF_SENSITIVE_FIELDS deny-list: never served)",
        )


class TestExplainRow(_Base):
    def test_row_in_another_tenant(self):
        alice = self.user("alice", ["underwriter"], self.acme)
        row = Deal.objects.create(
            title="x", brokerage=self.globex, assigned_broker=alice
        )
        out = self.explain("--user", "alice", "--model", "Deal", "--pk", str(row.pk))
        self.assertIn(
            f"Tenant boundary (brokerage): EXCLUDES it (row's tenant "
            f"{self.globex.pk}, caller's {self.acme.pk})",
            out,
        )
        self.assertIn(
            "Verdict: NOT visible through the API: the tenant boundary "
            "(brokerage) excludes it",
            out,
        )
        self.assertEqual(self.get(alice, f"/api/deals/{row.pk}/").status_code, 404)

    def test_row_hidden_by_a_predicate(self):
        alice = self.user("alice", ["underwriter"], self.acme)
        bob = self.user("bob", ["underwriter"], self.acme)
        boss = self.user("boss", ["manager"], self.acme)
        row = Deal.objects.create(title="x", brokerage=self.acme, assigned_broker=bob)
        out = self.explain("--user", "alice", "--model", "Deal", "--pk", str(row.pk))
        self.assertIn("Tenant boundary (brokerage): passes", out)
        self.assertIn(
            "Owner('assigned_broker', bypass=['admin', 'manager']): EXCLUDES it", out
        )
        self.assertIn(
            "Verdict: NOT visible through the API: Owner('assigned_broker', "
            "bypass=['admin', 'manager']) excludes it",
            out,
        )
        self.assertEqual(self.get(alice, f"/api/deals/{row.pk}/").status_code, 404)
        # A bypass role: the Owner rule doesn't restrict it.
        out = self.explain("--user", "boss", "--model", "Deal", "--pk", str(row.pk))
        self.assertIn(
            "Owner('assigned_broker', bypass=['admin', 'manager']): passes, "
            "no restriction for the caller's roles",
            out,
        )
        self.assertIn("Verdict: visible through the API", out)
        self.assertEqual(self.get(boss, f"/api/deals/{row.pk}/").status_code, 200)

    @override_settings(TURBODRF_ROLES={"board": ["test_app.scopeboard.read"]})
    def test_either_reports_each_child(self):
        owner = self.user("owner", ["board"], self.acme)
        watcher = self.user("watcher", ["board"], self.acme)
        board = ScopeBoard.objects.create(title="b", brokerage=self.acme, owner=owner)
        board.watchers.add(watcher)
        out = self.explain(
            "--user", "watcher", "--model", "ScopeBoard", "--pk", str(board.pk)
        )
        self.assertIn(
            "  Either (any child): passes\n"
            "    Owner('owner', bypass=['manager']): EXCLUDES it\n"
            "    Members('watchers'): passes\n"
            "  Verdict: visible through the API",
            out,
        )
        self.assertEqual(
            self.get(watcher, f"/api/scopeboards/{board.pk}/").status_code, 200
        )

    def test_no_read_permission_comes_first(self):
        self.user("ed", ["editor"], self.acme)
        row = Deal.objects.create(title="x", brokerage=self.globex)
        out = self.explain("--user", "ed", "--model", "Deal", "--pk", str(row.pk))
        self.assertIn(
            "Verdict: NOT visible through the API: the caller may not read the "
            "model (403)",
            out,
        )

    def test_reads_only(self):
        self.user("alice", ["underwriter"], self.acme)
        row = Deal.objects.create(title="x", brokerage=self.acme)
        with CaptureQueriesContext(connection) as queries:
            self.explain("--user", "alice", "--model", "Deal", "--pk", str(row.pk))
        for query in queries.captured_queries:
            self.assertTrue(query["sql"].lstrip().upper().startswith("SELECT"), query)


@override_settings(**PLATFORM)
class TestExplainPlatform(_Base):
    def test_platform_roles_fields_and_rows(self):
        self.user("sup", platform_roles=["support"], roles=["admin"])
        job = PlatformJob.objects.create(brokerage=self.globex, kind="sync")
        args = ["--user", "sup", "--model", "PlatformJob", "--pk", str(job.pk)]
        out = self.explain(*args, "--scope", "platform")
        self.assertIn("Platform roles: support", out)
        self.assertIn("Tenant: none applies on the platform scope", out)
        self.assertIn("Route: jobs/", out)
        self.assertIn("Rows: every tenant's", out)
        self.assertIn(
            "read    allowed  (test_app.platformjob.read: granted by support)", out
        )
        self.assertIn("create  refused", out)
        self.assertEqual(
            _field(out, "payload")[0],
            "no (field rule test_app.platformjob.payload.read, not granted)",
        )
        self.assertEqual(
            _field(out, "summary"),
            (
                "yes (model-level read; left out if it reads a hidden column)",
                "no (property: read-only)",
            ),
        )
        self.assertIn("Tenant boundary: none on the platform scope", out)
        self.assertIn("Verdict: visible through the API", out)

    def test_tenant_roles_grant_nothing_there(self):
        self.user("tenant_admin", roles=["admin", "account_admin"])
        out = self.explain(
            "--user", "tenant_admin", "--model", "PlatformJob", "--scope", "platform"
        )
        self.assertIn("Platform roles: NONE", out)
        self.assertIn("read    refused", out)
        # And the tenant scope: PlatformJob has no tenant route.
        out = self.explain("--user", "tenant_admin", "--model", "PlatformJob")
        self.assertIn("Route: none ('enabled': False)", out)


class _SchemaBase(_Base):
    def schema(self, *args):
        return json.loads(self.call("turbodrf_schema", *args))

    def docs_view(self, user=None, url="/swagger/?format=openapi"):
        client = APIClient()
        if user is not None:
            client.force_authenticate(user=user)
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        schema = response.json()
        # The command has no request host to name.
        del schema["host"], schema["schemes"]
        return schema

    @staticmethod
    def operations(schema, suffix):
        return {
            method
            for path, ops in schema["paths"].items()
            if path.endswith(suffix)
            for method in ops
            if method != "parameters"
        }


class TestSchema(_SchemaBase):
    def test_same_as_the_docs_view(self):
        for roles in (["editor"], ["viewer", "underwriter"]):
            with self.subTest(roles=roles):
                user = self.user("_".join(roles), roles)
                args = [a for role in roles for a in ("--role", role)]
                self.assertEqual(self.schema(*args), self.docs_view(user))
        self.assertEqual(self.schema("--anonymous"), self.docs_view())

    def test_writes_only_for_roles_that_may_write(self):
        admin = self.schema("--role", "admin")
        editor = self.schema("--role", "editor")
        anonymous = self.schema("--anonymous")
        self.assertIn("post", self.operations(admin, "/samplemodels/"))
        self.assertNotIn("post", self.operations(editor, "/samplemodels/"))
        self.assertIn("put", self.operations(editor, "/samplemodels/{id}/"))
        self.assertEqual(self.operations(anonymous, "/samplemodels/"), {"get"})
        self.assertNotIn("/deals/", json.dumps(anonymous["paths"]))

    def test_sensitive_fields_never_appear(self):
        with override_settings(TURBODRF_SENSITIVE_FIELDS=["secret_field"]):
            admin = json.dumps(self.schema("--role", "admin"))
        self.assertIn('"title"', admin)
        self.assertNotIn("secret_field", admin)
        self.assertNotIn("secret_field", json.dumps(self.schema("--role", "viewer")))

    def test_unknown_role(self):
        with self.assertRaisesMessage(CommandError, "Unknown tenant role(s): edtor"):
            self.call("turbodrf_schema", "--role", "edtor")

    def test_role_or_anonymous_is_required(self):
        with self.assertRaises(CommandError):
            self.call("turbodrf_schema")
        with self.assertRaises(CommandError):
            self.call("turbodrf_schema", "--role", "admin", "--anonymous")

    def test_yaml_to_a_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "schema.yaml")
            args = ["--role", "viewer", "--format", "yaml", "--output", path]
            out = self.call("turbodrf_schema", *args)
            with open(path, encoding="utf-8") as stream:
                schema = yaml.safe_load(stream)
        self.assertEqual(out, "")
        self.assertEqual(schema["swagger"], "2.0")
        self.assertEqual(self.operations(schema, "/samplemodels/"), {"get"})

    @override_settings(TURBODRF_PERMISSION_MODE="database")
    def test_database_roles(self):
        role = TurboDRFRole.objects.create(name="db_reader")
        RolePermission.objects.create(
            role=role, app_label="test_app", model_name="samplemodel", action="read"
        )
        schema = self.schema("--role", "db_reader")
        self.assertEqual(self.operations(schema, "/samplemodels/"), {"get"})
        self.assertNotIn("/relatedmodels/", json.dumps(schema["paths"]))


@override_settings(**PLATFORM)
class TestPlatformSchema(_SchemaBase):
    def test_platform_roles_and_the_gate(self):
        # IsPlatformStaff reads user.is_staff, which a role list doesn't have:
        # a role caller is taken to pass the gate.
        support = self.schema("--role", "support", "--scope", "platform")
        self.assertIn("get", self.operations(support, "/console-api/accounts/"))
        self.assertNotIn("post", self.operations(support, "/console-api/accounts/"))
        self.assertIn("post", self.operations(support, "/console-api/tickets/"))
        self.assertNotIn("payload", json.dumps(support))
        self.assertNotIn("/api/", json.dumps(support["paths"]))
        auditor = self.schema("--role", "auditor", "--scope", "platform")
        self.assertIn("payload", json.dumps(auditor))
        anonymous = self.schema("--anonymous", "--scope", "platform")
        self.assertEqual(anonymous["paths"], {})

    def test_platform_roles_are_checked_against_their_map(self):
        with self.assertRaisesMessage(CommandError, "Unknown platform role(s)"):
            self.call("turbodrf_schema", "--role", "viewer", "--scope", "platform")
