"""
Verify that predicate-based row scoping works with the compiled read path
(.values() + F() annotations bypassing DRF serializers).

Predicates apply via get_queryset() which runs BEFORE the compiler's
.values() call — so scoping flows through naturally to both code paths.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

User = get_user_model()


class TestCompiledPathRespectsTenantPredicate(TestCase):
    def setUp(self):
        # Ensure URL conf is loaded so router runs and predicates register
        import tests.urls  # noqa: F401
        from tests.test_app.models import (
            BankAccount,
            Brokerage,
            Deal,
            Transaction,
        )

        self.client = APIClient()
        self.brokerage_a = Brokerage.objects.create(name="A")
        self.brokerage_b = Brokerage.objects.create(name="B")

        self.user_a = User.objects.create_user(username="ca", password="x")
        self.user_a._test_roles = ["manager"]
        self.user_a._test_brokerage = self.brokerage_a

        self.deal_a = Deal.objects.create(
            title="A's deal", brokerage=self.brokerage_a
        )
        self.deal_b = Deal.objects.create(
            title="B's deal", brokerage=self.brokerage_b
        )
        self.bank_a = BankAccount.objects.create(name="A's bank", deal=self.deal_a)
        self.bank_b = BankAccount.objects.create(name="B's bank", deal=self.deal_b)
        Transaction.objects.create(amount=Decimal("100"), bank_account=self.bank_a)
        Transaction.objects.create(amount=Decimal("999"), bank_account=self.bank_b)

    def test_tenant_filter_applied_through_compiled_queryset(self):
        """Transaction has tenant_field='bank_account__deal__brokerage' (a
        SETTING, not a predicate, in the two-layer design). The mandatory
        tenant filter is applied separately via get_queryset → flows through
        to the compiled .values() path naturally."""
        from tests.test_app.models import Transaction
        from turbodrf.predicates import get_tenant_field

        tenant_field = get_tenant_field(Transaction)
        self.assertEqual(tenant_field, "bank_account__deal__brokerage")

        self.client.force_authenticate(user=self.user_a)
        response = self.client.get("/api/transactions/")
        self.assertEqual(response.status_code, 200)
        amounts = sorted(t["amount"] for t in response.data["data"])
        self.assertEqual(amounts, ["100.00"])

    def test_tenant_q_helper_fails_closed_without_request(self):
        """Direct unit test of _get_tenant_q with no request → no_match Q.
        Tenant boundary is mandatory and fails closed when no caller."""
        from tests.test_app.models import Transaction
        from turbodrf.predicates import _no_match_q, get_tenant_field
        from turbodrf.views import TurboDRFViewSet

        tenant_field = get_tenant_field(Transaction)
        self.assertEqual(tenant_field, "bank_account__deal__brokerage")

        ViewSet = type(
            "TestViewSet",
            (TurboDRFViewSet,),
            {"model": Transaction, "_tenant_field": tenant_field, "_predicates": []},
        )
        viewset = ViewSet()
        q = viewset._get_tenant_q(None)
        self.assertEqual(q, _no_match_q())
