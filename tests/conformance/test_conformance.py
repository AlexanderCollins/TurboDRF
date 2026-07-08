"""
Deterministic conformance pass: run the independent monitor against the REAL
TurboDRF API across an exhaustive small scope of (caller, role, tenant-model,
pathway) combinations.

For every combination, the monitor (tests/conformance/monitor.py) independently
recomputes the authorized view and asserts the actual HTTP response conforms. A
ConformanceViolation = the real code returned something its declared policy
forbids.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from tests.test_app.apps import _test_user_brokerages, set_test_brokerage
from tests.test_app.models import (
    BankAccount,
    Brokerage,
    Deal,
    RelatedModel,
    SampleModel,
    Transaction,
)

from .monitor import ConformanceMonitor, ConformanceViolation

User = get_user_model()


class ConformanceBase(TestCase):
    """Two tenants, underwriter/manager callers on each side, and a row of each
    tenant-scoped model per tenant."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        _test_user_brokerages.clear()
        self.client = APIClient()
        self.monitor = ConformanceMonitor()

        self.brok_a = Brokerage.objects.create(name="A")
        self.brok_b = Brokerage.objects.create(name="B")

        def mkuser(name, role, brok):
            u = User.objects.create_user(username=name, password="x")
            u._test_roles = [role]
            set_test_brokerage(u, brok)
            return u

        self.under_a = mkuser("under_a", "underwriter", self.brok_a)
        self.under_a2 = mkuser("under_a2", "underwriter", self.brok_a)
        self.manager_a = mkuser("manager_a", "manager", self.brok_a)
        self.under_b = mkuser("under_b", "underwriter", self.brok_b)

        # Deals (owner-scoped within tenant)
        self.deal_a1 = Deal.objects.create(
            title="A1", brokerage=self.brok_a, assigned_broker=self.under_a
        )
        self.deal_a2 = Deal.objects.create(
            title="A2", brokerage=self.brok_a, assigned_broker=self.under_a2
        )
        self.deal_b = Deal.objects.create(
            title="B1", brokerage=self.brok_b, assigned_broker=self.under_b
        )

        # BankAccounts (tenant via deal__brokerage)
        self.ba_a = BankAccount.objects.create(name="ba_a", deal=self.deal_a1)
        self.ba_b = BankAccount.objects.create(name="ba_b", deal=self.deal_b)

        # Transactions (tenant via bank_account__deal__brokerage)
        self.tx_a = Transaction.objects.create(amount=Decimal("1"), bank_account=self.ba_a)
        self.tx_b = Transaction.objects.create(amount=Decimal("2"), bank_account=self.ba_b)

        # A field-permissioned, non-tenant model for field-exposure checks
        self.rel = RelatedModel.objects.create(name="rel", description="d")
        self.sample = SampleModel.objects.create(
            title="S", description="desc", price=Decimal("9.99"), quantity=3,
            related=self.rel, secret_field="TOPSECRET", is_active=True,
        )

    # tenant-scoped models: (Model, list-url, tenant_field)
    TENANT_MODELS = [
        (Deal, "/api/deals/", "brokerage"),
        (BankAccount, "/api/bankaccounts/", "deal__brokerage"),
        (Transaction, "/api/transactions/", "bank_account__deal__brokerage"),
    ]

    def _list_pks(self, url):
        resp = self.client.get(url)
        assert resp.status_code == status.HTTP_200_OK, (url, resp.status_code)
        return [row["id"] for row in resp.data["data"]], resp


class TestTenantContainment(ConformanceBase):
    def test_containment_all_callers_all_models(self):
        callers = [
            (self.under_a, self.brok_a),
            (self.under_a2, self.brok_a),
            (self.manager_a, self.brok_a),
            (self.under_b, self.brok_b),
        ]
        checks = 0
        rows_seen = 0
        for user, brok in callers:
            self.client.force_authenticate(user=user)
            for Model, url, tfield in self.TENANT_MODELS:
                with self.subTest(user=user.username, model=Model.__name__):
                    pks, _ = self._list_pks(url)
                    rows_seen += len(pks)
                    self.monitor.check_tenant_containment(
                        Model, pks, brok.pk, tfield,
                        context=f"{user.username} GET {url}",
                    )
                    checks += 1
        # non-vacuous: callers actually saw rows (not passing on empty lists)
        self.assertGreater(rows_seen, 0)
        print(f"\n[conformance] tenant-containment: {checks} (caller×model) "
              f"list checks, {rows_seen} rows verified, 0 violations")

    def test_filter_injection_cannot_cross_tenant(self):
        # ?brokerage=<other tenant> must not surface the other tenant's deal
        self.client.force_authenticate(user=self.under_a)
        resp = self.client.get(f"/api/deals/?brokerage={self.brok_b.pk}")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        pks = [r["id"] for r in resp.data["data"]]
        self.assertNotIn(self.deal_b.pk, pks)
        self.monitor.check_tenant_containment(
            Deal, pks, self.brok_a.pk, "brokerage", context="filter-injection"
        )

    def test_detail_of_foreign_row_is_404_not_403(self):
        # No existence leak across tenants
        self.client.force_authenticate(user=self.under_a)
        self.assertEqual(
            self.client.get(f"/api/deals/{self.deal_b.pk}/").status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.client.force_authenticate(user=self.under_b)
        self.assertEqual(
            self.client.get(f"/api/deals/{self.deal_a1.pk}/").status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_monitor_is_not_vacuous(self):
        # Sanity: the monitor DOES fire when handed a cross-tenant row, proving
        # the conformance checks above are meaningful (not passing trivially).
        with self.assertRaises(ConformanceViolation):
            self.monitor.check_tenant_containment(
                Deal, [self.deal_b.pk], self.brok_a.pk, "brokerage",
                context="deliberate-leak",
            )


class TestFieldExposure(ConformanceBase):
    def _viewer(self):
        u = User.objects.create_user(username="viewer1", password="x")
        u._test_roles = ["viewer"]
        return u

    def _admin(self):
        u = User.objects.create_user(username="admin1", password="x", is_superuser=True)
        u._test_roles = ["admin"]
        return u

    def test_viewer_never_sees_unreadable_fields(self):
        # viewer lacks samplemodel.price.read and samplemodel.secret_field.read
        self.client.force_authenticate(user=self._viewer())
        # list
        resp = self.client.get("/api/samplemodels/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        for row in resp.data["data"]:
            self.monitor.check_field_exposure(
                "test_app", "samplemodel", row.keys(), ["viewer"], context="list"
            )
            self.assertNotIn("price", row)
            self.assertNotIn("secret_field", row)
        # detail
        resp = self.client.get(f"/api/samplemodels/{self.sample.pk}/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.monitor.check_field_exposure(
            "test_app", "samplemodel", resp.data.keys(), ["viewer"], context="detail"
        )
        self.assertNotIn("secret_field", resp.data)
        self.assertNotIn("price", resp.data)

    def test_positive_control_admin_does_see_them(self):
        # Proves field gating is real and the oracle has the right polarity:
        # admin (full field perms) SEES price + secret_field.
        self.client.force_authenticate(user=self._admin())
        resp = self.client.get(f"/api/samplemodels/{self.sample.pk}/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn("price", resp.data)
        self.assertIn("secret_field", resp.data)
