"""
Baseline security tests covering core defenses across the framework.

Adversarial scenarios over authentication, IDOR, FK injection, tenant
escalation, filter/ordering bypass, field exposure, owner-write injection,
permission cache, custom predicates, compiled-path safety, ORM gaps,
SQL injection, DoS resilience, concurrency snapshots, and auth-backend
integrations.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.models import Q
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from tests.test_app.apps import set_test_brokerage
from tests.test_app.models import (
    BankAccount,
    Brokerage,
    Deal,
    RelatedModel,
    SampleModel,
    Transaction,
)

User = get_user_model()


class SecurityTestBase(TestCase):
    """Two brokerages, multiple users with various roles, fixtures.

    Heavy fixture creation is hoisted to setUpTestData (runs once per
    TestCase class). Per-test setUp only resets transient state: cache,
    test-brokerage registry, and the authenticated client.
    """

    @classmethod
    def setUpTestData(cls):
        # Force URL discovery so predicates register
        import tests.urls  # noqa: F401

        cls.b_a = Brokerage.objects.create(name="Brokerage A")
        cls.b_b = Brokerage.objects.create(name="Brokerage B")

        # Non-bypass underwriter at A
        cls.u_a = User.objects.create_user(username="ua", password="x")
        # Another non-bypass underwriter at A (for within-tenant owner tests)
        cls.u_a2 = User.objects.create_user(username="ua2", password="x")
        # Manager (bypass) at A
        cls.m_a = User.objects.create_user(username="ma", password="x")
        # Non-bypass underwriter at B
        cls.u_b = User.objects.create_user(username="ub", password="x")
        # User with no roles (rejected at permission check)
        cls.no_role = User.objects.create_user(username="noroles", password="x")
        # User with no tenant — fail-closed verification
        cls.no_tenant = User.objects.create_user(username="notenant", password="x")
        # Viewer role
        cls.viewer = User.objects.create_user(username="viewer_u", password="x")

        # Deals
        cls.deal_a_owned = Deal.objects.create(
            title="A's deal (owned by ua)",
            brokerage=cls.b_a,
            assigned_broker=cls.u_a,
        )
        cls.deal_a_other = Deal.objects.create(
            title="A's deal (owned by ua2)",
            brokerage=cls.b_a,
            assigned_broker=cls.u_a2,
        )
        cls.deal_b = Deal.objects.create(
            title="B's deal",
            brokerage=cls.b_b,
            assigned_broker=cls.u_b,
        )

        # Bank accounts (chained tenancy)
        cls.bank_a = BankAccount.objects.create(name="A", deal=cls.deal_a_owned)
        cls.bank_b = BankAccount.objects.create(name="B", deal=cls.deal_b)

        # Transactions
        cls.tx_a = Transaction.objects.create(
            amount=Decimal("100.00"), bank_account=cls.bank_a
        )
        cls.tx_b = Transaction.objects.create(
            amount=Decimal("999.00"), bank_account=cls.bank_b
        )

    def setUp(self):
        from tests.test_app.apps import _test_user_brokerages

        cache.clear()
        _test_user_brokerages.clear()

        # Re-attach _test_roles & test brokerage on the cls user objects
        # (these attributes don't survive the savepoint/rollback cycle on
        # the user instance, so we restore them per test).
        self.u_a._test_roles = ["underwriter"]
        set_test_brokerage(self.u_a, self.b_a)
        self.u_a2._test_roles = ["underwriter"]
        set_test_brokerage(self.u_a2, self.b_a)
        self.m_a._test_roles = ["manager"]
        set_test_brokerage(self.m_a, self.b_a)
        self.u_b._test_roles = ["underwriter"]
        set_test_brokerage(self.u_b, self.b_b)
        self.no_role._test_roles = []
        set_test_brokerage(self.no_role, self.b_a)
        self.no_tenant._test_roles = ["underwriter"]
        # don't set brokerage — tests fail-closed behavior
        self.viewer._test_roles = ["viewer"]
        set_test_brokerage(self.viewer, self.b_a)

        self.client = APIClient()

    def _login(self, user):
        self.client.force_authenticate(user=user)

    def _logout(self):
        self.client.force_authenticate(user=None)


# ============================================================================
# Authentication bypass
# ============================================================================


class TestAuthenticationBypass(SecurityTestBase):
    def test_unauthenticated_cannot_list_deals(self):
        """Anonymous user gets 403 on tenant-scoped list (no public_access)."""
        self._logout()
        r = self.client.get("/api/deals/")
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_unauthenticated_cannot_create_deal(self):
        self._logout()
        r = self.client.post(
            "/api/deals/", {"title": "leak", "brokerage": self.b_a.id}, format="json"
        )
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_user_with_no_roles_denied(self):
        self._login(self.no_role)
        r = self.client.get("/api/deals/")
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)


# ============================================================================
# IDOR / BOLA — read & write across tenants
# ============================================================================


class TestIDOR(SecurityTestBase):
    def test_cross_tenant_list_excludes_other_tenant(self):
        self._login(self.u_a)
        r = self.client.get("/api/deals/")
        ids = [d["id"] for d in r.data["data"]]
        self.assertNotIn(self.deal_b.id, ids)

    def test_within_tenant_owner_scope_excludes_other_owners(self):
        self._login(self.u_a)
        r = self.client.get("/api/deals/")
        ids = [d["id"] for d in r.data["data"]]
        self.assertNotIn(self.deal_a_other.id, ids)

    def test_cross_tenant_detail_returns_404_not_403(self):
        """No existence leak — foreign tenant deal must return 404."""
        self._login(self.u_a)
        r = self.client.get(f"/api/deals/{self.deal_b.id}/")
        self.assertEqual(r.status_code, status.HTTP_404_NOT_FOUND)

    def test_cross_tenant_detail_via_chained_transaction(self):
        """User of brokerage A asks for transaction in brokerage B."""
        self._login(self.u_a)
        r = self.client.get(f"/api/transactions/{self.tx_b.id}/")
        self.assertEqual(r.status_code, status.HTTP_404_NOT_FOUND)

    def test_filter_does_not_bypass_predicate(self):
        """Even with ?bank_account=foreign_id, predicate filters."""
        self._login(self.u_a)
        r = self.client.get(f"/api/transactions/?bank_account={self.bank_b.id}")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(len(r.data["data"]), 0)

    def test_search_does_not_bypass_tenant(self):
        """?search= over searchable fields shouldn't reveal foreign rows."""
        self._login(self.u_a)
        r = self.client.get("/api/deals/?search=B")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        ids = [d["id"] for d in r.data["data"]]
        self.assertNotIn(self.deal_b.id, ids)

    def test_write_methods_to_foreign_tenant_blocked(self):
        """PATCH / PUT / DELETE on a foreign-tenant resource all return 404
        and leave the row unchanged (no existence leak, no mutation)."""
        self._login(self.u_a)
        original_title = self.deal_b.title

        r_patch = self.client.patch(
            f"/api/deals/{self.deal_b.id}/", {"title": "Hacked"}, format="json"
        )
        r_put = self.client.put(
            f"/api/deals/{self.deal_b.id}/",
            {"title": "Hacked", "brokerage": self.b_a.id},
            format="json",
        )
        r_delete = self.client.delete(f"/api/deals/{self.deal_b.id}/")

        for r in (r_patch, r_put, r_delete):
            self.assertEqual(r.status_code, status.HTTP_404_NOT_FOUND)

        self.deal_b.refresh_from_db()
        self.assertEqual(self.deal_b.title, original_title)
        self.assertTrue(Deal.objects.filter(pk=self.deal_b.pk).exists())


# ============================================================================
# FK injection / tenant escalation via body
# ============================================================================


class TestFKAndTenantInjection(SecurityTestBase):
    def test_create_transaction_with_foreign_bank_rejected(self):
        self._login(self.u_a)
        r = self.client.post(
            "/api/transactions/",
            {"amount": "1.00", "bank_account": self.bank_b.id},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Transaction.objects.filter(amount=Decimal("1.00")).exists())

    def test_create_bankaccount_with_foreign_deal_rejected(self):
        self._login(self.u_a)
        r = self.client.post(
            "/api/bankaccounts/",
            {"name": "hijack", "deal": self.deal_b.id},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_brokerage_reassignment_blocked_for_all_roles(self):
        """Bypass roles bypass owner, NOT tenant — tenant is mandatory MAC.
        Both an underwriter and a manager must be rejected when patching
        a deal's brokerage to a foreign tenant."""
        for actor in (self.u_a, self.m_a):
            self._login(actor)
            r = self.client.patch(
                f"/api/deals/{self.deal_a_owned.id}/",
                {"brokerage": self.b_b.id},
                format="json",
            )
            self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.deal_a_owned.refresh_from_db()
        self.assertEqual(self.deal_a_owned.brokerage_id, self.b_a.id)

    def test_create_deal_with_foreign_brokerage_in_body_rejected(self):
        """User submits brokerage=B in body. Should reject (validate_write)."""
        self._login(self.u_a)
        r = self.client.post(
            "/api/deals/",
            {"title": "leak", "brokerage": self.b_b.id},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Deal.objects.filter(title="leak").exists())

    def test_create_deal_omitting_brokerage_autofills_correct_one(self):
        """User omits brokerage — autofill puts THEIR brokerage, not attacker-controlled."""
        self._login(self.m_a)
        r = self.client.post("/api/deals/", {"title": "auto"}, format="json")
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        deal = Deal.objects.get(title="auto")
        self.assertEqual(deal.brokerage_id, self.b_a.id)


# ============================================================================
# Hidden-field exposure: filter / ordering / response / search bypass
# ============================================================================


class TestHiddenFieldExposure(SecurityTestBase):
    """Viewer has samplemodel.read but NOT samplemodel.secret_field.read.
    Every channel that could leak the hidden value (filter, icontains,
    ordering, response body) must be gated."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.related = RelatedModel.objects.create(name="r")
        cls.item_secret = SampleModel.objects.create(
            title="item",
            price=Decimal("10"),
            related=cls.related,
            secret_field="THE_SECRET_VALUE",
        )
        # Two items with secret_field that would sort differently
        cls.item_zzz = SampleModel.objects.create(
            title="A",
            price=Decimal("1"),
            related=cls.related,
            secret_field="zzz",
        )
        cls.item_aaa = SampleModel.objects.create(
            title="B",
            price=Decimal("2"),
            related=cls.related,
            secret_field="aaa",
        )

    def test_filter_on_secret_field_does_not_leak(self):
        """Equality and icontains filters on a non-readable field must return
        identical id-sets regardless of guess — otherwise the attacker can
        binary-search the hidden value."""
        self._login(self.viewer)
        cases = [
            ("secret_field=THE_SECRET_VALUE", "secret_field=WRONG_GUESS"),
            ("secret_field__icontains=THE", "secret_field__icontains=ZZZ"),
        ]
        for match_q, nomatch_q in cases:
            r_match = self.client.get(f"/api/samplemodels/?{match_q}")
            r_nomatch = self.client.get(f"/api/samplemodels/?{nomatch_q}")
            self.assertEqual(r_match.status_code, status.HTTP_200_OK)
            self.assertEqual(r_nomatch.status_code, status.HTTP_200_OK)
            ids_match = [d.get("id") for d in r_match.data["data"]]
            ids_nomatch = [d.get("id") for d in r_nomatch.data["data"]]
            self.assertEqual(
                ids_match,
                ids_nomatch,
                f"VULNERABILITY: filter {match_q!r} vs {nomatch_q!r} differ — "
                "hidden field value leaks via filter.",
            )

    def test_ordering_by_hidden_field_does_not_leak_order(self):
        """?ordering=secret_field would reveal the hidden value's order."""
        self._login(self.viewer)
        r_asc = self.client.get("/api/samplemodels/?ordering=secret_field")
        r_desc = self.client.get("/api/samplemodels/?ordering=-secret_field")
        self.assertEqual(r_asc.status_code, status.HTTP_200_OK)
        self.assertEqual(r_desc.status_code, status.HTTP_200_OK)
        ids_asc = [d.get("id") for d in r_asc.data["data"]]
        ids_desc = [d.get("id") for d in r_desc.data["data"]]
        if len(ids_asc) > 1 and ids_asc == ids_desc[::-1]:
            self.fail(
                f"VULNERABILITY: ordering=secret_field leaks order info. "
                f"asc={ids_asc} desc={ids_desc}."
            )

    def test_secret_field_not_in_response_for_viewer(self):
        self._login(self.viewer)
        r = self.client.get(f"/api/samplemodels/{self.item_secret.id}/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertNotIn(
            "secret_field",
            r.data,
            f"VULNERABILITY: secret_field exposed in response: {r.data}",
        )

    def test_search_does_not_match_against_hidden_field(self):
        """If a developer accidentally lists secret_field in searchable_fields,
        viewers without read permission on that field must NOT be able to
        substring-search by it."""
        original = list(SampleModel.searchable_fields)
        SampleModel.searchable_fields = ["title", "description", "secret_field"]
        try:
            self._login(self.viewer)
            r_match = self.client.get("/api/samplemodels/?search=THE_SECRET_VALUE")
            r_nomatch = self.client.get("/api/samplemodels/?search=ZZZNOMATCH")
            ids_match = [d.get("id") for d in r_match.data["data"]]
            ids_nomatch = [d.get("id") for d in r_nomatch.data["data"]]
            self.assertEqual(
                ids_match,
                ids_nomatch,
                "VULNERABILITY: ?search= matches against secret_field for viewer.",
            )
        finally:
            SampleModel.searchable_fields = original


# ============================================================================
# OPTIONS metadata gating
# ============================================================================


class TestOPTIONSDisclosure(SecurityTestBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.related_o = RelatedModel.objects.create(name="ro")
        cls.item_o = SampleModel.objects.create(
            title="t",
            price=Decimal("1"),
            related=cls.related_o,
            secret_field="s",
        )

    def test_options_for_underwriter_does_not_500(self):
        self._login(self.u_a)
        r = self.client.options("/api/deals/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)

    def test_options_metadata_excludes_unreadable_fields(self):
        """OPTIONS metadata is gated by per-field read permission. Viewer
        lacks price.read — `price` must not appear in the field metadata."""
        self._login(self.viewer)
        r = self.client.options(f"/api/samplemodels/{self.item_o.id}/")
        self.assertEqual(r.status_code, 200)
        fields_meta = r.data.get("model", {}).get("fields", {})
        if "price" in fields_meta:
            self.fail(
                f"REGRESSION: OPTIONS metadata still leaks price field for "
                f"viewer without price.read. Got: {fields_meta['price']}"
            )


# ============================================================================
# Null tenant / edge-case PKs / invalid-input resilience
# ============================================================================


class TestNullAndEdgeCases(SecurityTestBase):
    def test_user_with_no_tenant_sees_empty_list(self):
        self._login(self.no_tenant)
        r = self.client.get("/api/deals/")
        # Should fail closed — see no rows
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(len(r.data["data"]), 0)

    def test_user_with_no_tenant_cannot_create(self):
        self._login(self.no_tenant)
        r = self.client.post("/api/deals/", {"title": "x"}, format="json")
        # Auto-fill can't fill (no tenant) — model save fails
        self.assertNotEqual(r.status_code, status.HTTP_201_CREATED)

    def test_pathological_pks_and_filters_do_not_500(self):
        """Negative, huge, and non-int PKs in path; non-int filter values;
        regex-like search metachars — none may produce a 500."""
        self._login(self.u_a)
        endpoints = [
            "/api/deals/-1/",
            "/api/deals/99999999999/",
            "/api/deals/" + ("9" * 200) + "/",
            "/api/deals/?id=not-an-int",
        ]
        for url in endpoints:
            r = self.client.get(url)
            self.assertNotEqual(r.status_code, 500, f"{url} returned 500")
            # 404 / 400 are both acceptable
            self.assertIn(
                r.status_code,
                [
                    status.HTTP_404_NOT_FOUND,
                    status.HTTP_400_BAD_REQUEST,
                    status.HTTP_200_OK,
                ],
            )

        for payload in ["%", "_", "(?i:.*)", "((((((((a))))))))"]:
            r = self.client.get(f"/api/deals/?search={payload}")
            self.assertNotEqual(r.status_code, 500, f"search={payload!r} → 500")


# ============================================================================
# Owner write injection
# ============================================================================


class TestOwnerWriteInjection(SecurityTestBase):
    def test_underwriter_cannot_assign_deal_to_other_user(self):
        """Without bypass role, can't reassign owner FK."""
        self._login(self.u_a)
        r = self.client.post(
            "/api/deals/",
            {
                "title": "yours-now",
                "brokerage": self.b_a.id,
                "assigned_broker": self.u_a2.id,
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_underwriter_cannot_steal_owner_via_patch(self):
        """PATCH to set assigned_broker to themselves on a deal they don't
        own — get_object filters by owner, so 404."""
        self._login(self.u_a)
        r = self.client.patch(
            f"/api/deals/{self.deal_a_other.id}/",
            {"assigned_broker": self.u_a.id},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_404_NOT_FOUND)

    def test_manager_can_reassign_within_tenant(self):
        self._login(self.m_a)
        r = self.client.patch(
            f"/api/deals/{self.deal_a_other.id}/",
            {"assigned_broker": self.u_a.id},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)

    def test_manager_cannot_assign_to_user_in_different_tenant(self):
        """Cross-tenant FK assignment is rejected by the co-tenant check."""
        self._login(self.m_a)
        r = self.client.patch(
            f"/api/deals/{self.deal_a_owned.id}/",
            {"assigned_broker": self.u_b.id},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.deal_a_owned.refresh_from_db()
        self.assertEqual(self.deal_a_owned.assigned_broker_id, self.u_a.id)


# ============================================================================
# Swagger role parameter
# ============================================================================


class TestSwaggerRoleBypass(SecurityTestBase):
    def test_swagger_role_param_validates_against_user_roles(self):
        """?role=admin from a non-admin user must not elevate the schema to
        admin view; falls back to user's actual roles."""
        from drf_yasg import openapi
        from rest_framework.test import APIRequestFactory

        from turbodrf.swagger import RoleBasedSchemaGenerator

        factory = APIRequestFactory()
        request = factory.get("/api/swagger/?role=admin")
        request.user = self.u_a  # underwriter, NOT admin
        from django.contrib.sessions.middleware import SessionMiddleware

        SessionMiddleware(lambda r: None).process_request(request)

        info = openapi.Info(title="t", default_version="v1")
        gen = RoleBasedSchemaGenerator(info=info)
        try:
            gen.get_schema(request, public=False)
        except Exception:
            pass  # We only inspect what current_role got set to
        self.assertNotEqual(
            gen.current_role,
            "admin",
            "VULNERABILITY: ?role=admin still grants admin schema to non-admin",
        )


# ============================================================================
# Snapshot caching / role-change visibility
# ============================================================================


class TestSnapshotAndCacheBehavior(SecurityTestBase):
    """Per-request snapshot caching is the basis of TOCTOU concerns. Each
    request must build its own snapshot or hit cache; revoking a role
    mid-request shouldn't affect the in-flight check."""

    def test_snapshot_attached_per_request(self):
        from rest_framework.test import APIRequestFactory

        from turbodrf.backends import (
            attach_snapshot_to_request,
            get_snapshot_from_request,
        )

        factory = APIRequestFactory()
        req = factory.get("/api/deals/")
        req.user = self.u_a

        # Before attach: no snapshot
        self.assertIsNone(get_snapshot_from_request(req, Deal))
        # After attach: cached on request
        snap = attach_snapshot_to_request(req, Deal)
        self.assertIsNotNone(snap)
        self.assertIs(snap, get_snapshot_from_request(req, Deal))
        # Re-attach returns same instance (no rebuild)
        snap2 = attach_snapshot_to_request(req, Deal)
        self.assertIs(snap, snap2)

    def test_role_promotion_visible_after_cache_clear(self):
        """Underwriter sees own deal only; after promotion to manager and
        cache clear, sees all deals in tenant."""
        self._login(self.u_a)
        r1 = self.client.get("/api/deals/")
        ids1 = [d["id"] for d in r1.data["data"]]
        self.assertEqual(ids1, [self.deal_a_owned.id])

        # Promote
        self.u_a._test_roles = ["manager"]
        cache.clear()
        r2 = self.client.get("/api/deals/")
        ids2 = sorted(d["id"] for d in r2.data["data"])
        self.assertEqual(
            ids2,
            sorted([self.deal_a_owned.id, self.deal_a_other.id]),
            "Role promotion should take effect after cache clear",
        )


# ============================================================================
# Custom predicate risks
# ============================================================================


class TestCustomPredicateRisks(TestCase):
    """Custom predicates operate within tenant — the mandatory tenant
    boundary is enforced separately at the viewset level."""

    def test_custom_fails_closed_when_no_request(self):
        """No request → no_match (typical for schema gen / programmatic use)."""
        from turbodrf.predicates import Custom

        c = Custom(q_func=lambda r, u: Q())
        self.assertEqual(c.q(request=None, user_roles=set()), Q(pk__in=[]))

    def test_custom_q_returned_verbatim_with_request(self):
        """With a real request, q_func's return value is used directly."""
        from unittest.mock import Mock

        from turbodrf.predicates import Custom

        c = Custom(q_func=lambda r, u: Q(public=True))
        req = Mock()
        req.user = Mock()
        result = c.q(request=req, user_roles=set())
        self.assertEqual(result, Q(public=True))

    def test_custom_returning_empty_q_optionally_warns(self):
        """Custom returning Q() means 'no within-tenant restriction'. Tenant
        boundary still applies separately. With
        TURBODRF_LOG_UNRESTRICTED_CUSTOM=True a warning fires."""
        from rest_framework.test import APIRequestFactory

        from turbodrf.predicates import Custom

        c = Custom(q_func=lambda r, u: Q())
        factory = APIRequestFactory()
        req = factory.get("/")
        req.user = User.objects.create_user(username="vc", password="x")

        self.assertEqual(c.q(req, set()), Q())

        with override_settings(TURBODRF_LOG_UNRESTRICTED_CUSTOM=True):
            with self.assertLogs("turbodrf.predicates", level="WARNING") as cm:
                c.q(req, set())
            self.assertTrue(
                any("empty Q" in msg for msg in cm.output),
                "Warning should fire when TURBODRF_LOG_UNRESTRICTED_CUSTOM=True",
            )


# ============================================================================
# Compiled-path safety
# ============================================================================


class TestCompiledPathBypass(SecurityTestBase):
    def test_compiled_path_respects_tenant_setting(self):
        """The compiled .values() path consumes get_queryset(), which AND's
        the tenant_field setting."""
        from turbodrf.predicates import get_tenant_field

        self.assertEqual(
            get_tenant_field(Transaction),
            "bank_account__deal__brokerage",
        )


# ============================================================================
# Bulk / raw ORM gap
# ============================================================================


class TestBulkAndRaw(SecurityTestBase):
    def test_orm_objects_all_bypasses_scoping_known_gap(self):
        """Direct ORM access (not through ViewSet.get_queryset) bypasses
        scoping. Documented behavior — custom @actions or signal handlers
        must apply predicates manually."""
        all_deals = list(Deal.objects.all())
        self.assertGreaterEqual(len(all_deals), 3)


# ============================================================================
# Shared-model abuse (public_access)
# ============================================================================


class TestSharedModelAbuse(SecurityTestBase):
    def test_shared_model_visible_to_anonymous_via_public_access(self):
        """SampleModel has public_access=True. Anonymous reads allowed."""
        self._logout()
        r = self.client.get("/api/samplemodels/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)


# ============================================================================
# SQL injection / DoS resilience
# ============================================================================


class TestSQLInjectionAndDOS(SecurityTestBase):
    """Verify Django ORM properly parameterizes filter values, even when
    attacker submits SQL fragments. Plus DoS resilience to giant inputs."""

    SQLI_PAYLOADS = [
        "'; DROP TABLE test_app_deal; --",
        "' OR 1=1 --",
        "'; SELECT * FROM auth_user; --",
        "1; DELETE FROM test_app_deal WHERE 1=1; --",
        "' UNION SELECT username, password FROM auth_user --",
        "%27%20OR%20%271%27%3D%271",  # URL-encoded
    ]

    def test_sqli_payloads_in_filter_and_path_safe(self):
        """SQLi payloads in either a filter value or a path id must not 500.
        Filter values: 200 (literal match) or 400 (rejected). Path ids: 404."""
        self._login(self.u_a)
        for payload in self.SQLI_PAYLOADS:
            r_filter = self.client.get(f"/api/deals/?title={payload}")
            self.assertIn(
                r_filter.status_code,
                [status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST],
                f"Filter payload {payload!r} → {r_filter.status_code}",
            )
            r_path = self.client.get(f"/api/deals/{payload}/")
            self.assertNotEqual(r_path.status_code, 500)
        # Confirm the table still exists with data
        self.assertTrue(Deal.objects.exists())

    def test_dos_inputs_do_not_crash(self):
        """Deeply nested filter path, very large filter-param count, and a
        10k-char search term must all be handled without a 500."""
        self._login(self.u_a)
        deep = "__".join(["bank_account"] * 10) + "__deal__brokerage__name"
        r = self.client.get(f"/api/transactions/?{deep}=foo")
        self.assertNotEqual(r.status_code, 500)

        params = "&".join(f"f{i}=v{i}" for i in range(500))
        r = self.client.get(f"/api/deals/?{params}")
        self.assertNotEqual(r.status_code, 500)

        r = self.client.get(f"/api/deals/?search={'A' * 10000}")
        self.assertNotEqual(r.status_code, 500)


# ============================================================================
# Auth backend integrations — strict role mapping
# ============================================================================


class TestAuthBackendIntegrations(TestCase):
    """Unit-level verification of integration logic. Real-server testing is
    out of scope without running Keycloak."""

    def test_keycloak_unmapped_role_rejected_in_strict_mode(self):
        """With TURBODRF_KEYCLOAK_STRICT_ROLES=True and a mapping configured,
        unmapped Keycloak roles are dropped — they no longer pass through."""
        from turbodrf.integrations.keycloak import map_keycloak_roles_to_turbodrf

        with override_settings(
            TURBODRF_KEYCLOAK_ROLE_MAPPING={"realm-admin": "admin"},
            TURBODRF_KEYCLOAK_STRICT_ROLES=True,
        ):
            mapped = map_keycloak_roles_to_turbodrf(["realm-admin", "admin"])
            self.assertIn("admin", mapped)
            # 'admin' as a raw Keycloak role is REJECTED — only the explicitly
            # mapped 'realm-admin' resolves to 'admin'. Total = 1.
            self.assertEqual(
                mapped.count("admin"),
                1,
                "Unmapped 'admin' should be dropped in strict mode",
            )

    def test_keycloak_legacy_passthrough_when_strict_disabled(self):
        """TURBODRF_KEYCLOAK_STRICT_ROLES=False restores legacy passthrough."""
        from turbodrf.integrations.keycloak import map_keycloak_roles_to_turbodrf

        with override_settings(
            TURBODRF_KEYCLOAK_ROLE_MAPPING={"realm-admin": "admin"},
            TURBODRF_KEYCLOAK_STRICT_ROLES=False,
        ):
            mapped = map_keycloak_roles_to_turbodrf(["realm-admin", "admin"])
            self.assertEqual(mapped.count("admin"), 2)

    def test_keycloak_no_mapping_passes_through(self):
        """When no mapping is configured, Keycloak roles pass through."""
        from turbodrf.integrations.keycloak import map_keycloak_roles_to_turbodrf

        with override_settings(TURBODRF_KEYCLOAK_ROLE_MAPPING={}):
            mapped = map_keycloak_roles_to_turbodrf(["admin", "viewer"])
            self.assertEqual(mapped, ["admin", "viewer"])


# ============================================================================
# FK error message unification
# ============================================================================


class TestFKErrorUnification(TestCase):
    """FK error messages should NOT distinguish 'doesn't exist anywhere' from
    'exists but not in your tenant'. Also, type-mismatched PKs (e.g. string
    where int expected) must 400 cleanly without a 500."""

    def setUp(self):
        import tests.urls  # noqa: F401

        cache.clear()
        self.client = APIClient()
        self.b_a = Brokerage.objects.create(name="A")
        self.b_b = Brokerage.objects.create(name="B")

        self.user_a = User.objects.create_user(username="ua_fk", password="x")
        self.user_a._test_roles = ["underwriter"]
        set_test_brokerage(self.user_a, self.b_a)

        self.deal_b = Deal.objects.create(title="B", brokerage=self.b_b)
        self.bank_b = BankAccount.objects.create(name="B", deal=self.deal_b)

    def tearDown(self):
        cache.clear()

    def test_foreign_tenant_pk_and_nonexistent_pk_produce_same_message(self):
        """Cross-tenant PK (exists but not visible) must produce identical
        error text as a truly nonexistent PK."""
        self.client.force_authenticate(user=self.user_a)

        r_cross = self.client.post(
            "/api/transactions/",
            {"amount": "1.00", "bank_account": self.bank_b.id},
            format="json",
        )
        r_none = self.client.post(
            "/api/transactions/",
            {"amount": "1.00", "bank_account": 99999999},
            format="json",
        )
        self.assertEqual(r_cross.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(r_none.status_code, status.HTTP_400_BAD_REQUEST)

        msg_cross = r_cross.data["error"]["message"]["bank_account"]
        msg_none = r_none.data["error"]["message"]["bank_account"]
        self.assertEqual(
            msg_cross,
            msg_none,
            f"Cross-tenant and nonexistent PK should produce identical "
            f"messages. Got cross={msg_cross!r}, none={msg_none!r}",
        )

    def test_invalid_type_pk_does_not_500(self):
        """Non-integer string for an int FK must 400 cleanly (no 500/Traceback)."""
        self.client.force_authenticate(user=self.user_a)
        r = self.client.post(
            "/api/transactions/",
            {"amount": "1.00", "bank_account": "not-an-int"},
            format="json",
        )
        self.assertEqual(r.status_code, 400)
        msg = str(r.data)
        self.assertNotIn("Server Error", msg)
        self.assertNotIn("Traceback", msg)


# ============================================================================
# Sensitive deny-list at every __ segment
# ============================================================================


class TestSensitiveDenyListNested(TestCase):
    """Sensitive deny-list applies at every segment of a __ path."""

    def test_nested_password_or_token_in_path_is_blocked(self):
        from turbodrf.validation import is_field_path_sensitive

        for path in (
            "password",
            "related__password",
            "a__b__token",
            "x__api_key",
        ):
            self.assertTrue(
                is_field_path_sensitive(path),
                f"REGRESSION: path {path!r} not flagged as sensitive.",
            )
        for path in ("title", "related__name", "author__email"):
            self.assertFalse(is_field_path_sensitive(path))


# ============================================================================
# parse_config: tenant_field is a setting, not a predicate
# ============================================================================


class TestParseConfigInvariants(TestCase):
    """Tenant is a setting and cannot be OR-composed away by an
    Either(Owner, Tenant) trick — Tenant() is rejected inside Either at
    config time. Also: parse_config refuses to mix sugar (tenant_field=)
    with power-form (visibility=) keys."""

    def test_tenant_inside_either_raises_at_config_time(self):
        from django.core.exceptions import ImproperlyConfigured

        from turbodrf.predicates import Either, Owner, Tenant, parse_config

        bad_config = {"visibility": [Either(Owner("u", bypass=["admin"]), Tenant("t"))]}
        with self.assertRaises(ImproperlyConfigured) as ctx:
            parse_config(bad_config)
        self.assertIn("Either", str(ctx.exception))

    def test_mixing_owner_sugar_and_visibility_raises(self):
        """`owner_field` / `bypass_owner_roles` sugar conflicts with
        `visibility` (both compile to predicates) — must raise. But
        `tenant_field` is a setting, not a predicate, and is allowed
        alongside `visibility`."""
        from django.core.exceptions import ImproperlyConfigured

        from turbodrf.predicates import Owner, parse_config

        with self.assertRaises(ImproperlyConfigured) as cm:
            parse_config(
                {
                    "owner_field": "assigned_broker",
                    "visibility": [Owner("assigned_broker")],
                }
            )
        self.assertIn("Cannot mix", str(cm.exception))

        # tenant_field + visibility is the canonical power-form pairing —
        # the deprecated `Tenant() inside visibility` form told users to
        # use this combination. Should NOT raise.
        tf, preds = parse_config(
            {
                "tenant_field": "brokerage",
                "visibility": [Owner("assigned_broker")],
            }
        )
        self.assertEqual(tf, "brokerage")
        self.assertEqual(len(preds), 1)

    def test_tenant_layer_applied_separately_from_predicate_q(self):
        """Even if every predicate's Q resolves to Q() (unrestricted), the
        tenant layer still filters."""
        from rest_framework.test import APIRequestFactory

        from turbodrf.views import TurboDRFViewSet

        u = User.objects.create_user(username="vt2", password="x")
        u._test_roles = ["manager"]
        b = Brokerage.objects.create(name="B")
        set_test_brokerage(u, b)

        viewset = type(
            "VS",
            (TurboDRFViewSet,),
            {"model": Deal, "_tenant_field": "brokerage", "_predicates": []},
        )()
        factory = APIRequestFactory()
        req = factory.get("/")
        req.user = u
        viewset.request = req

        tenant_q = viewset._get_tenant_q(req)
        if tenant_q == Q():
            self.fail(
                "REGRESSION: _get_tenant_q returned unrestricted Q() for "
                "authenticated user with tenant. Tenant layer is missing."
            )


# ============================================================================
# Compiled path: nested FK perm gating, fields= bypass, anon guest, M2M sigs
# ============================================================================


class TestCompiledRegression(TestCase):
    """Nested FK annotations on the compiled path must be filtered by the
    FULL nested path (not just the base FK name); the ?fields= param must
    not bypass the FK gate; anon users with a configured 'guest' role must
    receive a snapshot; M2M filtering must support per-nested-field perms."""

    @override_settings(
        TURBODRF_ROLES={
            "limited_viewer_v3": [
                "test_app.compiledarticle.read",
                "test_app.compiledarticle.title.read",
                "test_app.compiledarticle.author.read",
                "test_app.relatedmodel.read",
                "test_app.relatedmodel.name.read",
            ],
            "_marker_v3": ["test_app.relatedmodel.description.read"],
        }
    )
    def test_compiled_path_strips_nested_fk_lacking_target_perm(self):
        from rest_framework.test import APIRequestFactory

        from tests.test_app.models import CompiledArticle
        from turbodrf.compiler import get_compiled_plan
        from turbodrf.views import TurboDRFViewSet

        u = User.objects.create_user(username="lv_v3", password="x")
        u._test_roles = ["limited_viewer_v3"]

        plan = get_compiled_plan(CompiledArticle)
        if plan is None:
            self.skipTest("compiled plan not registered for CompiledArticle")

        viewset = type(
            "VS",
            (TurboDRFViewSet,),
            {"model": CompiledArticle, "_predicates": [], "_tenant_field": None},
        )()
        factory = APIRequestFactory()
        req = factory.get("/")
        req.user = u
        viewset.request = req

        allowed_fk = viewset._filter_compiled_fk_annotations(plan, req)
        if allowed_fk is None:
            self.skipTest("no permission system applies — legacy path")

        if "author_description" in plan.fk_annotations:
            self.assertNotIn(
                "author_description",
                allowed_fk,
                "REGRESSION: compiled path includes author_description for "
                "user lacking relatedmodel.description.read.",
            )
        if "author_name" in plan.fk_annotations:
            self.assertIn("author_name", allowed_fk)

    def test_fields_param_intersects_with_fk_gate(self):
        """?fields=author_email submitted by a user without target perm must
        NOT bypass the FK annotation gate."""
        from rest_framework.test import APIRequestFactory

        from tests.test_app.models import CompiledArticle
        from turbodrf.compiler import get_compiled_plan
        from turbodrf.views import TurboDRFViewSet

        plan = get_compiled_plan(CompiledArticle)
        if plan is None:
            self.skipTest("no compiled plan")

        with override_settings(
            TURBODRF_ROLES={
                "lv_compiled": [
                    "test_app.compiledarticle.read",
                    "test_app.compiledarticle.title.read",
                    "test_app.compiledarticle.author.read",
                    "test_app.relatedmodel.read",
                    "test_app.relatedmodel.name.read",
                ],
                "_marker_compiled": [
                    "test_app.relatedmodel.description.read",
                ],
            }
        ):
            cache.clear()
            u = User.objects.create_user(username="lvc", password="x")
            u._test_roles = ["lv_compiled"]

            viewset = type(
                "VS",
                (TurboDRFViewSet,),
                {
                    "model": CompiledArticle,
                    "_predicates": [],
                    "_tenant_field": None,
                },
            )()
            factory = APIRequestFactory()
            req = factory.get("/?fields=author_description")
            req.user = u
            req.query_params = req.GET
            viewset.request = req

            allowed = viewset._filter_compiled_fk_annotations(plan, req)
            if allowed is not None and "author_description" in plan.fk_annotations:
                self.assertNotIn(
                    "author_description",
                    allowed,
                    "VULNERABILITY: ?fields=author_description bypasses FK gate.",
                )

    def test_anon_with_guest_role_gets_snapshot(self):
        """Anon users get a snapshot if a 'guest' role is configured."""
        from django.contrib.auth.models import AnonymousUser
        from rest_framework.test import APIRequestFactory

        from tests.test_app.models import CompiledSampleModel
        from turbodrf.views import TurboDRFViewSet

        with override_settings(
            TURBODRF_ROLES={
                "guest": [
                    "test_app.compiledsamplemodel.read",
                    "test_app.compiledsamplemodel.title.read",
                ],
                "_admin_marker": [
                    "test_app.compiledsamplemodel.price.read",
                ],
            }
        ):
            cache.clear()
            viewset = type(
                "VS",
                (TurboDRFViewSet,),
                {
                    "model": CompiledSampleModel,
                    "_predicates": [],
                    "_tenant_field": None,
                },
            )()
            factory = APIRequestFactory()
            req = factory.get("/")
            req.user = AnonymousUser()
            viewset.request = req

            readable = viewset._get_compiled_readable_fields(req)
            if readable is None:
                self.fail("REGRESSION: anon user with guest role still returns None.")
            self.assertIn("title", readable)
            self.assertNotIn(
                "price",
                readable,
                "REGRESSION: anon-guest snapshot includes 'price' despite "
                "guest having no price.read permission.",
            )

    def test_compiler_m2m_filter_uses_per_nested_perm(self):
        """apply_to_queryset must accept allowed_m2m_subfields for per-nested
        M2M permission filtering."""
        from inspect import signature

        from turbodrf.compiler import CompiledQueryPlan

        sig = signature(CompiledQueryPlan.apply_to_queryset)
        self.assertIn(
            "allowed_m2m_subfields",
            sig.parameters,
            "apply_to_queryset must accept allowed_m2m_subfields for per-nested "
            "M2M permission filtering",
        )

    def test_m2m_render_does_not_apply_target_predicates(self):
        """M2M nested render uses `manager.all()` without applying target
        predicates. Latent issue: if a M2M target ever gets row scoping,
        nested arrays in parent responses will leak."""
        from inspect import getsource

        from turbodrf.serializers import TurboDRFSerializer

        src = getsource(TurboDRFSerializer._serialize_m2m_field)
        applies = ("get_predicates" in src) or (".filter(" in src)
        if not applies and "m2m_manager.all()" in src:
            self.fail(
                "VULNERABILITY (latent): _serialize_m2m_field calls "
                "m2m_manager.all() without applying the target model's "
                "predicates or tenant_field. If a M2M target model gets "
                "predicates / tenant_field, nested arrays in parent "
                "responses leak across tenants."
            )


# ============================================================================
# Predicate-algebra invariants
# ============================================================================


class TestPredicateAlgebraInvariants(TestCase):
    """Conditional collapse + co-tenant check gating."""

    def test_conditional_returns_q_for_privileged_role_within_tenant(self):
        """Conditional operates within tenant. Privileged users (those with
        require_roles) get Q() — no within-tenant restriction. Tenant
        boundary is enforced separately."""
        from turbodrf.predicates import Conditional

        c = Conditional(when=Q(is_staff_loan=True), require_roles=["special"])
        # Privileged user → Q() (no within-tenant restriction)
        self.assertEqual(c.q(request=None, user_roles={"special"}), Q())
        # Non-privileged user → ~when (matching rows excluded)
        self.assertEqual(
            c.q(request=None, user_roles={"viewer"}),
            ~Q(is_staff_loan=True),
        )

    def test_co_tenant_check_fires_only_when_tenant_field_present(self):
        """serializers._apply_predicate_writes runs the co-tenant check only
        when the host has a `tenant_field` setting."""
        from inspect import getsource

        from turbodrf.serializers import _apply_predicate_writes

        src = getsource(_apply_predicate_writes)
        self.assertIn("tenant_user_field and tenant_field", src)


# ============================================================================
# Anon POST to tenant-scoped endpoint short-circuits at permission layer
# ============================================================================


class TestPrefillRequiresAuthenticatedUser(TestCase):
    """_prefill_required_fields skips on unauthenticated requests, so the
    permission layer must reject anon writes before serializer validation."""

    def setUp(self):
        import tests.urls  # noqa: F401

        cache.clear()
        self.client = APIClient()

    def tearDown(self):
        cache.clear()

    def test_anon_post_to_tenant_scoped_endpoint_403(self):
        # Deal has no public_access — anon should get 403 immediately
        r = self.client.post("/api/deals/", {"title": "leak"}, format="json")
        self.assertEqual(r.status_code, 403)
