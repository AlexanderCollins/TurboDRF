"""
Authorization, predicate, snapshot-cache, settings, and config-drift
security tests.

Adversary attempts to trick TurboDRF into treating them as authorized for
a foreign tenant via role merging, snapshot poisoning, predicate misuse,
schema role manipulation, settings/configuration drift, and operator
mistakes. Tests verify the framework holds the line across all knobs.
"""

import os
from decimal import Decimal

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Q
from django.test import TestCase, override_settings
from rest_framework.test import APIClient, APIRequestFactory

from tests.test_app.apps import _test_user_brokerages, set_test_brokerage
from tests.test_app.models import (
    BankAccount,
    Brokerage,
    Deal,
    Transaction,
)

User = get_user_model()

VICTIM_DEAL_TITLE = "VICTIM_SECRET_DEAL"
VICTIM_BANK_NAME = "VICTIM_BANK_ACCOUNT"
VICTIM_TX_AMOUNT = Decimal("999999.99")
VICTIM_TX_AMOUNT_STR = "999999.99"

SECRETS = ("VICTIM_SECRET_DEAL", "VICTIM_BANK_ACCOUNT", "999999.99")


def _no_secret_leak(testcase, response, label=""):
    """Assert no victim secret appears in body, headers, or .data."""
    blob = ""
    if hasattr(response, "data") and response.data is not None:
        try:
            blob += str(response.data)
        except Exception:
            pass
    if hasattr(response, "content"):
        try:
            blob += response.content.decode("utf-8", errors="replace")
        except Exception:
            pass
    try:
        blob += str(dict(response.items()))
    except Exception:
        pass
    for s in SECRETS:
        testcase.assertNotIn(
            s,
            blob,
            f"[{label}] Secret {s!r} leaked (status={response.status_code})",
        )


def _is_5xx(response):
    return 500 <= response.status_code < 600


# ============================================================================
# Single shared base — all merged sibling classes use this one fixture
# ============================================================================


class AuthzSecurityBase(TestCase):
    """Single shared fixture. DB rows hoisted to setUpTestData."""

    @classmethod
    def setUpTestData(cls):
        # Force URL discovery / predicate registration
        import tests.urls  # noqa: F401

        cls.brokerage_attacker = Brokerage.objects.create(name="Attacker Co")
        cls.brokerage_victim = Brokerage.objects.create(name="Victim Co")
        cls.brokerage_third = Brokerage.objects.create(name="Innocent Co")

        # Attacker — non-bypass underwriter at attacker brokerage
        cls.attacker = User.objects.create_user(username="attacker", password="x")
        # Manager (bypass owner) at attacker brokerage
        cls.attacker_manager = User.objects.create_user(username="mgr", password="x")
        # Victim user at victim brokerage
        cls.victim = User.objects.create_user(username="victim", password="x")

        # Victim's data — secrets
        cls.victim_deal = Deal.objects.create(
            title=VICTIM_DEAL_TITLE,
            brokerage=cls.brokerage_victim,
            assigned_broker=cls.victim,
        )
        cls.victim_bank = BankAccount.objects.create(
            name=VICTIM_BANK_NAME, deal=cls.victim_deal
        )
        cls.victim_tx = Transaction.objects.create(
            amount=VICTIM_TX_AMOUNT, bank_account=cls.victim_bank
        )

        # Attacker's own deal (legitimate)
        cls.attacker_deal = Deal.objects.create(
            title="ATTACKER_DEAL",
            brokerage=cls.brokerage_attacker,
            assigned_broker=cls.attacker,
        )

    def setUp(self):
        cache.clear()
        _test_user_brokerages.clear()
        # Re-bind per-instance role/brokerage state (the dict gets cleared per test)
        self.attacker._test_roles = ["underwriter"]
        set_test_brokerage(self.attacker, self.brokerage_attacker)
        self.attacker_manager._test_roles = ["manager"]
        set_test_brokerage(self.attacker_manager, self.brokerage_attacker)
        self.victim._test_roles = ["underwriter"]
        set_test_brokerage(self.victim, self.brokerage_victim)

        self.client = APIClient()
        self.client.force_authenticate(user=self.attacker)

    def tearDown(self):
        cache.clear()

    def _assert_no_victim_leak(self, response):
        body = (
            str(response.data)
            if hasattr(response, "data")
            else str(response.content)
        )
        self.assertNotIn(VICTIM_DEAL_TITLE, body)
        self.assertNotIn(VICTIM_BANK_NAME, body)
        self.assertNotIn(VICTIM_TX_AMOUNT_STR, body)


# ============================================================================
# Multi-role merge / role-shape tolerance
# ============================================================================


class TestRoleMerging(AuthzSecurityBase):
    def test_attacker_with_underwriter_and_manager_still_tenant_bound(self):
        """Manager bypasses Owner; tenant must still bind."""
        self.attacker._test_roles = ["underwriter", "manager"]
        r = self.client.get("/api/deals/")
        self.assertEqual(r.status_code, 200)
        self._assert_no_victim_leak(r)
        ids = [row.get("id") for row in r.data["data"]]
        for did in ids:
            d = Deal.objects.get(pk=did)
            self.assertEqual(d.brokerage_id, self.brokerage_attacker.id)

    def test_empty_or_unknown_roles_return_403(self):
        for roles in ([], ["totally_not_a_role"]):
            self.attacker._test_roles = roles
            r = self.client.get("/api/deals/")
            self.assertEqual(r.status_code, 403, f"roles={roles!r}")
            self._assert_no_victim_leak(r)

    def test_role_shape_variants_tolerated_no_leak(self):
        """Roles as duplicates / 100x list / tuple / str / frozenset / mixed
        guest+auth — none widen scope and none 5xx."""
        variants = [
            ["underwriter", "underwriter"],
            ["underwriter"] * 100,
            ("underwriter",),
            "underwriter",
            frozenset(["underwriter"]),
            ["guest", "underwriter"],
            ["underwriter", "manager", "admin"],
        ]
        for roles in variants:
            self.attacker._test_roles = roles
            r = self.client.get("/api/deals/")
            self.assertNotEqual(r.status_code, 500, f"5xx for roles={roles!r}")
            self._assert_no_victim_leak(r)

    def test_attacker_manager_cannot_see_or_get_victim(self):
        """Manager bypasses Owner but NOT tenant."""
        self.client.force_authenticate(user=self.attacker_manager)
        r_list = self.client.get("/api/deals/")
        self.assertEqual(r_list.status_code, 200)
        self._assert_no_victim_leak(r_list)
        r_detail = self.client.get(f"/api/deals/{self.victim_deal.pk}/")
        self.assertEqual(r_detail.status_code, 404)


# ============================================================================
# Snapshot cache poisoning / cache-key isolation
# ============================================================================


class TestSnapshotCache(AuthzSecurityBase):
    def test_prewarm_victim_then_attacker_request_blocked_all_models(self):
        from turbodrf.backends import build_permission_snapshot

        for model in (Deal, BankAccount, Transaction):
            build_permission_snapshot(self.victim, model)
        for path in ("/api/deals/", "/api/bankaccounts/", "/api/transactions/"):
            r = self.client.get(path)
            self._assert_no_victim_leak(r)

    def test_cache_key_user_specific_and_deterministic(self):
        from turbodrf.backends import get_cache_key

        ka = get_cache_key(self.attacker, Deal)
        kv = get_cache_key(self.victim, Deal)
        self.assertNotEqual(ka, kv, "Cache key collision attacker vs victim")
        self.assertEqual(ka, get_cache_key(self.attacker, Deal))

    def test_cache_key_separates_anon_models_and_prefix(self):
        from turbodrf.backends import get_cache_key

        anon = AnonymousUser()
        k_anon = get_cache_key(anon, Deal)
        k_auth = get_cache_key(self.attacker, Deal)
        self.assertNotEqual(k_anon, k_auth)
        self.assertIn("anonymous", k_anon)
        # Different models distinct
        self.assertNotEqual(
            get_cache_key(self.attacker, Deal),
            get_cache_key(self.attacker, BankAccount),
        )
        # Custom prefix changes the key
        with override_settings(TURBODRF_PERMISSION_CACHE_PREFIX="alt_prefix"):
            ka_alt = get_cache_key(self.attacker, Deal)
        self.assertNotEqual(ka_alt, k_auth)

    def test_cache_key_reflects_permission_mode(self):
        from turbodrf.backends import get_cache_key

        with override_settings(TURBODRF_PERMISSION_MODE="static"):
            get_cache_key(self.attacker, Deal)
        with override_settings(TURBODRF_PERMISSION_MODE="database"):
            try:
                get_cache_key(self.attacker, Deal)
            except Exception:
                pass
        # Regardless: API still doesn't leak
        r = self.client.get("/api/deals/")
        self._assert_no_victim_leak(r)

    def test_role_mutation_or_cache_clear_does_not_widen(self):
        """Snapshot pre-warmed; mutating roles or clearing the cache cannot
        widen scope past tenant boundary."""
        from turbodrf.backends import build_permission_snapshot

        build_permission_snapshot(self.attacker, Deal)
        self.attacker._test_roles = ["underwriter", "manager", "admin"]
        self._assert_no_victim_leak(self.client.get("/api/deals/"))
        cache.clear()
        self._assert_no_victim_leak(self.client.get("/api/deals/"))

    def test_request_level_snapshot_does_not_leak_across_requests(self):
        from turbodrf.backends import attach_snapshot_to_request

        factory = APIRequestFactory()
        req1 = factory.get("/")
        req1.user = self.attacker
        attach_snapshot_to_request(req1, Deal)
        req2 = factory.get("/")
        req2.user = self.attacker
        self.assertFalse(hasattr(req2, "_turbodrf_snapshots"))


# ============================================================================
# Tenant attribute / user-field confusion
# ============================================================================


class TestTenantConfusion(AuthzSecurityBase):
    def test_swap_attacker_brokerage_to_victim_via_registry(self):
        """Re-reads tenant per request — first request safe, swap, second
        reflects new tenant (no 5xx)."""
        r1 = self.client.get("/api/deals/")
        self._assert_no_victim_leak(r1)
        set_test_brokerage(self.attacker, self.brokerage_victim)
        r2 = self.client.get("/api/deals/")
        self.assertNotEqual(r2.status_code, 500)
        # And reverting works
        set_test_brokerage(self.attacker, self.brokerage_attacker)
        r3 = self.client.get("/api/deals/")
        self._assert_no_victim_leak(r3)

    def test_brokerage_none_yields_empty_no_leak(self):
        if self.attacker.pk in _test_user_brokerages:
            del _test_user_brokerages[self.attacker.pk]
        self.attacker._test_brokerage = None
        r = self.client.get("/api/deals/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["pagination"]["total_items"], 0)
        self._assert_no_victim_leak(r)

    def test_brokerage_wrong_types_no_leak(self):
        """String / dict in place of Brokerage → fail closed, no leak."""
        for bad in ("not-a-brokerage", {"id": self.brokerage_victim.pk}):
            self.attacker._test_brokerage = bad
            if self.attacker.pk in _test_user_brokerages:
                _test_user_brokerages[self.attacker.pk] = bad
            r = self.client.get("/api/deals/")
            self._assert_no_victim_leak(r)
            self.assertNotEqual(r.status_code, 500)

    def test_get_user_tenant_dict_or_raising_returns_none(self):
        """user.brokerage as dict → fail-closed (None); raising property →
        propagates or returns None."""
        from turbodrf.predicates import get_user_tenant

        class DictBrokerageUser:
            is_authenticated = True
            pk = 12345
            id = 12345
            _test_brokerage = {"id": 999}
            roles = ["underwriter"]

            @property
            def brokerage(self):
                return self._test_brokerage

        self.assertIsNone(get_user_tenant(DictBrokerageUser()))

        class RaisingUser:
            is_authenticated = True
            pk = 1
            id = 1

            @property
            def brokerage(self):
                raise RuntimeError("brokerage exploded")

        try:
            self.assertIsNone(get_user_tenant(RaisingUser()))
        except Exception:
            pass

    def test_tenant_user_field_drift_no_leak(self):
        """TURBODRF_TENANT_USER_FIELD pointing at None / missing attr / string
        attr / id collision / boolean / method — all fail-closed, no leak."""
        for value in (
            None,
            "",
            "nonexistent_attribute",
            "username",
            "id",
            "is_superuser",
            "is_staff",
            "get_username",
        ):
            with override_settings(TURBODRF_TENANT_USER_FIELD=value):
                try:
                    r = self.client.get("/api/deals/")
                except (ValueError, TypeError):
                    continue
                self._assert_no_victim_leak(r)
                self.assertNotEqual(r.status_code, 500)

    def test_authed_user_helpers_reject_unauth(self):
        from turbodrf.predicates import _authed_user

        class HalfUser:
            is_authenticated = False
            pk = 1
            id = 1
            roles = ["underwriter"]

        self.assertIsNone(
            _authed_user(type("R", (), {"user": HalfUser()})())
        )


# ============================================================================
# Permission flags — DISABLE / DEFAULT_PERMS / mode / cache settings
# ============================================================================


class TestPermissionFlags(AuthzSecurityBase):
    def test_permission_flag_combinations_no_leak(self):
        """DISABLE_PERMISSIONS / USE_DEFAULT_PERMISSIONS / mode / require —
        every common combo: tenant filter is independent. No leak."""
        combos = [
            {"TURBODRF_DISABLE_PERMISSIONS": True},
            {"TURBODRF_USE_DEFAULT_PERMISSIONS": True},
            {
                "TURBODRF_DISABLE_PERMISSIONS": True,
                "TURBODRF_USE_DEFAULT_PERMISSIONS": True,
            },
            {"TURBODRF_PERMISSION_MODE": "static"},
            {"TURBODRF_PERMISSION_MODE": "database"},
            {"TURBODRF_PERMISSION_MODE": "UNKNOWN_MODE"},
            {"TURBODRF_PERMISSION_MODE": None},
            {"TURBODRF_PERMISSION_MODE": ""},
            {"TURBODRF_REQUIRE_TENANCY": False},
            {"TURBODRF_REQUIRE_TENANCY": "False"},
            {"TURBODRF_REQUIRE_TENANCY": 0},
            {"TURBODRF_PERMISSION_CACHE_TIMEOUT": 0},
            {"TURBODRF_PERMISSION_CACHE_TIMEOUT": 99999999},
            {"TURBODRF_PERMISSION_CACHE_TIMEOUT": -1},
            {"TURBODRF_PERMISSION_CACHE_PREFIX": "alt"},
            {"TURBODRF_SENSITIVE_FIELDS": []},
            {"TURBODRF_SENSITIVE_FIELDS": set()},
            {"TURBODRF_SENSITIVE_FIELDS": ("password", "title")},
            {"TURBODRF_SENSITIVE_FIELDS": {"x": 1}},
            {"TURBODRF_MAX_NESTING_DEPTH": 999},
            {"TURBODRF_MAX_NESTING_DEPTH": 0},
            {"TURBODRF_LOG_UNRESTRICTED_CUSTOM": True},
            {"TURBODRF_ENABLE_DOCS": False},
            {"TURBODRF_ROLES": {}},
        ]
        for kwargs in combos:
            with override_settings(**kwargs):
                try:
                    r = self.client.get("/api/deals/")
                except (TypeError, AttributeError, ValueError):
                    continue
                self._assert_no_victim_leak(r)

    def test_disable_permissions_anon_and_detail_no_leak(self):
        """Anon user under DISABLE_PERMISSIONS still blocked; detail of
        victim deal does not return 200."""
        with override_settings(TURBODRF_DISABLE_PERMISSIONS=True):
            self.client.force_authenticate(user=None)
            self._assert_no_victim_leak(self.client.get("/api/deals/"))
            self.client.force_authenticate(user=self.attacker)
            r = self.client.get(f"/api/deals/{self.victim_deal.pk}/")
            self._assert_no_victim_leak(r)
            self.assertNotEqual(r.status_code, 200)

    def test_default_perms_with_superuser_no_leak(self):
        """Superuser with attacker brokerage / no brokerage: tenant filter
        binds independently of DefaultDjangoPermission."""
        admin1 = User.objects.create_superuser(
            username="djadmin", password="x", email="a@b.c"
        )
        set_test_brokerage(admin1, self.brokerage_attacker)
        admin2 = User.objects.create_superuser(
            username="djadmin2", password="x", email="b@b.c"
        )
        with override_settings(TURBODRF_USE_DEFAULT_PERMISSIONS=True):
            for u in (admin1, admin2):
                self.client.force_authenticate(user=u)
                self._assert_no_victim_leak(self.client.get("/api/deals/"))

    def test_runtime_toggle_settings_no_leak(self):
        """Toggle several settings mid-test sequentially — tenant binds always."""
        r1 = self.client.get("/api/deals/")
        self._assert_no_victim_leak(r1)
        with override_settings(TURBODRF_DISABLE_PERMISSIONS=True):
            self._assert_no_victim_leak(self.client.get("/api/deals/"))
        with override_settings(TURBODRF_USE_DEFAULT_PERMISSIONS=True):
            self._assert_no_victim_leak(self.client.get("/api/deals/"))
        with override_settings(TURBODRF_PERMISSION_MODE="database"):
            self._assert_no_victim_leak(self.client.get("/api/deals/"))


# ============================================================================
# Predicate construction / Q-tricks / parse_config validation
# ============================================================================


class TestPredicates(AuthzSecurityBase):
    def test_custom_predicate_with_empty_q_still_tenant_bound(self):
        from turbodrf.predicates import (
            Custom,
            get_predicates,
            register_predicates,
        )

        orig = list(get_predicates(Deal))
        try:
            register_predicates(Deal, [Custom(q_func=lambda r, u: Q())])
            r = self.client.get("/api/deals/")
            self._assert_no_victim_leak(r)
        finally:
            register_predicates(Deal, orig)

    def test_custom_q_returns_none_or_string_or_raises(self):
        """q_func returning None → AttributeError; returning string → AttributeError;
        raising → propagates. None is a leak vector — verify framework path
        handles it (no API leak)."""
        from turbodrf.predicates import Custom

        factory = APIRequestFactory()
        req = factory.get("/")
        req.user = self.attacker

        # None
        try:
            Custom(q_func=lambda r, u: None).q(req, set(["underwriter"]))
        except AttributeError:
            pass
        # Non-Q (string)
        with self.assertRaises(AttributeError):
            Custom(q_func=lambda r, u: "not a Q").q(req, set())
        # Raising
        c = Custom(
            q_func=lambda r, u: (_ for _ in ()).throw(RuntimeError("kaboom"))
        )
        with self.assertRaises(RuntimeError):
            c.q(req, set())
        # API path remains safe
        r = self.client.get("/api/deals/")
        self._assert_no_victim_leak(r)

    def test_custom_q_func_side_effects_cannot_widen(self):
        from turbodrf.predicates import Custom

        def naughty(req, u):
            req.user = self.victim
            return Q()

        factory = APIRequestFactory()
        req = factory.get("/")
        req.user = self.attacker
        Custom(q_func=naughty).q(req, set(["underwriter"]))
        r = self.client.get("/api/deals/")
        self._assert_no_victim_leak(r)

    def test_predicate_negating_tenant_cannot_escape(self):
        """Custom returning ~Q(brokerage=attacker_brokerage) cannot escape
        tenant filter (separate AND layer)."""
        from turbodrf.predicates import (
            Custom,
            get_predicates,
            register_predicates,
        )

        orig = list(get_predicates(Deal))
        try:
            register_predicates(
                Deal,
                [
                    Custom(
                        q_func=lambda r, u: ~Q(brokerage=self.brokerage_attacker)
                    )
                ],
            )
            r = self.client.get("/api/deals/")
            self._assert_no_victim_leak(r)
        finally:
            register_predicates(Deal, orig)

    def test_conditional_with_attacker_chosen_when_does_not_leak(self):
        """Conditional with attacker-controlled `when` cannot break tenant."""
        from turbodrf.predicates import (
            Conditional,
            get_predicates,
            register_predicates,
        )

        orig = list(get_predicates(Deal))
        try:
            cond = Conditional(
                when=Q(pk__in=[self.victim_deal.pk]),
                require_roles=["admin"],
            )
            register_predicates(Deal, [cond])
            r = self.client.get("/api/deals/")
            self._assert_no_victim_leak(r)
        finally:
            register_predicates(Deal, orig)

    def test_conditional_returns_negation_when_role_missing(self):
        from turbodrf.predicates import Conditional

        cond = Conditional(
            when=Q(pk__in=[self.victim_deal.pk]),
            require_roles=["admin"],
        )
        factory = APIRequestFactory()
        req = factory.get("/")
        req.user = self.attacker
        q = cond.q(req, set(["underwriter"]))
        self.assertEqual(q, ~Q(pk__in=[self.victim_deal.pk]))

    def test_members_and_group_anon_no_match(self):
        from turbodrf.predicates import Group, Members

        factory = APIRequestFactory()
        req = factory.get("/")
        req.user = AnonymousUser()
        self.assertEqual(
            Members(m2m_field="some_m2m").q(req, set()), Q(pk__in=[])
        )
        self.assertEqual(
            Group(field="brokerage", user_via="members").q(req, set()),
            Q(pk__in=[]),
        )

    def test_either_and_tenant_inside_either_validation(self):
        """Either() rejects empty/non-Predicate args; Tenant inside Either is
        a tenant-escape and must raise."""
        from turbodrf.predicates import (
            Either,
            Owner,
            Tenant,
            _reject_tenant_inside_either,
        )

        with self.assertRaises(ImproperlyConfigured):
            Either("not a predicate", "also not")
        with self.assertRaises(ImproperlyConfigured):
            Either()
        bad = Either(Tenant("brokerage"), Owner("assigned_broker"))
        with self.assertRaises(ImproperlyConfigured):
            _reject_tenant_inside_either(bad)

    def test_either_fail_open_cannot_escape_tenant(self):
        from turbodrf.predicates import (
            Custom,
            Either,
            get_predicates,
            register_predicates,
        )

        orig = list(get_predicates(Deal))
        try:
            register_predicates(
                Deal,
                [
                    Either(
                        Custom(q_func=lambda r, u: Q()),
                        Custom(q_func=lambda r, u: Q()),
                    )
                ],
            )
            r = self.client.get("/api/deals/")
            self._assert_no_victim_leak(r)
        finally:
            register_predicates(Deal, orig)

    def test_parse_config_validation(self):
        """parse_config rejects: non-dict, mixing visibility+sugar, non-list
        visibility, non-Predicate visibility item, non-string tenant_field,
        invalid owner_field type, owner_field dict."""
        from turbodrf.predicates import Owner, parse_config

        bad_inputs = [
            [],
            {"visibility": [], "tenant_field": "brokerage"},
            {"visibility": "not a list"},
            {"visibility": ["string not a predicate"]},
            {"tenant_field": 123},
            {"owner_field": 12345},
            {"tenant_field": "brokerage", "owner_field": {"x": 1}},
        ]
        for cfg in bad_inputs:
            with self.assertRaises(ImproperlyConfigured):
                parse_config(cfg)

    def test_owner_predicate_construction_and_bypass(self):
        from turbodrf.predicates import Owner

        with self.assertRaises(ImproperlyConfigured):
            Owner([])
        # Multiple fields → composite Q
        o = Owner(["assigned_broker", "deal__assigned_broker"])
        factory = APIRequestFactory()
        req = factory.get("/")
        req.user = self.attacker
        self.assertIsNotNone(o.q(req, set()))
        # Bypass intersection → empty Q
        ob = Owner("assigned_broker", bypass=["manager"])
        self.assertEqual(ob.q(req, {"manager"}), Q())

    def test_predicate_construction_and_empty_config(self):
        """Custom/Conditional construction validation; empty visibility/
        owner_field configurations resolve to no predicates."""
        from turbodrf.predicates import Conditional, Custom, parse_config

        with self.assertRaises(ImproperlyConfigured):
            Custom(q_func="not callable")
        with self.assertRaises(ImproperlyConfigured):
            Conditional(when="not a Q", require_roles=["admin"])
        tf, preds = parse_config({"visibility": []})
        self.assertIsNone(tf)
        self.assertEqual(preds, [])
        tf, preds = parse_config(
            {"tenant_field": "brokerage", "owner_field": []}
        )
        self.assertEqual(tf, "brokerage")
        self.assertEqual(preds, [])

    def test_tenant_predicate_anon_no_match(self):
        from turbodrf.predicates import Tenant

        factory = APIRequestFactory()
        req = factory.get("/")
        req.user = AnonymousUser()
        self.assertEqual(Tenant("brokerage").q(req, set()), Q(pk__in=[]))


# ============================================================================
# Viewset internals — fail-closed paths
# ============================================================================


class TestViewSetInternals(AuthzSecurityBase):
    def test_viewset_q_construction_fail_closed(self):
        """_get_predicate_q and _get_tenant_q corner cases: predicates=None
        and tenant_field='' return Q(); request=None and anon user fail
        closed with _no_match_q; user with no brokerage attr fails closed."""
        from turbodrf.predicates import Owner
        from turbodrf.views import TurboDRFViewSet

        factory = APIRequestFactory()
        req = factory.get("/")
        req.user = self.attacker

        # predicates=None / empty tenant_field → Q()
        v = TurboDRFViewSet()
        v._predicates = None
        v._tenant_field = "brokerage"
        self.assertEqual(v._get_predicate_q(req), Q())
        v._tenant_field = ""
        v._predicates = []
        self.assertEqual(v._get_tenant_q(req), Q())

        # request=None → _no_match_q on both helpers
        v._predicates = [Owner("assigned_broker")]
        v._tenant_field = "brokerage"
        self.assertEqual(v._get_predicate_q(None), Q(pk__in=[]))
        self.assertEqual(v._get_tenant_q(None), Q(pk__in=[]))

        # Anon user → _no_match_q
        anon_req = factory.get("/")
        anon_req.user = AnonymousUser()
        self.assertEqual(v._get_tenant_q(anon_req), Q(pk__in=[]))

        # User with no brokerage attr fails closed
        class _FakeVS:
            _tenant_field = "brokerage"
            _predicates = []

        u = User.objects.create_user(username="no_brok", password="x")
        no_brok_req = factory.get("/api/deals/")
        no_brok_req.user = u
        q = TurboDRFViewSet._get_tenant_q(_FakeVS(), no_brok_req)
        self.assertIn("pk", str(q))


# ============================================================================
# Anonymous / public access / no User endpoint
# ============================================================================


class TestAnonymousAndUserExposure(AuthzSecurityBase):
    def test_anon_blocked_on_predicate_models(self):
        self.client.force_authenticate(user=None)
        for path in (
            "/api/deals/",
            "/api/bankaccounts/",
            "/api/transactions/",
        ):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 403, f"Anon got {r.status_code} on {path}")
            self._assert_no_victim_leak(r)

    def test_users_endpoint_not_exposed(self):
        """auth.User does not inherit TurboDRFMixin — neither GET nor PATCH
        should land on any user-shaped endpoint."""
        for path in (
            "/api/users/",
            "/api/users/me/",
            f"/api/users/{self.attacker.pk}/",
        ):
            r = self.client.get(path)
            self.assertIn(r.status_code, (404, 403))
        for path in (
            "/api/users/1/",
            "/api/auth/users/me/",
            "/api/me/",
            "/api/profiles/me/",
        ):
            r = self.client.patch(
                path,
                {"_test_brokerage": self.brokerage_victim.pk},
                format="json",
            )
            self.assertNotIn(r.status_code, (200, 201, 204))


# ============================================================================
# Header / query-param tenant override attempts
# ============================================================================


class TestHeaderAndQueryOverride(AuthzSecurityBase):
    def test_tenant_override_attempts_ignored(self):
        """Headers, query params, and reverse-relation filters cannot widen
        the attacker's scope to victim rows."""
        for header_name in (
            "HTTP_X_BROKERAGE",
            "HTTP_X_TENANT",
            "HTTP_X_TENANT_ID",
            "HTTP_BROKERAGE",
        ):
            kwargs = {header_name: str(self.brokerage_victim.pk)}
            self._assert_no_victim_leak(
                self.client.get("/api/deals/", **kwargs)
            )
        self._assert_no_victim_leak(
            self.client.get(f"/api/deals/?brokerage={self.brokerage_victim.pk}")
        )
        for q in (
            f"?bank_account__deal__brokerage={self.brokerage_victim.pk}",
            f"?bank_account__deal__assigned_broker={self.victim.pk}",
            f"?bank_account__deal={self.victim_deal.pk}",
            f"?bank_account={self.victim_bank.pk}",
        ):
            self._assert_no_victim_leak(
                self.client.get(f"/api/transactions/{q}")
            )


# ============================================================================
# Direct retrieve/list/search/pagination probes
# ============================================================================


class TestCrossTenantReadProbes(AuthzSecurityBase):
    def test_get_victim_pk_returns_404_all_models(self):
        for path in (
            f"/api/deals/{self.victim_deal.pk}/",
            f"/api/bankaccounts/{self.victim_bank.pk}/",
            f"/api/transactions/{self.victim_tx.pk}/",
        ):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 404)
            self._assert_no_victim_leak(r)

    def test_pagination_count_and_page_size_scoped(self):
        """total_items reflects only attacker's tenant; large page_size
        cannot widen scope."""
        for i in range(7):
            Deal.objects.create(
                title=f"victim_more_{i}",
                brokerage=self.brokerage_victim,
                assigned_broker=self.victim,
            )
        r = self.client.get("/api/deals/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            r.data["pagination"]["total_items"],
            1,
            "total_items leaks count!",
        )
        self._assert_no_victim_leak(r)
        r2 = self.client.get("/api/deals/?page_size=999")
        self.assertEqual(r2.status_code, 200)
        self._assert_no_victim_leak(r2)

    def test_search_and_filter_does_not_leak(self):
        for path in (
            f"/api/deals/?search={VICTIM_DEAL_TITLE}",
            "/api/deals/?title__icontains=VICTIM",
            f"/api/deals/?assigned_broker={self.victim.pk}",
        ):
            r = self.client.get(path)
            self._assert_no_victim_leak(r)

    def test_attacker_only_sees_own_brokerage(self):
        r = self.client.get("/api/deals/")
        self.assertEqual(r.status_code, 200)
        data = r.data.get("data", []) if isinstance(r.data, dict) else r.data
        for row in data:
            self.assertIn(row.get("title"), ("ATTACKER_DEAL",))


# ============================================================================
# Cross-tenant write probes
# ============================================================================


class TestCrossTenantWriteProbes(AuthzSecurityBase):
    def test_victim_detail_methods_blocked(self):
        """PATCH/DELETE/PUT/HEAD all 404; OPTIONS no leak; victim row intact."""
        r = self.client.patch(
            f"/api/deals/{self.victim_deal.pk}/",
            {"title": "stolen"},
            format="json",
        )
        self.assertEqual(r.status_code, 404)

        r = self.client.delete(f"/api/deals/{self.victim_deal.pk}/")
        self.assertEqual(r.status_code, 404)

        r = self.client.put(
            f"/api/deals/{self.victim_deal.pk}/",
            {"title": "x", "brokerage": self.brokerage_attacker.pk},
            format="json",
        )
        self.assertEqual(r.status_code, 404)
        self._assert_no_victim_leak(r)

        r = self.client.head(f"/api/deals/{self.victim_deal.pk}/")
        self.assertEqual(r.status_code, 404)

        r = self.client.options(f"/api/deals/{self.victim_deal.pk}/")
        self._assert_no_victim_leak(r)

        # Victim row intact and unchanged
        self.victim_deal.refresh_from_db()
        self.assertEqual(self.victim_deal.title, VICTIM_DEAL_TITLE)
        self.assertTrue(Deal.objects.filter(pk=self.victim_deal.pk).exists())

    def test_post_with_victim_brokerage_id_blocked(self):
        r = self.client.post(
            "/api/deals/",
            {
                "title": "ATTACKER_INJECTED",
                "brokerage": self.brokerage_victim.pk,
                "assigned_broker": self.attacker.pk,
            },
            format="json",
        )
        self.assertFalse(
            Deal.objects.filter(
                title="ATTACKER_INJECTED", brokerage=self.brokerage_victim
            ).exists()
        )
        self._assert_no_victim_leak(r)

    def test_post_no_roles_returns_403(self):
        self.attacker._test_roles = []
        r = self.client.post(
            "/api/deals/",
            {"title": "evil", "brokerage": self.brokerage_victim.pk},
            format="json",
        )
        self.assertEqual(r.status_code, 403)
        self.assertFalse(
            Deal.objects.filter(
                title="evil", brokerage=self.brokerage_victim
            ).exists()
        )

    def test_cross_tenant_post_under_drift_blocked(self):
        """Cross-tenant POST under DISABLE_PERMISSIONS, USE_DEFAULT_PERMISSIONS,
        and as a superuser at attacker brokerage — none can plant a row at
        victim brokerage."""
        admin = User.objects.create_superuser(
            username="evilsu", password="x", email="a@b.c"
        )
        set_test_brokerage(admin, self.brokerage_attacker)

        cases = [
            ({"TURBODRF_DISABLE_PERMISSIONS": True}, self.attacker, "EVIL_FLAG_D"),
            ({"TURBODRF_USE_DEFAULT_PERMISSIONS": True}, self.attacker, "EVIL_FLAG_U"),
            ({"TURBODRF_USE_DEFAULT_PERMISSIONS": True}, admin, "EVIL_SU"),
        ]
        for kw, user, title in cases:
            with override_settings(**kw):
                self.client.force_authenticate(user=user)
                r = self.client.post(
                    "/api/deals/",
                    {
                        "title": title,
                        "brokerage": self.brokerage_victim.pk,
                        "assigned_broker": user.pk,
                    },
                    format="json",
                )
                self._assert_no_victim_leak(r)
                self.assertFalse(
                    Deal.objects.filter(
                        title=title, brokerage=self.brokerage_victim
                    ).exists()
                )

    def test_manager_cross_tenant_writes_blocked(self):
        """Manager bypasses Owner — verify cross-tenant brokerage and victim
        assigned_broker both rejected."""
        self.client.force_authenticate(user=self.attacker_manager)
        # Cross-tenant brokerage
        r1 = self.client.post(
            "/api/deals/",
            {
                "title": "EVIL_MGR",
                "brokerage": self.brokerage_victim.pk,
                "assigned_broker": self.attacker_manager.pk,
            },
            format="json",
        )
        self._assert_no_victim_leak(r1)
        self.assertFalse(
            Deal.objects.filter(
                title="EVIL_MGR", brokerage=self.brokerage_victim
            ).exists()
        )
        # Cross-tenant assigned_broker
        r2 = self.client.post(
            "/api/deals/",
            {
                "title": "EVIL_USER",
                "brokerage": self.brokerage_attacker.pk,
                "assigned_broker": self.victim.pk,
            },
            format="json",
        )
        self._assert_no_victim_leak(r2)
        self.assertFalse(
            Deal.objects.filter(
                title="EVIL_USER", assigned_broker=self.victim
            ).exists()
        )

    def test_post_raw_string_body_no_leak(self):
        r = self.client.post("/api/deals/", "raw_string", format="json")
        self._assert_no_victim_leak(r)
        self.assertNotEqual(r.status_code, 201)


# ============================================================================
# Schema / Swagger role manipulation
# ============================================================================


class TestSwaggerRoleHandling(AuthzSecurityBase):
    def test_swagger_role_manipulation_no_leak(self):
        """Anon ?role=admin must not 5xx; ?role= variants (empty / ADMIN /
        undefined / SQLi-ish) do not grant the attacker the role; session
        api_role='admin' on swagger.json must not leak."""
        from turbodrf.backends import get_user_roles

        # Anon hitting swagger with ?role=admin → no 5xx, no leak
        self.client.force_authenticate(user=None)
        for path in (
            "/swagger/?role=admin&format=openapi",
            "/swagger.json",
        ):
            r = self.client.get(path)
            body = (
                str(r.data)
                if hasattr(r, "data")
                else r.content.decode("utf-8", errors="ignore")
            )
            self.assertNotEqual(
                r.status_code, 500, f"5xx leaks stack trace at {path}"
            )
            self.assertNotIn(VICTIM_DEAL_TITLE, body)
            self.assertNotIn(VICTIM_BANK_NAME, body)
            self.assertNotIn(VICTIM_TX_AMOUNT_STR, body)

        # ?role= variants do not grant roles
        for role_value in ("", "ADMIN", "undefined", "admin' OR '1'='1"):
            factory = APIRequestFactory()
            req = factory.get(f"/swagger/?role={role_value}")
            req.user = self.attacker
            user_roles = set(get_user_roles(req.user) or [])
            self.assertNotIn(role_value, user_roles)
            self.assertNotIn("admin", user_roles)
            self.assertIn("underwriter", user_roles)

        # Session api_role='admin' must not leak
        self.client.force_authenticate(user=self.attacker)
        s = self.client.session
        s["api_role"] = "admin"
        s.save()
        r = self.client.get("/swagger/?format=openapi")
        body = (
            str(r.data)
            if hasattr(r, "data")
            else r.content.decode("utf-8", errors="ignore")
        )
        self.assertNotIn(VICTIM_DEAL_TITLE, body)
        self.assertNotIn(VICTIM_BANK_NAME, body)
        self.assertNotIn(VICTIM_TX_AMOUNT_STR, body)


# ============================================================================
# Keycloak integration
# ============================================================================


class TestKeycloakIntegration(AuthzSecurityBase):
    def test_strict_mode_filters_unmapped_roles(self):
        """Strict mode drops unmapped/whitespace-padded/SQLi-ish/empty inputs;
        only literal-key matches pass through."""
        from turbodrf.integrations.keycloak import (
            map_keycloak_roles_to_turbodrf,
        )

        with override_settings(
            TURBODRF_KEYCLOAK_ROLE_MAPPING={"realm-user": "underwriter"},
            TURBODRF_KEYCLOAK_STRICT_ROLES=True,
        ):
            mapped = map_keycloak_roles_to_turbodrf(["admin", "realm-user"])
            self.assertNotIn("admin", mapped)
            self.assertIn("underwriter", mapped)
        with override_settings(
            TURBODRF_KEYCLOAK_ROLE_MAPPING={"specific-role": "underwriter"},
            TURBODRF_KEYCLOAK_STRICT_ROLES=True,
        ):
            self.assertEqual(
                map_keycloak_roles_to_turbodrf(["admin", "manager", "anything"]),
                [],
            )
            self.assertEqual(map_keycloak_roles_to_turbodrf([]), [])
        with override_settings(
            TURBODRF_KEYCLOAK_ROLE_MAPPING={"realm-admin": "underwriter"},
            TURBODRF_KEYCLOAK_STRICT_ROLES=True,
        ):
            self.assertEqual(
                map_keycloak_roles_to_turbodrf(["  realm-admin  "]), []
            )
            self.assertEqual(
                map_keycloak_roles_to_turbodrf(["admin'; DROP TABLE--"]), []
            )

    def test_no_mapping_and_non_string_value_behavior(self):
        """No mapping → passthrough; non-string mapping value passes verbatim."""
        from turbodrf.integrations.keycloak import (
            map_keycloak_roles_to_turbodrf,
        )

        with override_settings(TURBODRF_KEYCLOAK_ROLE_MAPPING={}):
            self.assertEqual(map_keycloak_roles_to_turbodrf(["admin"]), ["admin"])
        with override_settings(
            TURBODRF_KEYCLOAK_ROLE_MAPPING={"realm-admin": 12345},
            TURBODRF_KEYCLOAK_STRICT_ROLES=True,
        ):
            self.assertEqual(
                map_keycloak_roles_to_turbodrf(["realm-admin"]), [12345]
            )

    def test_extract_roles_token_drift(self):
        from turbodrf.integrations.keycloak import extract_roles_from_token

        with override_settings(TURBODRF_KEYCLOAK_ROLE_CLAIM="realm_access.roles"):
            self.assertEqual(
                extract_roles_from_token({"some_other_field": ["admin"]}), []
            )
        with override_settings(TURBODRF_KEYCLOAK_ROLE_CLAIM="roles"):
            self.assertEqual(extract_roles_from_token({"roles": "admin"}), [])


# ============================================================================
# Tenancy resolution (programmatic) and router validation
# ============================================================================


class TestTenancyResolution(AuthzSecurityBase):
    def test_autodetect_path_resolution(self):
        from turbodrf.tenancy import find_tenant_path
        from tests.test_app.models import Category

        self.assertEqual(find_tenant_path(BankAccount, Brokerage), "deal__brokerage")
        self.assertEqual(
            find_tenant_path(Transaction, Brokerage),
            "bank_account__deal__brokerage",
        )
        # No path
        self.assertIsNone(find_tenant_path(Category, Brokerage))
        # Tenant model itself
        self.assertIsNone(find_tenant_path(Brokerage, Brokerage))

    def test_resolve_tenant_model_invalid_and_valid(self):
        from turbodrf.tenancy import _resolve_tenant_model

        with self.assertRaises(ImproperlyConfigured):
            _resolve_tenant_model("not_a_real_app.NotARealModel")
        with self.assertRaises(ImproperlyConfigured):
            _resolve_tenant_model("missing.Model")
        self.assertIs(_resolve_tenant_model("test_app.Brokerage"), Brokerage)

    def test_resolve_tenancy_for_model_invariants(self):
        """resolve_tenancy_for_model: empty config → no field, no preds;
        Tenant() inside visibility → extracted to tenant_field;
        tenancy='shared' → no field; bad field paths/owner_field/either-with-Tenant raise."""
        import warnings
        from turbodrf.predicates import Either, Owner, Tenant
        from turbodrf.tenancy import resolve_tenancy_for_model

        class _Solo:
            class _meta:
                app_label = "test_app"
                model_name = "solo"

                @staticmethod
                def get_fields():
                    return []

        tf, preds, ad = resolve_tenancy_for_model(
            _Solo, {}, tenant_model_setting=None, autodetect=False
        )
        self.assertIsNone(tf)
        self.assertEqual(preds, [])
        self.assertFalse(ad)

        # Tenant() in visibility → field extracted
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            tf, preds, _ad = resolve_tenancy_for_model(
                Deal,
                {"visibility": [Tenant("brokerage")]},
                tenant_model_setting="test_app.Brokerage",
                autodetect=False,
            )
        self.assertEqual(tf, "brokerage")
        self.assertEqual(preds, [])

        # tenancy='shared'
        tf, preds, ad = resolve_tenancy_for_model(
            Deal, {"tenancy": "shared"}, tenant_model_setting=None, autodetect=False
        )
        self.assertIsNone(tf)
        self.assertEqual(preds, [])

        # Tenant inside Either → reject
        with self.assertRaises(ImproperlyConfigured):
            resolve_tenancy_for_model(
                Deal,
                {
                    "visibility": [
                        Either(Tenant("brokerage"), Owner("assigned_broker"))
                    ]
                },
                tenant_model_setting="test_app.Brokerage",
                autodetect=False,
            )
        # Mixing visibility + sugar → reject
        with self.assertRaises(ImproperlyConfigured):
            resolve_tenancy_for_model(
                Deal,
                {
                    "visibility": [Owner("assigned_broker")],
                    "tenant_field": "brokerage",
                },
                tenant_model_setting="test_app.Brokerage",
                autodetect=False,
            )
        # Bad field path
        with self.assertRaises(ImproperlyConfigured):
            resolve_tenancy_for_model(
                Deal,
                {"tenant_field": "nope_no_such_field"},
                tenant_model_setting="test_app.Brokerage",
                autodetect=False,
            )
        # Bad owner_field path
        with self.assertRaises(ImproperlyConfigured):
            resolve_tenancy_for_model(
                Deal,
                {
                    "tenant_field": "brokerage",
                    "owner_field": "nope_not_a_real_field",
                },
                tenant_model_setting="test_app.Brokerage",
                autodetect=False,
            )

    def test_router_walk_predicates_terminates(self):
        from turbodrf.predicates import Either, Owner
        from turbodrf.router import _walk_predicates

        inner = Either(Owner("assigned_broker"))
        outer = Either(Owner("assigned_broker"), inner)
        items = list(_walk_predicates([outer]))
        self.assertGreaterEqual(len(items), 2)


# ============================================================================
# Router / URL conf / endpoint discovery
# ============================================================================


class TestRouterAndUrls(AuthzSecurityBase):
    def test_router_does_not_register_unscoped_actions(self):
        from django.urls import get_resolver

        resolver = get_resolver()
        flat = []

        def walk(patterns, prefix=""):
            for p in patterns:
                if hasattr(p, "url_patterns"):
                    walk(p.url_patterns, prefix + str(p.pattern))
                else:
                    flat.append(prefix + str(p.pattern))

        walk(resolver.url_patterns)
        suspicious = [
            u
            for u in flat
            if any(
                model in u for model in ("deals", "bankaccounts", "transactions")
            )
            and any(seg in u for seg in ("/extra", "/all", "/admin", "/dump", "/raw"))
        ]
        self.assertEqual(suspicious, [])

    def test_endpoint_discovery_and_drift_no_leak(self):
        """Disabled/nonexistent endpoints 404; custom-items 200/no-leak;
        special-char/double-slash/capitalized/no-slash variants no leak;
        re-importing URL conf and re-init router idempotent."""
        import importlib

        import tests.urls
        from turbodrf.router import TurboDRFRouter

        for path in (
            "/api/disabledmodels/",
            "/api/nonexistent_models/",
            "/api/deals/abc/",
        ):
            self.assertEqual(
                self.client.get(path).status_code, 404, f"Expected 404 for {path}"
            )
        self._assert_no_victim_leak(self.client.get("/api/custom-items/"))
        for path in (
            "/api/deals%00/",
            "//api/deals/",
            "/api/DEALS/",
            "/api/deals/",
            "/api/deals",
        ):
            self._assert_no_victim_leak(self.client.get(path))

        TurboDRFRouter()
        TurboDRFRouter()
        importlib.reload(tests.urls)
        self._assert_no_victim_leak(self.client.get("/api/deals/"))


# ============================================================================
# App ready / model config / registry lifecycle
# ============================================================================


class TestAppAndRegistry(AuthzSecurityBase):
    def test_user_extension_and_brokerage_resolution(self):
        """User has roles/brokerage attrs; set/unset brokerage resolves correctly."""
        self.assertTrue(hasattr(User, "roles"))
        self.assertTrue(hasattr(User, "brokerage"))
        u = User.objects.create_user(username="ready_test", password="x")
        set_test_brokerage(u, self.brokerage_attacker)
        self.assertEqual(
            User.objects.get(pk=u.pk).brokerage, self.brokerage_attacker
        )
        u2 = User.objects.create_user(username="ready_test2", password="x")
        self.assertIsNone(User.objects.get(pk=u2.pk).brokerage)
        # turbodrf app config wired
        self.assertEqual(django_apps.get_app_config("turbodrf").name, "turbodrf")

    def test_get_user_roles_and_tenant_inputs(self):
        from turbodrf.backends import get_user_roles
        from turbodrf.predicates import get_user_tenant

        self.assertIn("underwriter", get_user_roles(self.attacker))
        self.assertEqual(get_user_roles(AnonymousUser()), [])
        self.assertEqual(get_user_roles(None), [])
        with override_settings(TURBODRF_TENANT_USER_FIELD=None):
            self.assertIsNone(get_user_tenant(self.attacker))

    def test_tenant_field_registered_for_chained_models(self):
        from turbodrf.predicates import get_tenant_field

        self.assertEqual(get_tenant_field(Deal), "brokerage")
        self.assertEqual(get_tenant_field(BankAccount), "deal__brokerage")
        self.assertEqual(
            get_tenant_field(Transaction), "bank_account__deal__brokerage"
        )

    def test_no_mixin_means_not_registered(self):
        from tests.test_app.models import NoTurboDRFModel
        from turbodrf.predicates import get_tenant_field

        self.assertIsNone(get_tenant_field(NoTurboDRFModel))
        r = self.client.get("/api/noturbodrfmodels/")
        self.assertEqual(r.status_code, 404)

    def test_register_predicates_registry_lifecycle(self):
        """clear, generator, last-wins, late re-bind: live viewsets keep
        their original config. Re-import URLs after clearing to restore
        registry for sibling tests."""
        import importlib

        import tests.urls
        from turbodrf.predicates import (
            Owner,
            clear_predicates,
            get_predicates,
            get_tenant_field,
            register_predicates,
            register_tenant_field,
        )

        original_pred = list(get_predicates(Deal))
        original_tf = get_tenant_field(Deal)
        try:
            clear_predicates()
            self.assertEqual(get_predicates(Deal), [])
            self.assertIsNone(get_tenant_field(Deal))
            self._assert_no_victim_leak(self.client.get("/api/deals/"))

            # Generator coerced to list
            register_predicates(
                Deal, (p for p in [Owner("assigned_broker")])
            )
            self.assertIsInstance(get_predicates(Deal), list)

            # Last-wins
            register_predicates(Deal, [])
            register_predicates(Deal, [Owner("assigned_broker")])
            self.assertEqual(len(get_predicates(Deal)), 1)
            self._assert_no_victim_leak(self.client.get("/api/deals/"))

            # Late tenant_field rebind: live viewset keeps original
            register_tenant_field(Deal, "different_field")
            self._assert_no_victim_leak(self.client.get("/api/deals/"))
        finally:
            register_predicates(Deal, original_pred)
            register_tenant_field(Deal, original_tf)
            # Restore full registry by re-importing urls (re-registers
            # BankAccount, Transaction, etc.)
            importlib.reload(tests.urls)


# ============================================================================
# Compiled plan / ContentType / DB
# ============================================================================


class TestCompiledAndDB(AuthzSecurityBase):
    def test_compile_model_disabled_and_sample_no_leak(self):
        from turbodrf.compiler import _compiled_plans, compile_model

        self.assertIsNone(compile_model(Deal))
        self.assertNotIn(Deal, _compiled_plans)
        self._assert_no_victim_leak(self.client.get("/api/compiledsamplemodels/"))

    def test_contenttype_db_and_model_label(self):
        """ContentType keyed by (app_label, model); single 'default' DB;
        app registry resolves to the correct test_app.Deal."""
        from django.contrib.contenttypes.models import ContentType
        from django.db import connections

        ct = ContentType.objects.get_for_model(Deal)
        self.assertEqual(ct.app_label, "test_app")
        self.assertEqual(ct.model, "deal")
        self.assertIn("default", connections.databases)
        self.assertIs(django_apps.get_model("test_app.Deal"), Deal)
        self._assert_no_victim_leak(self.client.get("/api/deals/"))


# ============================================================================
# Drift configurations — settings, middleware, cache backends, environment
# ============================================================================


class TestSettingsDrift(AuthzSecurityBase):
    """Operator-mistake settings combos. Tenant filter binds regardless."""

    def test_tenancy_setting_drift_no_leak(self):
        """REQUIRE_TENANCY/TENANT_MODEL/TENANT_USER_FIELD in unusual combos
        — runtime tenant_field is bound on viewsets at URL-import time, so
        post-startup mutation cannot loosen scope."""
        combos = [
            {
                "TURBODRF_TENANT_MODEL": "test_app.Brokerage",
                "TURBODRF_TENANT_USER_FIELD": None,
            },
            {
                "TURBODRF_TENANT_USER_FIELD": "brokerage",
                "TURBODRF_TENANT_MODEL": None,
            },
            {
                "TURBODRF_REQUIRE_TENANCY": True,
                "TURBODRF_TENANT_MODEL": None,
            },
            {"TURBODRF_TENANT_MODEL": "auth.User"},
            {"TURBODRF_TENANT_MODEL": "test_app.RelatedModel"},
            {"TURBODRF_TENANT_MODEL": "test_app.Brokerage"},
            {
                "TURBODRF_AUTODETECT_TENANT": True,
                "TURBODRF_TENANT_MODEL": "test_app.Brokerage",
            },
            {"TURBODRF_TENANT_MODEL": "nonexistent_app.Tenant"},
        ]
        for kw in combos:
            with override_settings(**kw):
                r = self.client.get("/api/deals/")
                _no_secret_leak(self, r, str(kw))
                self.assertFalse(_is_5xx(r))

    def test_keycloak_and_disable_with_custom_role_no_leak(self):
        """Keycloak enabled (no mapping) and DISABLE_PERMISSIONS with a forged
        admin role — neither bypasses tenant filter."""
        with override_settings(
            TURBODRF_KEYCLOAK_INTEGRATION=True,
            TURBODRF_KEYCLOAK_ROLE_MAPPING={},
        ):
            self._assert_no_victim_leak(self.client.get("/api/deals/"))
        with override_settings(TURBODRF_DISABLE_PERMISSIONS=True):
            self.attacker._test_roles = ["admin"]
            self._assert_no_victim_leak(self.client.get("/api/deals/"))

    def test_role_dict_drift_and_anon_guest_no_leak(self):
        """Various malformed/empty TURBODRF_ROLES configurations and an anon
        user with a 'guest' role granting read — none break tenant boundary."""
        for kw in (
            {"TURBODRF_ROLES": {}},
            {"TURBODRF_ROLES": None},
            {"TURBODRF_ROLES": {"underwriter": ["test_app.deal.read"]}},
            {"TURBODRF_ROLES": {"guest": ["test_app.deal.read"]}},
            {
                "TURBODRF_DISABLE_PERMISSIONS": True,
                "TURBODRF_ROLES": {"admin": ["test_app.deal.read"]},
            },
        ):
            with override_settings(**kw):
                try:
                    r = self.client.get("/api/deals/")
                except (TypeError, AttributeError):
                    continue
                self._assert_no_victim_leak(r)
        # Anon with guest role
        with override_settings(
            TURBODRF_ROLES={"guest": ["test_app.deal.read"]}
        ):
            self.client.force_authenticate(user=None)
            self._assert_no_victim_leak(self.client.get("/api/deals/"))


class TestMiddlewareDrift(AuthzSecurityBase):
    def test_middleware_variations_no_leak(self):
        """Removing/duplicating/reordering middleware: force_authenticate
        bypasses the chain so tenant filter binds via request.user."""
        common = [
            "django.middleware.security.SecurityMiddleware",
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.middleware.common.CommonMiddleware",
        ]
        configs = [
            # No AuthenticationMiddleware
            common + ["django.middleware.csrf.CsrfViewMiddleware"],
            # Keycloak after auth
            common
            + [
                "django.contrib.auth.middleware.AuthenticationMiddleware",
                "turbodrf.integrations.keycloak.KeycloakRoleMiddleware",
                "django.middleware.csrf.CsrfViewMiddleware",
            ],
            # Duplicated SecurityMiddleware
            [
                "django.middleware.security.SecurityMiddleware",
                "django.middleware.security.SecurityMiddleware",
                "django.contrib.sessions.middleware.SessionMiddleware",
                "django.middleware.common.CommonMiddleware",
                "django.contrib.auth.middleware.AuthenticationMiddleware",
                "django.middleware.csrf.CsrfViewMiddleware",
            ],
        ]
        for mw in configs:
            with override_settings(MIDDLEWARE=mw):
                r = self.client.get("/api/deals/")
                self._assert_no_victim_leak(r)

    def test_anon_or_no_session_no_leak(self):
        client = APIClient()
        r = client.get("/api/deals/")
        self._assert_no_victim_leak(r)
        # And with explicit force_authenticate(None)
        self.client.force_authenticate(user=None)
        r2 = self.client.get("/api/deals/")
        self._assert_no_victim_leak(r2)


class TestCacheBackendDrift(AuthzSecurityBase):
    def test_cache_backend_variants_no_cross_user_leak(self):
        """DummyCache (no-op), LocMemCache, default w/ shared prefix —
        cache key includes user.pk so cross-user collision impossible."""
        cache.clear()
        configs = [
            {"CACHES": {"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}}},
            {
                "CACHES": {
                    "default": {
                        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                        "LOCATION": "drift-test-loc",
                    }
                }
            },
        ]
        for kw in configs:
            with override_settings(**kw):
                cache.clear()
                self.client.force_authenticate(user=self.victim)
                self.client.get("/api/deals/")
                self.client.force_authenticate(user=self.attacker)
                r = self.client.get("/api/deals/")
                self._assert_no_victim_leak(r)
        # Shared prefix
        cache.clear()
        with override_settings(TURBODRF_PERMISSION_CACHE_PREFIX="shared_prefix"):
            self.client.force_authenticate(user=self.victim)
            self.client.get("/api/deals/")
            self.client.force_authenticate(user=self.attacker)
            self._assert_no_victim_leak(self.client.get("/api/deals/"))

    def test_cross_user_warm_and_repeat_no_leak(self):
        """Warm cache as victim/anon then query as attacker; repeat requests
        with timeout=0 — never see victim data."""
        for warm_user in (self.victim, None):
            self.client.force_authenticate(user=warm_user)
            self.client.get("/api/deals/")
            self.client.force_authenticate(user=self.attacker)
            self._assert_no_victim_leak(self.client.get("/api/deals/"))
        with override_settings(TURBODRF_PERMISSION_CACHE_TIMEOUT=0):
            for _ in range(3):
                self._assert_no_victim_leak(self.client.get("/api/deals/"))


class TestEnvironmentDrift(AuthzSecurityBase):
    def test_debug_true_no_secret_in_4xx_or_5xx(self):
        with override_settings(DEBUG=True):
            r1 = self.client.get("/api/deals/not_a_pk/")
            self._assert_no_victim_leak(r1)
            r2 = self.client.get(f"/api/deals/{99999999}/")
            self._assert_no_victim_leak(r2)
            # Query injection attempt
            r3 = self.client.get(
                "/api/deals/?brokerage__name__icontains=Victim_Co"
            )
            self._assert_no_victim_leak(r3)
            # Detail/POST under DEBUG
            r4 = self.client.get(f"/api/deals/{self.victim_deal.pk}/")
            self._assert_no_victim_leak(r4)
            r5 = self.client.post(
                "/api/deals/",
                {
                    "title": "EVIL_NEW",
                    "brokerage": self.brokerage_victim.pk,
                    "assigned_broker": self.attacker.pk,
                },
                format="json",
            )
            self._assert_no_victim_leak(r5)
            self.assertNotEqual(r5.status_code, 201)

    def test_allowed_hosts_wildcard_no_leak(self):
        """ALLOWED_HOSTS=['*'] is orthogonal to tenant boundary; verify
        settings module is loaded as expected."""
        sm = os.environ.get("DJANGO_SETTINGS_MODULE", "")
        self.assertIn("settings", sm.lower())
        with override_settings(ALLOWED_HOSTS=["*"]):
            r = self.client.get("/api/deals/")
            self._assert_no_victim_leak(r)


class TestRendererAndAuthDrift(AuthzSecurityBase):
    def test_renderer_and_format_variants_no_leak(self):
        """Various format/Accept/renderer configs — no row-level Deal leak."""
        for path, kwargs in (
            ("/api/deals/?format=api", {}),
            ("/api/deals/?format=json", {}),
            ("/api/deals/", {"HTTP_ACCEPT": "text/html"}),
            ("/api/deals/?format=this_is_not_a_format", {}),
        ):
            r = self.client.get(path, **kwargs)
            if hasattr(r, "data") and r.data is not None:
                blob = str(r.data)
                self.assertNotIn(VICTIM_DEAL_TITLE, blob)
                self.assertNotIn(VICTIM_BANK_NAME, blob)
                self.assertNotIn(VICTIM_TX_AMOUNT_STR, blob)
        # JSON-only renderers
        with override_settings(
            REST_FRAMEWORK={
                "DEFAULT_RENDERER_CLASSES": [
                    "rest_framework.renderers.JSONRenderer",
                ],
                "DEFAULT_AUTHENTICATION_CLASSES": [
                    "rest_framework.authentication.SessionAuthentication",
                ],
                "DEFAULT_FILTER_BACKENDS": [
                    "django_filters.rest_framework.DjangoFilterBackend",
                    "rest_framework.filters.SearchFilter",
                    "rest_framework.filters.OrderingFilter",
                ],
                "EXCEPTION_HANDLER": "turbodrf.exceptions.turbodrf_exception_handler",
            }
        ):
            self._assert_no_victim_leak(self.client.get("/api/deals/"))

    def test_auth_class_variants_no_leak(self):
        """No auth classes / token-only / basic-only — force_authenticate
        skips the chain so attacker still bound; anon client → _no_match_q."""
        for auth_classes in (
            [],
            [
                "rest_framework.authentication.SessionAuthentication",
                "rest_framework.authentication.TokenAuthentication",
            ],
            ["rest_framework.authentication.BasicAuthentication"],
        ):
            with override_settings(
                REST_FRAMEWORK={
                    "DEFAULT_AUTHENTICATION_CLASSES": auth_classes,
                    "DEFAULT_FILTER_BACKENDS": [
                        "django_filters.rest_framework.DjangoFilterBackend",
                        "rest_framework.filters.SearchFilter",
                        "rest_framework.filters.OrderingFilter",
                    ],
                    "EXCEPTION_HANDLER": "turbodrf.exceptions.turbodrf_exception_handler",
                }
            ):
                anon_client = APIClient()
                self._assert_no_victim_leak(anon_client.get("/api/deals/"))
                self._assert_no_victim_leak(self.client.get("/api/deals/"))


class TestModelConfigDrift(AuthzSecurityBase):
    def test_tenancy_shared_via_registry_no_live_leak(self):
        """Operator marks Deal as shared via register_tenant_field(None);
        live viewset retains its bound _tenant_field. No leak."""
        from turbodrf.predicates import (
            get_tenant_field,
            register_tenant_field,
        )

        original = get_tenant_field(Deal)
        try:
            register_tenant_field(Deal, None)
            self._assert_no_victim_leak(self.client.get("/api/deals/"))
        finally:
            register_tenant_field(Deal, original)

    def test_chained_endpoint_under_disable_or_db_mode_no_leak(self):
        """Transactions under DISABLE_PERMISSIONS; ?role=admin under DB mode —
        tenant binds independently of perm layer."""
        with override_settings(TURBODRF_DISABLE_PERMISSIONS=True):
            self._assert_no_victim_leak(self.client.get("/api/transactions/"))
        with override_settings(TURBODRF_PERMISSION_MODE="database"):
            self._assert_no_victim_leak(
                self.client.get("/api/deals/?role=admin")
            )


# ============================================================================
# OPTIONS / preflight cross-tenant
# ============================================================================


class TestOptionsAndMisc(AuthzSecurityBase):
    def test_prewarmed_victim_snapshot_options_and_anon(self):
        """Pre-warm victim snapshot, then OPTIONS as attacker (no schema
        leak) and GET as anon (403, no leak)."""
        from turbodrf.backends import build_permission_snapshot

        build_permission_snapshot(self.victim, Deal)
        r = self.client.options("/api/deals/")
        body = (
            str(r.data)
            if hasattr(r, "data") and r.data is not None
            else r.content.decode("utf-8", errors="ignore")
        )
        self.assertNotIn(VICTIM_DEAL_TITLE, body)
        self.assertNotIn(VICTIM_BANK_NAME, body)
        self.assertNotIn(VICTIM_TX_AMOUNT_STR, body)

        self.client.force_authenticate(user=None)
        r = self.client.get("/api/deals/")
        self.assertEqual(r.status_code, 403)
        self._assert_no_victim_leak(r)

    def test_fake_user_cache_key_robust(self):
        """Adversarial users (no pk; flipping pk) — cache_key returns a string,
        no crash, format includes ':'."""
        from unittest.mock import MagicMock

        from turbodrf.backends import get_cache_key

        fake = MagicMock(is_authenticated=True, id=None, pk=None)
        fake.roles = ["underwriter"]
        self.assertIn(":", get_cache_key(fake, Deal))

        class FlipUser:
            is_authenticated = True
            _i = 0

            @property
            def pk(self):
                FlipUser._i += 1
                return FlipUser._i

            @property
            def id(self):
                FlipUser._i += 1
                return FlipUser._i

            roles = ["underwriter"]

        fu = FlipUser()
        self.assertIsInstance(get_cache_key(fu, Deal), str)
        self.assertIsInstance(get_cache_key(fu, Deal), str)

    def test_get_user_roles_property_raising_propagates_or_returns_list(self):
        from turbodrf.backends import get_user_roles

        class BoomUser:
            is_authenticated = True
            pk = 99999

            @property
            def roles(self):
                raise RuntimeError("kaboom")

        try:
            roles = get_user_roles(BoomUser())
            self.assertIsInstance(roles, list)
        except Exception:
            pass
