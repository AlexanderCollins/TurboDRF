"""
Integration tests for FK injection prevention on writes.

When a user POSTs/PATCHes with a foreign key, that FK target must resolve to
a row visible to the user under the related model's predicate stack.

Covers:
- POST with cross-tenant FK → 400
- POST with own-tenant FK → 201
- PATCH attempting tenant reassignment → 400
- POST without tenant_field provided → auto-filled from request.user
- POST with explicit-but-correct tenant → 201
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from tests.test_app.apps import _test_user_brokerages, set_test_brokerage
from tests.test_app.models import BankAccount, Brokerage, Deal, Transaction

User = get_user_model()


class FKInjectionTestBase(TestCase):
    def setUp(self):
        _test_user_brokerages.clear()
        self.client = APIClient()

        self.brokerage_a = Brokerage.objects.create(name="A")
        self.brokerage_b = Brokerage.objects.create(name="B")

        self.under_a = User.objects.create_user(username="ua", password="x")
        self.under_a._test_roles = ["underwriter"]
        set_test_brokerage(self.under_a, self.brokerage_a)

        self.manager_a = User.objects.create_user(username="ma", password="x")
        self.manager_a._test_roles = ["manager"]
        set_test_brokerage(self.manager_a, self.brokerage_a)

        self.deal_a = Deal.objects.create(
            title="A's deal",
            brokerage=self.brokerage_a,
            assigned_broker=self.under_a,
        )
        self.deal_b = Deal.objects.create(
            title="B's deal",
            brokerage=self.brokerage_b,
            assigned_broker=None,
        )
        self.bank_a = BankAccount.objects.create(name="A's bank", deal=self.deal_a)
        self.bank_b = BankAccount.objects.create(name="B's bank", deal=self.deal_b)

    def _login(self, user):
        self.client.force_authenticate(user=user)


class TestCreateFKInjection(FKInjectionTestBase):
    def test_underwriter_a_cannot_create_transaction_on_b_bank(self):
        # Cross-tenant FK injection: try to create a Transaction whose
        # bank_account belongs to brokerage B.
        self._login(self.under_a)
        response = self.client.post(
            "/api/transactions/",
            {"amount": "100.00", "bank_account": self.bank_b.id},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # Confirm nothing was actually created
        self.assertFalse(Transaction.objects.filter(bank_account=self.bank_b).exists())

    def test_underwriter_a_can_create_transaction_on_own_bank(self):
        self._login(self.under_a)
        response = self.client.post(
            "/api/transactions/",
            {"amount": "100.00", "bank_account": self.bank_a.id},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_underwriter_a_cannot_create_bankaccount_on_b_deal(self):
        self._login(self.under_a)
        response = self.client.post(
            "/api/bankaccounts/",
            {"name": "Hijack", "deal": self.deal_b.id},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class TestTenantReassignmentRejected(FKInjectionTestBase):
    def test_underwriter_cannot_patch_deal_to_change_brokerage(self):
        # User tries to move their own deal to another brokerage. Tenant
        # validate_write should reject.
        self._login(self.under_a)
        response = self.client.patch(
            f"/api/deals/{self.deal_a.id}/",
            {"brokerage": self.brokerage_b.id},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # Confirm row not changed
        self.deal_a.refresh_from_db()
        self.assertEqual(self.deal_a.brokerage_id, self.brokerage_a.id)

    def test_manager_also_cannot_change_brokerage(self):
        # Bypass roles bypass *owner*, not tenant — this is mandatory MAC
        self._login(self.manager_a)
        response = self.client.patch(
            f"/api/deals/{self.deal_a.id}/",
            {"brokerage": self.brokerage_b.id},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class TestTenantAutoFill(FKInjectionTestBase):
    def test_create_deal_without_brokerage_auto_fills(self):
        # Manager has bypass — and is allowed to create deals
        self._login(self.manager_a)
        response = self.client.post(
            "/api/deals/",
            {"title": "Auto-tenant"},  # no brokerage provided
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        deal = Deal.objects.get(title="Auto-tenant")
        # Tenant predicate auto-filled brokerage from request.user.brokerage
        self.assertEqual(deal.brokerage_id, self.brokerage_a.id)

    def test_create_deal_with_correct_explicit_brokerage_succeeds(self):
        self._login(self.manager_a)
        response = self.client.post(
            "/api/deals/",
            {"title": "Explicit", "brokerage": self.brokerage_a.id},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)


class TestOwnerInjection(FKInjectionTestBase):
    def test_underwriter_cannot_assign_deal_to_other_user(self):
        # under_a tries to create a deal assigned to manager_a — Owner.validate_write
        # rejects because under_a doesn't have a bypass role.
        self._login(self.under_a)
        response = self.client.post(
            "/api/deals/",
            {
                "title": "Yours now",
                "brokerage": self.brokerage_a.id,
                "assigned_broker": self.manager_a.id,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_manager_can_assign_deal_to_other_user(self):
        self._login(self.manager_a)
        response = self.client.post(
            "/api/deals/",
            {
                "title": "Manager-assigned",
                "brokerage": self.brokerage_a.id,
                "assigned_broker": self.under_a.id,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
