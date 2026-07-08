"""
Property-based conformance fuzz: throw randomized callers, target models, and
filter/ordering/pagination query params at the REAL API and assert the
independent monitor's tenant-containment invariant never breaks.

Where test_conformance.py exhaustively covers a small fixed scope, this widens
coverage with Hypothesis-generated request shapes — the property under test is:

    for ALL callers, target models, and (even hostile/garbage) query params,
    a 200 response never contains a row outside the caller's tenant.
"""

from decimal import Decimal

import pytest

pytest.importorskip("hypothesis", reason="hypothesis not installed (dev/test extra)")

from django.contrib.auth import get_user_model  # noqa: E402
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from hypothesis.extra.django import TestCase as HypothesisTestCase  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from tests.test_app.apps import _test_user_brokerages, set_test_brokerage
from tests.test_app.models import (
    BankAccount,
    Brokerage,
    Deal,
    Transaction,
)

from .monitor import ConformanceMonitor

User = get_user_model()

EXTRAS = ["", "&ordering=id", "&ordering=-id", "&page_size=1", "&page_size=50"]


class TestTenantContainmentFuzz(HypothesisTestCase):
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

        self.callers = {
            "under_a": (mkuser("under_a", "underwriter", self.brok_a), self.brok_a),
            "under_a2": (mkuser("under_a2", "underwriter", self.brok_a), self.brok_a),
            "manager_a": (mkuser("manager_a", "manager", self.brok_a), self.brok_a),
            "under_b": (mkuser("under_b", "underwriter", self.brok_b), self.brok_b),
        }
        d_a = Deal.objects.create(title="A1", brokerage=self.brok_a,
                                  assigned_broker=self.callers["under_a"][0])
        Deal.objects.create(title="A2", brokerage=self.brok_a,
                            assigned_broker=self.callers["under_a2"][0])
        d_b = Deal.objects.create(title="B1", brokerage=self.brok_b,
                                  assigned_broker=self.callers["under_b"][0])
        ba_a = BankAccount.objects.create(name="ba_a", deal=d_a)
        ba_b = BankAccount.objects.create(name="ba_b", deal=d_b)
        Transaction.objects.create(amount=Decimal("1"), bank_account=ba_a)
        Transaction.objects.create(amount=Decimal("2"), bank_account=ba_b)

        self.models = [
            (Deal, "/api/deals/", "brokerage"),
            (BankAccount, "/api/bankaccounts/", "deal__brokerage"),
            (Transaction, "/api/transactions/", "bank_account__deal__brokerage"),
        ]

    @settings(max_examples=300, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        caller=st.sampled_from(["under_a", "under_a2", "manager_a", "under_b"]),
        midx=st.integers(min_value=0, max_value=2),
        brk=st.integers(min_value=0, max_value=12),   # random/foreign/garbage tenant id
        extra=st.sampled_from(EXTRAS),
    )
    def test_no_query_param_induces_cross_tenant_leak(self, caller, midx, brk, extra):
        user, brok = self.callers[caller]
        Model, url, tfield = self.models[midx]
        self.client.force_authenticate(user=user)
        # Inject a (possibly hostile) brokerage filter on Deal; ordering/paging on all
        q = f"?brokerage={brk}{extra}" if Model is Deal else (
            f"?{extra[1:]}" if extra else "")
        resp = self.client.get(url + q)
        if resp.status_code != 200:
            return  # 4xx on garbage params is fine; we only constrain 200s
        pks = [r["id"] for r in resp.data["data"]]
        self.monitor.check_tenant_containment(
            Model, pks, brok.pk, tfield, context=f"fuzz {caller} {url}{q}"
        )
