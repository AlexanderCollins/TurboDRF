"""Computed (@property) read fields on the serializer path, and opt-in
full_clean model validation (model clean()/constraints -> 400, not 500)."""

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from tests.test_app.models import Gadget


class TestComputedFields(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.gadget = Gadget.objects.create(name="wrench", qty=3)

    def test_property_field_rendered_on_serializer_path(self):
        # Gadget is compiled=False -> DRF serializer path; `label` is a @property.
        resp = self.client.get(f"/api/gadgets/{self.gadget.pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["label"], "wrench x3")


class TestFullClean(TestCase):
    def setUp(self):
        self.client = APIClient()

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_full_clean_allows_valid_write(self):
        resp = self.client.post(
            "/api/gadgets/", {"name": "ok", "qty": 1}, format="json"
        )
        self.assertEqual(resp.status_code, 201)

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_full_clean_rejects_business_rule_as_400(self):
        # clean() raises for name == "forbidden" -> 400, not a 500.
        resp = self.client.post(
            "/api/gadgets/", {"name": "forbidden", "qty": 1}, format="json"
        )
        self.assertEqual(resp.status_code, 400)
        # the clean() error surfaces in the response (error-envelope agnostic)
        self.assertIn("forbidden", str(resp.data))

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_property_field_is_read_only(self):
        # Writing `label` is ignored — it's a computed read field.
        resp = self.client.post(
            "/api/gadgets/",
            {"name": "x", "qty": 2, "label": "hacked"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["label"], "x x2")
