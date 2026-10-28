"""
Regression tests: turbodrf_check reports what the router and the API actually
do. Each test failed before its fix: it reported an ``owner_field``-only model
OK though the router refuses it, reported every column for a detail-only
fields dict, and counted a reverse FK path as an FK.
"""

from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from tests.test_app.models import Deal, SampleModel

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
