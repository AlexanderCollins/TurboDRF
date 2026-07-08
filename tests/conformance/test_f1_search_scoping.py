"""
Demonstrates finding F1 (search JOIN-target leak) is closed at REQUEST time.

Setup: ``BankAccount`` (tenant-scoped) is made searchable by ``deal__title``.
``Deal`` carries an Owner predicate, so a non-bypass underwriter can see only
their own deal. Both bank accounts are in the same tenant, so the underwriter
can *list* both — but must not be able to infer the title of a deal they don't
own by searching the parent across the nested path.

Before the fix, ``?search=<other deal's title>`` JOINs Deal unscoped and
surfaces the linked bank account (leaking the title). After the fix,
``filter_queryset`` scopes the Deal JOIN to visible deals.

This test FAILS without the views.py ``filter_queryset`` scoping (verified by
stashing it) — i.e. it genuinely exercises the fix.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from tests.test_app.apps import _test_user_brokerages, set_test_brokerage
from tests.test_app.models import BankAccount, Brokerage, Deal

User = get_user_model()


class TestSearchTargetScopingF1(TestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        _test_user_brokerages.clear()
        self.client = APIClient()

        self.brok = Brokerage.objects.create(name="A")

        def mk(name, role):
            u = User.objects.create_user(username=name, password="x")
            u._test_roles = [role]
            set_test_brokerage(u, self.brok)
            return u

        self.under = mk("ua", "underwriter")       # non-bypass owner
        self.other = mk("ua2", "underwriter")
        self.manager = mk("mgr", "manager")        # owner-bypass

        # Same tenant, different owners; unique searchable titles.
        self.deal_mine = Deal.objects.create(
            title="ZEBRAALPHA", brokerage=self.brok, assigned_broker=self.under
        )
        self.deal_other = Deal.objects.create(
            title="QUOKKAOMEGA", brokerage=self.brok, assigned_broker=self.other
        )
        self.ba_mine = BankAccount.objects.create(name="mine", deal=self.deal_mine)
        self.ba_other = BankAccount.objects.create(name="other", deal=self.deal_other)

        # Make BankAccount searchable across a nested path into the
        # Owner-scoped Deal (startup gate already ran on the empty default).
        self._orig = getattr(BankAccount, "searchable_fields", None)
        BankAccount.searchable_fields = ["deal__title"]

    def tearDown(self):
        if self._orig is None:
            if "searchable_fields" in BankAccount.__dict__:
                del BankAccount.searchable_fields
        else:
            BankAccount.searchable_fields = self._orig

    def _ids(self, resp):
        return [r["id"] for r in resp.data["data"]]

    def test_underwriter_cannot_find_unowned_deal_via_search(self):
        # The leak: searching the OTHER (unowned) deal's title must not surface
        # its bank account — that would confirm the title the owner can't read.
        self.client.force_authenticate(user=self.under)
        resp = self.client.get("/api/bankaccounts/?search=QUOKKAOMEGA")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(self.ba_other.id, self._ids(resp))  # F1 closed

    def test_search_still_finds_own_deal(self):
        # Positive control: searching their OWN deal's title still works.
        self.client.force_authenticate(user=self.under)
        resp = self.client.get("/api/bankaccounts/?search=ZEBRAALPHA")
        self.assertIn(self.ba_mine.id, self._ids(resp))

    def test_bypass_role_can_find_it(self):
        # Non-vacuity: a manager (owner-bypass) sees all tenant deals, so the
        # same search DOES surface ba_other — proving the scoping is owner-aware,
        # not a blanket block on the path.
        self.client.force_authenticate(user=self.manager)
        resp = self.client.get("/api/bankaccounts/?search=QUOKKAOMEGA")
        self.assertIn(self.ba_other.id, self._ids(resp))
