"""
Integration test for the chained-tenancy scenario from the design discussion:

    Brokerage → Deal → BankAccount → Transaction

The Transaction's tenant is reached via two FK hops:
    bank_account__deal__brokerage

The user can:
- Hit /transactions/ cold and see only their tenant's rows
- Hit /transactions/?bank_account=X — for any X, results filtered by predicate
- Hit /transactions/{id}/ — 404 for foreign-tenant rows
- POST to /transactions/ with bank_account FK from another tenant → 400
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from tests.test_app.models import BankAccount, Brokerage, Deal, Transaction

User = get_user_model()


class ChainedPredicateTestBase(TestCase):
    def setUp(self):
        self.client = APIClient()

        self.brokerage_a = Brokerage.objects.create(name="A")
        self.brokerage_b = Brokerage.objects.create(name="B")

        self.user_a = User.objects.create_user(username="a", password="x")
        self.user_a._test_roles = ["manager"]  # bypass owner — only tenant matters
        self.user_a._test_brokerage = self.brokerage_a

        self.deal_a = Deal.objects.create(
            title="A's deal", brokerage=self.brokerage_a, assigned_broker=self.user_a
        )
        self.deal_b = Deal.objects.create(
            title="B's deal", brokerage=self.brokerage_b, assigned_broker=None
        )
        self.bank_a = BankAccount.objects.create(name="A's bank", deal=self.deal_a)
        self.bank_b = BankAccount.objects.create(name="B's bank", deal=self.deal_b)

        self.tx_a1 = Transaction.objects.create(
            amount=Decimal("100.00"), bank_account=self.bank_a
        )
        self.tx_a2 = Transaction.objects.create(
            amount=Decimal("200.00"), bank_account=self.bank_a
        )
        self.tx_b1 = Transaction.objects.create(
            amount=Decimal("999.00"), bank_account=self.bank_b
        )

    def _login(self, user):
        self.client.force_authenticate(user=user)


class TestChainedListing(ChainedPredicateTestBase):
    def test_cold_list_filters_by_tenant_chain(self):
        # GET /transactions/ with no filter → only A's transactions
        self._login(self.user_a)
        response = self.client.get("/api/transactions/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = sorted(t["id"] for t in response.data["data"])
        expected = sorted([self.tx_a1.id, self.tx_a2.id])
        self.assertEqual(ids, expected)

    def test_filter_by_own_bank_account(self):
        self._login(self.user_a)
        response = self.client.get(f"/api/transactions/?bank_account={self.bank_a.id}")
        ids = sorted(t["id"] for t in response.data["data"])
        expected = sorted([self.tx_a1.id, self.tx_a2.id])
        self.assertEqual(ids, expected)

    def test_filter_by_foreign_bank_account_returns_empty(self):
        # User can ASK for B's bank but predicate filters → empty list, NOT 403
        self._login(self.user_a)
        response = self.client.get(f"/api/transactions/?bank_account={self.bank_b.id}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 0)


class TestChainedDetail(ChainedPredicateTestBase):
    def test_detail_for_own_transaction(self):
        self._login(self.user_a)
        response = self.client.get(f"/api/transactions/{self.tx_a1.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_detail_for_foreign_transaction_404(self):
        # No information leak — 404 not 403
        self._login(self.user_a)
        response = self.client.get(f"/api/transactions/{self.tx_b1.id}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class TestChainedWrites(ChainedPredicateTestBase):
    def test_create_transaction_on_foreign_bank_rejected(self):
        # Even though user has create permission, the FK target is invisible
        self._login(self.user_a)
        response = self.client.post(
            "/api/transactions/",
            {"amount": "5.00", "bank_account": self.bank_b.id},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # Confirm not created
        self.assertFalse(Transaction.objects.filter(amount=Decimal("5.00")).exists())

    def test_create_transaction_on_own_bank(self):
        self._login(self.user_a)
        response = self.client.post(
            "/api/transactions/",
            {"amount": "7.00", "bank_account": self.bank_a.id},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)


class TestChainedBankAccountListing(ChainedPredicateTestBase):
    def test_bankaccount_list_chained_one_hop(self):
        # BankAccount.tenant_field = 'deal__brokerage' (one-hop chain)
        self._login(self.user_a)
        response = self.client.get("/api/bankaccounts/")
        ids = sorted(b["id"] for b in response.data["data"])
        self.assertEqual(ids, [self.bank_a.id])

    def test_bankaccount_detail_foreign_404(self):
        self._login(self.user_a)
        response = self.client.get(f"/api/bankaccounts/{self.bank_b.id}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
