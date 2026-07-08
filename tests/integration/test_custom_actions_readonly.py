"""Custom actions (`actions` config) and read-only resources (`read_only` config).

Both are auto-wired onto the router-generated viewset, so:
  - a `@turbodrf_action` handler is routed and gets get_object() scoping;
  - a `read_only: True` model serves reads (200) and rejects writes (405).
"""

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from tests.test_app.models import Widget


class TestCustomActionAndReadOnly(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.widget = Widget.objects.create(name="gizmo")

    def test_custom_action_is_routed_and_scoped(self):
        resp = self.client.get(f"/api/widgets/{self.widget.pk}/ping/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["pong"], "gizmo")

    def test_read_only_allows_reads(self):
        self.assertEqual(self.client.get("/api/widgets/").status_code, 200)
        self.assertEqual(
            self.client.get(f"/api/widgets/{self.widget.pk}/").status_code, 200
        )

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_read_only_blocks_writes(self):
        # With permissions disabled, the ONLY thing that can block a write is
        # the read_only config, so a disallowed method returns 405 (not a 403
        # from the permission layer).
        self.assertEqual(
            self.client.post(
                "/api/widgets/", {"name": "x"}, format="json"
            ).status_code,
            405,
        )
        self.assertEqual(
            self.client.delete(f"/api/widgets/{self.widget.pk}/").status_code,
            405,
        )
