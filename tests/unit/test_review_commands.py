"""
Regression tests: the management commands report what the router and the
API actually do.

Each test failed before its fix:

- turbodrf_check reported an ``owner_field``-only model OK though the router
  refuses it, reported every column for a detail-only fields dict, printed
  "[compiled] OK" for a model the compiler blocks, and counted a reverse FK
  path as an FK.
- turbodrf_explain printed "Compiled: False" for models the router compiles,
  "Total queries: 1" for a list that takes 2, pruned/permitted fields that
  didn't match the API's response, ignored ``--fields``, and left a model's
  ``turbodrf`` patched if compiling raised.
- turbodrf_benchmark crashed on any model with a nested list field and
  benchmarked an unscoped serializer instead of real requests.
"""

from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from tests.test_app.models import Deal, Gadget, RelatedModel, SampleModel

User = get_user_model()


def _cfg(config):
    return classmethod(lambda cls: config)


def _run(*args, **kwargs):
    out = StringIO()
    call_command(*args, stdout=out, stderr=StringIO(), **kwargs)
    return out.getvalue()


class TestCheckMatchesRouter(TestCase):
    @override_settings(
        TURBODRF_TENANT_MODEL="test_app.Brokerage", TURBODRF_REQUIRE_TENANCY=True
    )
    def test_owner_field_only_model_reported_as_refused(self):
        cfg = {
            "owner_field": "assigned_broker",
            "fields": ["id", "title", "brokerage", "assigned_broker"],
        }
        with mock.patch.object(Deal, "turbodrf", _cfg(cfg)):
            out = _run("turbodrf_check", model="Deal")
        self.assertIn("NOT DECLARED", out)
        self.assertIn("owner_field", out)
        self.assertNotIn("Within-tenant predicates", out)

    def test_detail_only_dict_reports_the_detail_fields(self):
        cfg = {"public_access": True, "fields": {"detail": ["title", "price"]}}
        with mock.patch.object(SampleModel, "turbodrf", _cfg(cfg)):
            out = _run("turbodrf_check", model="SampleModel")
        self.assertIn("Fields: 2 DB, 0 FK, 0 M2M, 0 property", out)

    def test_blocked_model_header_says_not_compiled(self):
        cfg = {"public_access": True, "fields": ["name", "test_models__title"]}
        with mock.patch.object(RelatedModel, "turbodrf", _cfg(cfg)):
            out = _run("turbodrf_check", model="RelatedModel")
        self.assertIn("Served by the serializer path", out)
        header = next(line for line in out.splitlines() if "RelatedModel" in line)
        self.assertIn("[not compiled]", header)
        # test_models is a reverse FK, not an FK.
        self.assertIn("0 FK", out)
        self.assertIn("1 reverse", out)


class TestExplainMatchesApi(TestCase):
    def setUp(self):
        cache.clear()

    def test_compiled_flag_matches_router(self):
        from turbodrf.compiler import is_compiled

        self.assertTrue(is_compiled(SampleModel))
        self.assertIn("Compiled: True", _run("turbodrf_explain", "SampleModel"))

    def test_paginated_list_query_count(self):
        out = _run("turbodrf_explain", "SampleModel")
        self.assertIn("Total queries: 2", out)

    def test_role_fields_match_the_api_response(self):
        rel = RelatedModel.objects.create(name="r")
        SampleModel.objects.create(title="t", price=Decimal("1.00"), related=rel)
        viewer = User.objects.create_user("explain_viewer", password="x")
        viewer._test_roles = ["viewer"]
        client = APIClient()
        client.force_authenticate(viewer)
        row = client.get("/api/samplemodels/").json()["data"][0]

        out = _run("turbodrf_explain", "SampleModel", role="viewer")
        rendered = next(
            line for line in out.splitlines() if line.strip().startswith("Rendered:")
        )
        keys = {k.strip() for k in rendered.split(":", 1)[1].split(",")}
        self.assertEqual(keys, set(row))

    def test_fields_option_selects_like_the_query_parameter(self):
        out = _run("turbodrf_explain", "SampleModel", fields="title")
        rendered = next(
            line for line in out.splitlines() if line.strip().startswith("Rendered:")
        )
        self.assertEqual(rendered.split(":", 1)[1].strip(), "title")

    def test_role_with_no_readable_fields_says_so(self):
        roles = {"nobody": ["test_app.samplemodel.read"], "other": []}
        with override_settings(TURBODRF_ROLES=roles):
            cfg = {"public_access": True, "fields": ["title"]}
            rules = {"other": ["test_app.samplemodel.title.read"]}
            with override_settings(TURBODRF_ROLES={**roles, **rules}):
                with mock.patch.object(SampleModel, "turbodrf", _cfg(cfg)):
                    out = _run("turbodrf_explain", "SampleModel", role="nobody")
        self.assertIn("Rendered: (none)", out)
        self.assertIn("Pruned: title", out)

    def test_turbodrf_restored_when_compiling_raises(self):
        original = Gadget.__dict__["turbodrf"]

        def compile_model(model):
            # Gadget opts out; the forced (patched-config) compile raises.
            if model.turbodrf().get("compiled", True):
                raise RuntimeError("boom")
            return None

        with mock.patch(
            "turbodrf.management.commands.turbodrf_explain.compile_model",
            side_effect=compile_model,
        ):
            with self.assertRaises(RuntimeError):
                _run("turbodrf_explain", "Gadget")
        self.assertIs(Gadget.__dict__["turbodrf"], original)
        self.assertFalse(Gadget.turbodrf().get("compiled", True))

    @override_settings(TURBODRF_PERMISSION_MODE="database")
    def test_role_in_database_mode_is_refused(self):
        out = _run("turbodrf_explain", "SampleModel", role="viewer")
        self.assertIn("static permission mode only", out)


class TestBenchmarkRunsRealRequests(TestCase):
    def setUp(self):
        cache.clear()
        User.objects.create_superuser("bench_root", "b@x.test", "x")

    def test_nested_list_fields_dont_crash(self):
        rel = RelatedModel.objects.create(name="r")
        SampleModel.objects.create(title="t", price=Decimal("1.00"), related=rel)
        out = _run(
            "turbodrf_benchmark",
            "SampleModel",
            user="bench_root",
            requests=2,
            warmup=0,
            page_size=5,
        )
        self.assertIn("1 objects", out)
        self.assertIn("Speedup", out)

    @override_settings(TURBODRF_PERMISSION_MODE="database")
    def test_counts_only_rows_the_user_can_see(self):
        # Funds in another brokerage aren't visible: the benchmark measures
        # the scoped list as that user, not every row in the table. (Fund
        # also has no `objects` manager.)
        from tests.test_app.apps import set_test_brokerage
        from tests.test_app.models import Brokerage, Fund
        from turbodrf.models import RolePermission, TurboDRFRole, UserRole

        role = TurboDRFRole.objects.create(name="fund_reader")
        RolePermission.objects.create(
            role=role, app_label="test_app", model_name="fund", action="read"
        )
        user = User.objects.create_user("fund_user", password="x")
        UserRole.objects.create(user=user, role=role)
        mine = Brokerage.objects.create(name="A")
        theirs = Brokerage.objects.create(name="B")
        set_test_brokerage(user, mine)
        Fund.all_objects.create(name="F1", brokerage=mine)
        Fund.all_objects.create(name="F2", brokerage=theirs)
        Fund.all_objects.create(name="F3", brokerage=theirs)

        out = _run(
            "turbodrf_benchmark",
            "Fund",
            user="fund_user",
            requests=1,
            warmup=0,
        )
        self.assertIn("1 objects", out)
