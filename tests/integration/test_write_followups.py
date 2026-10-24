"""Write-path follow-ups: a role that can create but not write a required
column gets a 400, and predicate-only hosts get the co-tenant check."""

from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from tests.test_app.apps import _test_user_brokerages, set_test_brokerage
from tests.test_app.models import Brokerage, RbacBoard, RelatedModel
from turbodrf.serializers import _check_fk_injection_writes

User = get_user_model()

MAKER = {
    "admin": [
        "test_app.samplemodel.read",
        "test_app.samplemodel.create",
        "test_app.samplemodel.price.read",
        "test_app.samplemodel.price.write",
    ],
    "maker": [
        "test_app.samplemodel.read",
        "test_app.samplemodel.create",
        "test_app.samplemodel.title.read",
        "test_app.samplemodel.title.write",
        "test_app.samplemodel.price.read",
        "test_app.samplemodel.related.read",
        "test_app.samplemodel.related.write",
    ],
}


class TestRequiredColumnTheRoleCantWrite(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="maker")
        self.user._test_roles = ["maker"]
        self.related = RelatedModel.objects.create(name="r")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    @override_settings(TURBODRF_ROLES=MAKER)
    def test_create_is_400_not_500(self):
        """price is required, and maker can create but has no price.write
        (admin has one, so price is write-restricted): the insert used to fail
        with an IntegrityError (500)."""
        resp = self.client.post(
            "/api/samplemodels/",
            {"title": "t", "price": "1.00", "related": self.related.pk},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("price", resp.json()["error"]["message"])


class TestCoTenantCheckOnPredicateOnlyHost(TestCase):
    def setUp(self):
        cache.clear()
        _test_user_brokerages.clear()
        self.a = Brokerage.objects.create(name="A")
        self.b = Brokerage.objects.create(name="B")
        self.user_a = User.objects.create_user(username="ua")
        self.user_b = User.objects.create_user(username="ub")
        set_test_brokerage(self.user_a, self.a)
        set_test_brokerage(self.user_b, self.b)

    def test_user_in_another_tenant_is_rejected(self):
        """RbacBoard has visibility only (no tenant_field); a write naming a
        user from another tenant used to skip the co-tenant check."""
        request = SimpleNamespace(user=self.user_a)
        errors = _check_fk_injection_writes(
            RbacBoard, {"owner": self.user_b}, request, None
        )
        self.assertIn("owner", errors)
        self.assertEqual(
            _check_fk_injection_writes(
                RbacBoard, {"owner": self.user_a}, request, None
            ),
            {},
        )
