"""
Integration tests for IDOR / BOLA prevention via predicate-based row scoping.

Each test creates two brokerages with their own users + deals and verifies that
cross-tenant access is impossible:
- LIST excludes other tenants' rows
- DETAIL/PATCH/DELETE return 404 (not 403) for foreign rows (no existence leak)
- Owner restriction within tenant works
- Bypass roles see all rows in tenant
- Multi-role merge: more roles = more access
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from tests.test_app.models import Brokerage, Deal

User = get_user_model()


class IDORTestBase(TestCase):
    """Two brokerages, one user each, deals on each side."""

    def setUp(self):
        from django.core.cache import cache

        from tests.test_app.apps import _test_user_brokerages

        cache.clear()
        _test_user_brokerages.clear()

        self.client = APIClient()

        # Two tenants
        self.brokerage_a = Brokerage.objects.create(name="Brokerage A")
        self.brokerage_b = Brokerage.objects.create(name="Brokerage B")

        # Underwriter at A
        self.under_a = User.objects.create_user(
            username="under_a", password="x", is_staff=False
        )
        self.under_a._test_roles = ["underwriter"]
        self.under_a._test_brokerage = self.brokerage_a

        # Another underwriter at A (to test owner scoping within tenant)
        self.under_a2 = User.objects.create_user(
            username="under_a2", password="x", is_staff=False
        )
        self.under_a2._test_roles = ["underwriter"]
        self.under_a2._test_brokerage = self.brokerage_a

        # Manager at A (bypass role)
        self.manager_a = User.objects.create_user(
            username="manager_a", password="x", is_staff=False
        )
        self.manager_a._test_roles = ["manager"]
        self.manager_a._test_brokerage = self.brokerage_a

        # Underwriter at B
        self.under_b = User.objects.create_user(
            username="under_b", password="x", is_staff=False
        )
        self.under_b._test_roles = ["underwriter"]
        self.under_b._test_brokerage = self.brokerage_b

        # Deals
        self.deal_a_under = Deal.objects.create(
            title="A's deal (under_a's)",
            brokerage=self.brokerage_a,
            assigned_broker=self.under_a,
        )
        self.deal_a_under2 = Deal.objects.create(
            title="A's deal (under_a2's)",
            brokerage=self.brokerage_a,
            assigned_broker=self.under_a2,
        )
        self.deal_b = Deal.objects.create(
            title="B's deal",
            brokerage=self.brokerage_b,
            assigned_broker=self.under_b,
        )

    def _login(self, user):
        # Force login because _test_roles/_test_brokerage are set on the
        # in-memory instance — re-fetching from DB would lose them.
        self.client.force_authenticate(user=user)


class TestTenantIsolationOnList(IDORTestBase):
    def test_underwriter_a_does_not_see_b_deals_in_list(self):
        self._login(self.under_a)
        response = self.client.get("/api/deals/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = [d["id"] for d in response.data["data"]]
        self.assertIn(self.deal_a_under.id, ids)
        self.assertNotIn(self.deal_b.id, ids)

    def test_manager_a_does_not_see_b_deals(self):
        # Manager is bypass for owner check, NOT for tenant — still scoped
        self._login(self.manager_a)
        response = self.client.get("/api/deals/")
        ids = [d["id"] for d in response.data["data"]]
        self.assertNotIn(self.deal_b.id, ids)

    def test_under_a2_only_sees_own_deal(self):
        # under_a2 is non-bypass, so within-tenant they see only their own
        self._login(self.under_a2)
        response = self.client.get("/api/deals/")
        ids = [d["id"] for d in response.data["data"]]
        self.assertEqual(ids, [self.deal_a_under2.id])

    def test_manager_sees_all_in_tenant(self):
        # Manager has bypass — sees all A's deals (but no B's)
        self._login(self.manager_a)
        response = self.client.get("/api/deals/")
        ids = sorted(d["id"] for d in response.data["data"])
        expected = sorted([self.deal_a_under.id, self.deal_a_under2.id])
        self.assertEqual(ids, expected)


class TestTenantIsolationOnDetail(IDORTestBase):
    def test_underwriter_a_gets_404_for_b_deal_detail(self):
        self._login(self.under_a)
        response = self.client.get(f"/api/deals/{self.deal_b.id}/")
        # 404, not 403 — no existence leak
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_underwriter_a_gets_404_for_other_underwriter_deal(self):
        # Within same tenant, owner restriction → 404
        self._login(self.under_a)
        response = self.client.get(f"/api/deals/{self.deal_a_under2.id}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_underwriter_a_can_get_own_deal(self):
        self._login(self.under_a)
        response = self.client.get(f"/api/deals/{self.deal_a_under.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_manager_a_can_get_any_a_deal(self):
        self._login(self.manager_a)
        response = self.client.get(f"/api/deals/{self.deal_a_under2.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_manager_a_gets_404_for_b_deal(self):
        self._login(self.manager_a)
        response = self.client.get(f"/api/deals/{self.deal_b.id}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class TestTenantIsolationOnPatch(IDORTestBase):
    def test_underwriter_a_cannot_patch_b_deal(self):
        self._login(self.under_a)
        response = self.client.patch(
            f"/api/deals/{self.deal_b.id}/",
            {"title": "Hacked"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        # Verify nothing was actually changed
        self.deal_b.refresh_from_db()
        self.assertEqual(self.deal_b.title, "B's deal")

    def test_underwriter_a_cannot_patch_other_underwriter_deal(self):
        self._login(self.under_a)
        response = self.client.patch(
            f"/api/deals/{self.deal_a_under2.id}/",
            {"title": "Mine now"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_underwriter_a_can_patch_own_deal(self):
        self._login(self.under_a)
        response = self.client.patch(
            f"/api/deals/{self.deal_a_under.id}/",
            {"title": "Updated by owner"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_manager_a_can_patch_other_a_deal(self):
        # Bypass role can patch other underwriters' deals within tenant
        self._login(self.manager_a)
        response = self.client.patch(
            f"/api/deals/{self.deal_a_under2.id}/",
            {"title": "Manager edit"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class TestTenantIsolationOnDelete(IDORTestBase):
    def test_underwriter_a_cannot_delete_b_deal(self):
        self._login(self.under_a)
        response = self.client.delete(f"/api/deals/{self.deal_b.id}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        # Confirm the row is still there
        self.assertTrue(Deal.objects.filter(pk=self.deal_b.pk).exists())

    def test_manager_a_cannot_delete_b_deal(self):
        self._login(self.manager_a)
        response = self.client.delete(f"/api/deals/{self.deal_b.id}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class TestMultiRoleMerge(IDORTestBase):
    def test_user_with_both_underwriter_and_manager_sees_all_in_tenant(self):
        # Underwriter + manager → manager bypass wins (more roles = more access)
        self.under_a._test_roles = ["underwriter", "manager"]
        self._login(self.under_a)
        response = self.client.get("/api/deals/")
        ids = sorted(d["id"] for d in response.data["data"])
        expected = sorted([self.deal_a_under.id, self.deal_a_under2.id])
        self.assertEqual(ids, expected)


class TestNoTenantAttribute(IDORTestBase):
    def test_user_without_brokerage_attr_sees_nothing(self):
        # User with no _test_brokerage → user.brokerage = None → fail-closed
        self.under_a._test_brokerage = None
        self._login(self.under_a)
        response = self.client.get("/api/deals/")
        self.assertEqual(len(response.data["data"]), 0)
