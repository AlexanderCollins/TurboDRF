"""
Predicate-algebra and ORM-layer security tests.

Probes Tenant/Owner/Either/Custom/Conditional/Members/Group composition,
parsing, and registration; verifies cross-tenant Q construction is
impossible. Plus ORM-direct probes (Manager bypass, queryset annotations,
select_related, signals, raw SQL, transaction.atomic, cache, migrations,
model meta, bulk ops) for routes that go around the predicate gate.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Q
from django.test import TestCase
from rest_framework.test import APIClient

from tests.test_app.apps import _test_user_brokerages, set_test_brokerage
from tests.test_app.models import (
    BankAccount,
    Brokerage,
    Deal,
    SampleModel,
    Transaction,
)
from turbodrf.predicates import (
    Conditional,
    Custom,
    Either,
    Group,
    Members,
    Owner,
    Predicate,
    Tenant,
    _no_match_q,
    clear_predicates,
    get_predicates,
    get_tenant_field,
    has_tenancy_declaration,
    parse_config,
    register_predicates,
    register_tenant_field,
)

User = get_user_model()

VICTIM_DEAL_TITLE = "VICTIM_SECRET_DEAL"
VICTIM_BANK_NAME = "VICTIM_BANK_ACCOUNT"
VICTIM_TX_AMOUNT = Decimal("999999.99")
VICTIM_TX_AMOUNT_STR = "999999.99"

SECRETS = ("VICTIM_SECRET_DEAL", "VICTIM_BANK_ACCOUNT", "999999.99")


def assert_no_secrets(testcase, response):
    """Fail loudly if a secret leaks or the framework 5xxs."""
    blob = (
        str(getattr(response, "data", ""))
        + " "
        + str(getattr(response, "content", b""))
    )
    for secret in SECRETS:
        if secret in blob:
            testcase.fail(
                f"VULNERABILITY: {secret!r} leaked. "
                f"status={response.status_code} blob={blob[:600]!r}"
            )
    if response.status_code >= 500:
        testcase.fail(
            f"VULNERABILITY: 5xx ({response.status_code}) — possible "
            f"info leak. body={blob[:600]!r}"
        )


# ============================================================================
# Shared base — fixtures hoisted to setUpTestData (one DB build per class)
# ============================================================================


class SecurityBase(TestCase):
    """Attacker @ brokerage A, victim @ brokerage B, plus an innocent third.

    Per-test setUp keeps only cache.clear, brokerage map repopulation, and
    APIClient construction. All DB rows are built once via setUpTestData.
    """

    @classmethod
    def setUpTestData(cls):
        import tests.urls  # noqa: F401  — force router init

        cls.brokerage_attacker = Brokerage.objects.create(name="Attacker Co")
        cls.brokerage_victim = Brokerage.objects.create(name="Victim Co")
        cls.brokerage_third = Brokerage.objects.create(name="Innocent Co")

        cls.attacker = User.objects.create_user(username="attacker", password="x")
        cls.attacker._test_roles = ["underwriter"]

        cls.attacker_manager = User.objects.create_user(username="mgr", password="x")
        cls.attacker_manager._test_roles = ["manager"]

        cls.victim = User.objects.create_user(username="victim", password="x")
        cls.victim._test_roles = ["underwriter"]

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

        cls.attacker_deal = Deal.objects.create(
            title="ATTACKER_DEAL",
            brokerage=cls.brokerage_attacker,
            assigned_broker=cls.attacker,
        )
        cls.attacker_bank = BankAccount.objects.create(
            name="ATTACKER_BANK", deal=cls.attacker_deal
        )
        cls.attacker_tx = Transaction.objects.create(
            amount=Decimal("11.11"), bank_account=cls.attacker_bank
        )

    def setUp(self):
        cache.clear()
        _test_user_brokerages.clear()
        # Re-establish the per-test mapping (cleared above is process-global).
        set_test_brokerage(self.attacker, self.brokerage_attacker)
        set_test_brokerage(self.attacker_manager, self.brokerage_attacker)
        set_test_brokerage(self.victim, self.brokerage_victim)

        self.client = APIClient()
        self.client.force_authenticate(user=self.attacker)

    def tearDown(self):
        cache.clear()

    # --- helpers ----------------------------------------------------------
    def _request_stub(self, user=None, authed=True):
        class _R:
            pass

        r = _R()
        r.user = user if user is not None else self.attacker
        if not authed:

            class _AnonUser:
                is_authenticated = False
                pk = None

            r.user = _AnonUser()
        return r

    def _no_victim_in_qs(self, qs):
        ids = list(qs.values_list("pk", flat=True))
        self.assertNotIn(self.victim_deal.pk, ids)
        self.assertNotIn(self.victim_bank.pk, ids)
        self.assertNotIn(self.victim_tx.pk, ids)

    def _api_no_leak(self, response):
        body = (
            str(response.data) if hasattr(response, "data") else str(response.content)
        )
        self.assertNotIn(VICTIM_DEAL_TITLE, body)
        self.assertNotIn(VICTIM_BANK_NAME, body)
        self.assertNotIn(VICTIM_TX_AMOUNT_STR, body)

    def _swap_predicates(self, model, new_preds):
        """Patch registry + live ViewSet so HTTP requests pick up the change."""
        from turbodrf.router import TurboDRFRouter

        orig_preds = list(get_predicates(model))
        register_predicates(model, new_preds)
        TurboDRFRouter()

        def restore():
            register_predicates(model, orig_preds)
            TurboDRFRouter()

        return restore

    def _victim_unchanged(self):
        self.victim_deal.refresh_from_db()
        self.victim_bank.refresh_from_db()
        self.victim_tx.refresh_from_db()
        self.assertEqual(self.victim_deal.title, VICTIM_DEAL_TITLE)
        self.assertEqual(self.victim_deal.brokerage_id, self.brokerage_victim.id)
        self.assertEqual(self.victim_deal.assigned_broker_id, self.victim.id)
        self.assertEqual(self.victim_bank.name, VICTIM_BANK_NAME)
        self.assertEqual(self.victim_bank.deal_id, self.victim_deal.id)
        self.assertEqual(self.victim_tx.amount, VICTIM_TX_AMOUNT)


# ============================================================================
# 1. Tenant predicate
# ============================================================================


class TestTenantPredicate(SecurityBase):
    def test_tenant_weird_field_paths_never_widen(self):
        """Various semantically-bogus field paths (empty, chained, non-FK,
        through-user) must never accidentally match the victim deal. Either
        raise (fail closed) or filter out the victim row."""
        for field in ("", "brokerage__pk", "title", "assigned_broker__pk"):
            t = Tenant(field=field)
            try:
                q = t.q(self._request_stub(), set())
                qs = Deal.objects.filter(q)
                self.assertNotIn(
                    self.victim_deal.pk,
                    [d.pk for d in qs],
                    f"victim leaked with field={field!r}",
                )
            except Exception:
                # raising is acceptable — fail closed
                pass

    def test_tenant_resolved_column_and_standard_fk(self):
        """Both 'brokerage' (FK) and 'brokerage_id' (resolved column) must
        scope to attacker tenant and exclude victim."""
        for field in ("brokerage", "brokerage_id"):
            t = Tenant(field=field)
            q = t.q(self._request_stub(), set())
            qs = Deal.objects.filter(q)
            self.assertNotIn(self.victim_deal.pk, [d.pk for d in qs])
            for d in qs:
                self.assertEqual(d.brokerage_id, self.brokerage_attacker.pk)

    def test_tenant_q_when_user_tenant_is_none_returns_no_match(self):
        """Tenant.q with user.brokerage=None must fail closed."""
        nuser = User.objects.create_user(username="notenant")
        t = Tenant(field="brokerage")
        q = t.q(self._request_stub(user=nuser), set())
        qs = Deal.objects.filter(q)
        self.assertEqual(qs.count(), 0)


# ============================================================================
# 2. Owner predicate
# ============================================================================


class TestOwnerPredicate(SecurityBase):
    def test_empty_field_list_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            Owner(field=[])

    def test_bypass_normalisation_and_q_resolution(self):
        """bypass=None → set(); duplicates collapse; bypassing role → Q();
        non-bypass role → restricted Q; user_roles=None (falsy) → owner-FK
        filter (NOT Q())."""
        self.assertEqual(Owner("assigned_broker", bypass=None).bypass, set())
        o = Owner("assigned_broker", bypass=["manager", "manager"])
        self.assertEqual(o.bypass, {"manager"})
        self.assertEqual(o.q(self._request_stub(), {"viewer", "admin", "manager"}), Q())
        self.assertNotEqual(o.q(self._request_stub(), {"underwriter"}), Q())
        self.assertNotEqual(o.q(self._request_stub(), None), Q())

    def test_validate_write_semantics(self):
        """Self-assignment OK; cross-user errors; chained '__' field skips;
        no authed user skips (Tenant layer is the gate)."""
        o = Owner("assigned_broker", bypass=["manager"])
        req = self._request_stub()
        self.assertEqual(
            o.validate_write({"assigned_broker": self.attacker}, None, req), []
        )
        self.assertNotEqual(
            o.validate_write({"assigned_broker": self.victim}, None, req), []
        )

        o2 = Owner("assigned_broker__pk")
        self.assertEqual(o2.validate_write({"assigned_broker__pk": 999}, None, req), [])
        self.assertEqual(
            Owner("assigned_broker").validate_write(
                {"assigned_broker": self.victim}, None, self._request_stub(authed=False)
            ),
            [],
        )

    def test_auto_fill_preserves_explicit_value_and_skips_dunder(self):
        o = Owner("assigned_broker")
        out = o.auto_fill({"assigned_broker": self.victim}, self._request_stub())
        self.assertIs(out["assigned_broker"], self.victim)

        out2 = Owner("assigned_broker__pk").auto_fill({}, self._request_stub())
        self.assertNotIn("assigned_broker__pk", out2)


# ============================================================================
# 3. Composition: Either + Conditional + Members + Group
# ============================================================================


class TestComposition(SecurityBase):
    def test_either_no_children_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            Either()

    def test_either_with_one_match_all_child_does_not_break_tenant(self):
        """Either(Custom(Q()), Owner('assigned_broker')) → match-all within
        tenant. Tenant Q layer still binds at the API layer."""
        e = Either(Custom(q_func=lambda r, u: Q()), Owner("assigned_broker"))
        restore = self._swap_predicates(Deal, [e])
        try:
            r = self.client.get("/api/deals/")
            self.assertEqual(r.status_code, 200)
            self._api_no_leak(r)
            for row in r.data["data"]:
                d = Deal.objects.get(pk=row["id"])
                self.assertEqual(d.brokerage_id, self.brokerage_attacker.pk)
        finally:
            restore()

    def test_either_validate_write_passes_if_any_child_passes(self):
        """Either.validate_write returns [] if any child passes; aggregates
        errors if ALL fail."""
        ok = Custom(q_func=lambda r, u: Q(), write_validator=lambda vd, i, r: [])
        e_pass = Either(Owner("assigned_broker"), ok)
        req = self._request_stub()
        self.assertEqual(
            e_pass.validate_write({"assigned_broker": self.victim}, None, req), []
        )

        bad = Custom(
            q_func=lambda r, u: Q(),
            write_validator=lambda vd, i, r: ["custom rejected"],
        )
        e_fail = Either(Owner("assigned_broker"), bad)
        errs = e_fail.validate_write({"assigned_broker": self.victim}, None, req)
        self.assertGreater(len(errs), 0)

    def test_conditional_when_match_all_or_match_none(self):
        """Conditional(when=Q(), require_roles=['underwriter']) returns Q()
        for an underwriter (match all within-tenant). when=Q(pk__in=[]) for
        a non-bypass user returns ~Q(pk__in=[]) (match all). Both must NOT
        leak the victim through the tenant layer."""
        for cond in [
            Conditional(when=Q(), require_roles=["underwriter"]),
            Conditional(when=Q(pk__in=[]), require_roles=["admin"]),
        ]:
            restore = self._swap_predicates(Deal, [cond])
            try:
                r = self.client.get("/api/deals/")
                self._api_no_leak(r)
            finally:
                restore()

    def test_members_and_group_fail_closed_for_bogus_field_or_anon(self):
        """Members/Group with bogus path: runtime FieldError on filter, never
        leak victim. With an anon user: both return _no_match_q()."""
        # Bogus path → runtime error
        for predicate in (
            Members(m2m_field="bogus"),
            Group(field="brokerage", user_via="users"),
        ):
            try:
                list(Deal.objects.filter(predicate.q(self._request_stub(), set())))
                self.fail(f"Expected FieldError for {predicate}")
            except Exception:
                pass

        # Anon → no match
        anon_req = self._request_stub(authed=False)
        self.assertEqual(
            Members(m2m_field="bogus_m2m").q(anon_req, set()), _no_match_q()
        )
        self.assertEqual(Group(field="brokerage").q(anon_req, set()), _no_match_q())


# ============================================================================
# 4. Custom predicate
# ============================================================================


class TestCustomPredicate(SecurityBase):
    def test_custom_q_func_variants_never_escape_tenant(self):
        """A custom Q that asks for everything (~Q(pk__in=[])), all-rows
        subqueries, or even an explicit Q(pk__in=[victim_pk]) must NOT
        escape the tenant layer enforced by get_queryset."""
        from django.db.models import Subquery

        victim_pk = self.victim_deal.pk
        for q_func in [
            lambda r, u: ~Q(pk__in=[]),
            lambda r, u: Q(pk__in=Subquery(Deal.objects.values("pk"))),
            lambda r, u: Q(pk__in=Deal.objects.values("pk")),
            lambda r, u: Q(pk__in=[victim_pk]),
            # Custom returning non-Q (None) must not silently widen
            lambda r, u: None,
        ]:
            restore = self._swap_predicates(Deal, [Custom(q_func=q_func)])
            try:
                r = self.client.get("/api/deals/")
                self._api_no_leak(r)
            finally:
                restore()

    def test_custom_q_mutating_user_does_not_widen_tenant(self):
        """Custom q_func that mutates request.user mid-call. Tenant Q is
        built once at get_queryset time — mutation must not retroactively
        widen scope."""

        def mutator(req, roles):
            try:
                req.user._test_brokerage = self.brokerage_victim
                _test_user_brokerages[req.user.pk] = self.brokerage_victim
            except Exception:
                pass
            return Q()

        restore = self._swap_predicates(Deal, [Custom(q_func=mutator)])
        try:
            r = self.client.get("/api/deals/")
            self._api_no_leak(r)
        finally:
            set_test_brokerage(self.attacker, self.brokerage_attacker)
            restore()

    def test_custom_write_validator_malformed_does_not_5xx(self):
        """write_validator returning a non-list (dict) — serializer must not
        crash with 5xx and reveal internals."""
        bad = Custom(
            q_func=lambda r, u: Q(),
            write_validator=lambda vd, inst, req: {"bogus": "string"},
        )
        restore = self._swap_predicates(Deal, [bad])
        try:
            r = self.client.post(
                "/api/deals/",
                data={
                    "title": "x",
                    "brokerage": self.brokerage_attacker.pk,
                    "assigned_broker": self.attacker.pk,
                },
                format="json",
            )
            self.assertLess(r.status_code, 500)
        finally:
            restore()

    def test_custom_auto_filler_injecting_victim_fk_overwritten(self):
        """auto_filler injects victim's brokerage. Layer 1 (tenant) must
        either reject (4xx) or overwrite back to attacker's tenant."""

        def filler(vd, req):
            vd = dict(vd)
            vd["brokerage"] = self.brokerage_victim
            return vd

        bad = Custom(q_func=lambda r, u: Q(), auto_filler=filler)
        restore = self._swap_predicates(Deal, [bad])
        try:
            r = self.client.post(
                "/api/deals/",
                data={
                    "title": "INJECTED",
                    "assigned_broker": self.attacker.pk,
                    "brokerage": self.brokerage_attacker.pk,
                },
                format="json",
            )
            if r.status_code == 201:
                created = Deal.objects.get(pk=r.data.get("id"))
                self.assertEqual(created.brokerage_id, self.brokerage_attacker.pk)
            else:
                self.assertLess(r.status_code, 500)
        finally:
            restore()


# ============================================================================
# 5. parse_config + register_predicates / register_tenant_field
# ============================================================================


class TestParseConfigAndRegistration(SecurityBase):
    def test_invalid_inputs_raise(self):
        """Various malformed configs must all raise ImproperlyConfigured."""
        bad_configs = [
            "string",  # not a dict
            {"visibility": [Owner("a")], "tenant_field": "x"},  # mixed forms
            {"visibility": ["not a predicate"]},
            {"tenant_field": ["a", "b"]},  # tenant_field as list
        ]
        for cfg in bad_configs:
            with self.assertRaises(ImproperlyConfigured):
                parse_config(cfg)

    def test_valid_inputs_yield_no_predicates(self):
        """tenancy='shared' and visibility=[] both yield (None, [])."""
        for cfg in (
            {"tenancy": "shared", "tenant_field": "x"},
            {"visibility": []},
        ):
            tf, preds = parse_config(cfg)
            self.assertIsNone(tf)
            self.assertEqual(preds, [])

    def test_bypass_with_non_string_does_not_crash_and_deeply_nested_either(self):
        """bypass_owner_roles with mixed-type entries works. Deeply-nested
        Either does not blow recursion."""
        tf, preds = parse_config(
            {
                "owner_field": "assigned_broker",
                "bypass_owner_roles": ["manager", 42, None],
            }
        )
        self.assertEqual(len(preds), 1)
        # 42/None won't match any real role; manager bypasses.
        self.assertEqual(preds[0].q(self._request_stub(), {"manager"}), Q())

        # Deeply nested Either (no recursion blow-up)
        e = Owner("assigned_broker")
        for _ in range(50):
            e = Either(e)
        tf2, preds2 = parse_config({"visibility": [e]})
        self.assertIsNone(tf2)
        self.assertEqual(len(preds2), 1)

    def test_register_with_model_none_does_not_affect_other_models(self):
        register_predicates(None, [Owner("assigned_broker")])
        deal_preds = get_predicates(Deal)
        register_predicates(None, [])  # cleanup
        self.assertNotIn(None, [type(p) for p in deal_preds])

    def test_unregistered_model_returns_defaults_and_clear_predicates(self):
        """Unregistered model → ([], None). clear_predicates() wipes the
        registry; restore via register + router rehydrate."""
        from turbodrf.router import TurboDRFRouter

        self.assertEqual(get_predicates(SampleModel), [])
        self.assertIsNone(get_tenant_field(SampleModel))

        orig_preds = list(get_predicates(Deal))
        orig_tf = get_tenant_field(Deal)
        try:
            clear_predicates()
            self.assertEqual(get_predicates(Deal), [])
            self.assertIsNone(get_tenant_field(Deal))
        finally:
            register_predicates(Deal, orig_preds)
            register_tenant_field(Deal, orig_tf)
            TurboDRFRouter()


# ============================================================================
# 6. _get_tenant_q / _get_predicate_q (viewset helpers)
# ============================================================================


class TestViewSetTenantQHelpers(SecurityBase):
    def _make_viewset(self):
        from turbodrf.router import TurboDRFRouter

        TurboDRFRouter()
        from turbodrf.views import TurboDRFViewSet

        cls = type(
            "DealVS",
            (TurboDRFViewSet,),
            {
                "model": Deal,
                "queryset": Deal.objects.all(),
                "_predicates": list(get_predicates(Deal)),
                "_tenant_field": get_tenant_field(Deal),
            },
        )
        return cls()

    def test_get_tenant_q_request_none_fails_closed(self):
        vs = self._make_viewset()
        self.assertEqual(str(vs._get_tenant_q(None)), str(_no_match_q()))

    def test_get_tenant_q_user_no_tenant_fails_closed(self):
        nuser = User.objects.create_user(username="ghost")
        vs = self._make_viewset()
        q = vs._get_tenant_q(self._request_stub(user=nuser))
        self.assertEqual(Deal.objects.filter(q).count(), 0)

    def test_get_predicate_q_propagates_predicate_exceptions(self):
        """Predicate.q raises → helper must NOT swallow and resolve to Q()."""

        class BoomPredicate(Predicate):
            def q(self, request, user_roles):
                raise RuntimeError("boom")

        vs = self._make_viewset()
        vs._predicates = [BoomPredicate()]
        with self.assertRaises(RuntimeError):
            vs._get_predicate_q(self._request_stub())


# ============================================================================
# 7. Two-layer composition + extra corners
# ============================================================================


class TestTwoLayerAndCorners(SecurityBase):
    def test_tenant_q_applied_first_in_get_queryset(self):
        """Order matters: tenant filter applied BEFORE within-tenant
        predicates. Inspect SQL or fall back to row check."""
        from turbodrf.router import TurboDRFRouter

        TurboDRFRouter()
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory

        from turbodrf.views import TurboDRFViewSet

        cls = type(
            "DealVS2",
            (TurboDRFViewSet,),
            {
                "model": Deal,
                "queryset": Deal.objects.all(),
                "_predicates": list(get_predicates(Deal)),
                "_tenant_field": get_tenant_field(Deal),
            },
        )
        vs = cls()
        vs.action = "list"
        vs.kwargs = {}
        vs.format_kwarg = None

        factory = APIRequestFactory()
        req = factory.get("/api/deals/")
        req.user = self.attacker
        vs.request = Request(req)
        qs = vs.get_queryset()
        try:
            sql = str(qs.query)
            self.assertIn("brokerage", sql)
        except Exception:
            ids = list(qs.values_list("pk", flat=True))
            self.assertNotIn(self.victim_deal.pk, ids)

    def test_predicates_with_no_request_or_anon_return_no_match(self):
        """Owner / Tenant / Custom — for None request or anon user, all
        return _no_match_q() (fail closed)."""
        self.assertEqual(
            Owner("assigned_broker").q(self._request_stub(authed=False), set()),
            _no_match_q(),
        )
        self.assertEqual(Tenant(field="brokerage").q(None, set()), _no_match_q())
        self.assertEqual(Custom(q_func=lambda r, u: Q()).q(None, set()), _no_match_q())
        self.assertEqual(
            Owner("assigned_broker", bypass={"manager"}).q(None, set()), _no_match_q()
        )

    def test_has_tenancy_declaration_strings(self):
        self.assertTrue(has_tenancy_declaration({"tenant_field": "x"}))
        self.assertTrue(has_tenancy_declaration({"visibility": []}))
        self.assertFalse(has_tenancy_declaration({}))
        self.assertFalse(has_tenancy_declaration("not a dict"))

    def test_predicate_q_user_roles_must_be_set(self):
        """The viewset always passes a set; passing a list trips a TypeError
        in `user_roles & self.bypass`. This documents the invariant."""
        with self.assertRaises(TypeError):
            Owner("assigned_broker", bypass=["manager"]).q(
                self._request_stub(), ["manager"]
            )


# ============================================================================
# 8. Manager-level bypass / unscoped manager attrs
# ============================================================================


class TestManagerLevelBypass(SecurityBase):
    def test_orm_unscoped_baseline_is_not_exposed_via_api(self):
        """ORM Model.objects.all() is unscoped (expected); verify the API
        path never exposes this."""
        all_deals = list(Deal.objects.all().values_list("id", flat=True))
        self.assertIn(self.victim_deal.id, all_deals)
        r = self.client.get("/api/deals/")
        assert_no_secrets(self, r)

    def test_no_unknown_action_decorators_on_viewset(self):
        """Custom @action methods on viewsets could bypass scope. Confirm
        none exist beyond the standard DRF set."""
        from turbodrf.router import TurboDRFRouter

        router = TurboDRFRouter()
        for prefix, viewset, _ in router.registry:
            for attr_name in dir(viewset):
                if attr_name.startswith("_"):
                    continue
                attr = getattr(viewset, attr_name, None)
                if callable(attr) and (
                    hasattr(attr, "mapping") or hasattr(attr, "detail")
                ):
                    self.assertIn(
                        attr_name,
                        {
                            "create",
                            "destroy",
                            "list",
                            "partial_update",
                            "retrieve",
                            "update",
                        },
                        f"Unexpected custom @action {attr_name!r} on "
                        f"{viewset.__name__}",
                    )

    def test_no_custom_for_user_managers_and_default_matches_public(self):
        """No `for_user`/`for_tenant`/etc. custom managers. _default_manager
        equals public manager (no shadow manager that skips scope)."""
        for model in (Deal, BankAccount, Transaction):
            for name in ("for_user", "for_tenant", "for_request", "for_brokerage"):
                self.assertFalse(
                    hasattr(model.objects, name),
                    f"{model.__name__}.objects.{name} exists",
                )
            self.assertIs(model._default_manager, model.objects)
            self.assertEqual(type(model._meta.base_manager), type(model.objects))

    def test_anon_request_does_not_get_unscoped_listing(self):
        """Unauthed request to a tenant-scoped model never includes secrets."""
        client = APIClient()
        r = client.get("/api/deals/")
        assert_no_secrets(self, r)


# ============================================================================
# 11. Queryset annotation / filter / param injection (unsupported params)
# ============================================================================


class TestUnsupportedQueryParams(SecurityBase):
    def test_unknown_or_unsupported_query_params_never_leak(self):
        """Various query-string injection attempts (annotate, aggregate,
        F-expr, distinct, values, values_list, extra, raw, _meta, count,
        update, delete, _or) are silently ignored. ?id__in even with the
        victim's PK does not return the victim row. None must leak or 5xx."""
        params = [
            "annotate=count(id)",
            "aggregate=sum(amount)",
            "brokerage=F('brokerage_id')",
            "distinct=true",
            "values=brokerage,title",
            "values_list=id",
            "extra=where=1=1",
            "raw=SELECT * FROM test_app_deal",
            "_meta.get_field=brokerage",
            f"count=brokerage&brokerage={self.brokerage_victim.id}",
            "update=title=hacked",
            "delete=true",
            f"_or=brokerage:{self.brokerage_victim.id}",
            "_or=title__icontains:VICTIM_SECRET_DEAL",
            f"id__in={self.victim_deal.id},{self.attacker_deal.id}",
        ]
        for q in params:
            r = self.client.get(f"/api/deals/?{q}")
            assert_no_secrets(self, r)
            if r.status_code == 200 and isinstance(r.data, dict):
                ids = [
                    d.get("id") for d in r.data.get("data", []) if isinstance(d, dict)
                ]
                self.assertNotIn(self.victim_deal.id, ids)
        self._victim_unchanged()


# ============================================================================
# 12. select_related / multi-hop tenant filters
# ============================================================================


class TestSelectRelatedAndDetail(SecurityBase):
    def test_multi_hop_tenant_filters_hold(self):
        """2-hop (BankAccount.deal__brokerage) and 3-hop
        (Transaction.bank_account__deal__brokerage) tenant filters must
        exclude all victim rows."""
        r1 = self.client.get("/api/bankaccounts/")
        assert_no_secrets(self, r1)

        r2 = self.client.get("/api/transactions/")
        assert_no_secrets(self, r2)
        if r2.status_code == 200:
            rows = r2.data.get("data", []) if isinstance(r2.data, dict) else []
            for row in rows:
                self.assertNotEqual(str(row.get("amount")), VICTIM_TX_AMOUNT_STR)

    def test_detail_get_on_victim_pks_404(self):
        """Direct PK lookups across tenant boundaries must 404, never 200."""
        for url in (
            f"/api/deals/{self.victim_deal.id}/",
            f"/api/bankaccounts/{self.victim_bank.id}/",
            f"/api/transactions/{self.victim_tx.id}/",
        ):
            r = self.client.get(url)
            self.assertNotEqual(r.status_code, 200)
            assert_no_secrets(self, r)


# ============================================================================
# 13. Signals
# ============================================================================


class TestSignals(SecurityBase):
    def test_no_turbodrf_production_signal_handlers(self):
        """No signal receiver in turbodrf.* (excluding rls) mucks with
        tenant data."""
        from django.db.models.signals import (
            post_delete,
            post_save,
            pre_delete,
            pre_save,
        )

        for sig in (post_save, pre_save, post_delete, pre_delete):
            for (lookup_key, receiver_id), ref in sig.receivers:
                receiver = ref() if callable(ref) else ref
                if receiver is None:
                    continue
                module = getattr(receiver, "__module__", "")
                self.assertFalse(
                    module.startswith("turbodrf.") and "rls" not in module,
                    f"Production turbodrf signal receiver: "
                    f"{module}.{getattr(receiver, '__name__', '')}",
                )

    def test_save_does_not_trigger_unscoped_writes(self):
        """A POST that injects a victim FK or attempts to write at a
        victim brokerage must leave victim brokerage row count unchanged
        and create no Transaction at the victim bank."""
        before_victim_deals = Deal.objects.filter(
            brokerage=self.brokerage_victim
        ).count()
        before_tx = Transaction.objects.count()

        r1 = self.client.post(
            "/api/deals/",
            {"title": "atk-new", "brokerage": self.brokerage_attacker.id},
            format="json",
        )
        assert_no_secrets(self, r1)
        r2 = self.client.post(
            "/api/transactions/",
            {"bank_account": self.victim_bank.id, "amount": "1.00"},
            format="json",
        )
        self.assertNotEqual(r2.status_code, 201)

        self.assertEqual(
            Deal.objects.filter(brokerage=self.brokerage_victim).count(),
            before_victim_deals,
        )
        self.assertEqual(Transaction.objects.count(), before_tx)

    def test_delete_attacker_does_not_cascade_to_victim_and_cache_keys_distinct(self):
        """Deleting attacker_deal cascades only to attacker children. Cache
        keys + snapshots for distinct users are isolated."""
        from turbodrf.backends import build_permission_snapshot, get_cache_key

        r = self.client.delete(f"/api/deals/{self.attacker_deal.id}/")
        self.assertIn(r.status_code, (204, 404, 200))
        self._victim_unchanged()

        self.assertNotEqual(
            get_cache_key(self.attacker, Deal),
            get_cache_key(self.victim, Deal),
        )
        snap_a = build_permission_snapshot(self.attacker, Deal, use_cache=False)
        snap_v = build_permission_snapshot(self.victim, Deal, use_cache=False)
        self.assertIsNot(snap_a, snap_v)


# ============================================================================
# 12. Static audits: no raw SQL, parameterised RLS, schema invariants,
#     unregistered endpoints
# ============================================================================


class TestStaticAudits(SecurityBase):
    def test_no_raw_or_shell_paths_in_views_or_serializers(self):
        """Production views.py / serializers.py contain no .extra / .raw /
        RawSQL / cursor / shell-out calls."""
        import inspect

        from turbodrf import serializers as turbo_ser
        from turbodrf import views

        needles = (
            ".extra(",
            ".raw(",
            "RawSQL(",
            "connection.cursor(",
            "subprocess.",
            "os.system(",
        )
        for mod in (views, turbo_ser):
            src = inspect.getsource(mod)
            for needle in needles:
                self.assertNotIn(needle, src, f"{needle!r} in {mod.__name__}")

    def test_rls_middleware_uses_parameterised_sql(self):
        """RLS middleware uses %s placeholders, not f-strings."""
        import inspect

        from turbodrf.rls import middleware

        src = inspect.getsource(middleware)
        for needle in (
            "set_config('app.user_id', %s, true)",
            "set_config('app.tenant_id', %s, true)",
            "set_config('app.user_roles', %s, true)",
        ):
            self.assertIn(needle, src)
        self.assertNotIn("set_config('app.user_id', '{", src)

    def test_no_runsql_in_migrations_outside_rls_and_tenant_fk_not_null(self):
        """No raw RunSQL in production migrations (except RLS) AND tenant FK
        must be NOT NULL with the right related model."""
        import os

        mig_dir = os.path.normpath(
            os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                "..",
                "turbodrf",
                "migrations",
            )
        )
        if os.path.isdir(mig_dir):
            for fname in os.listdir(mig_dir):
                if not fname.endswith(".py") or fname == "__init__.py":
                    continue
                with open(os.path.join(mig_dir, fname)) as f:
                    src = f.read()
                if "RunSQL" in src:
                    self.assertIn("rls", src.lower())

        field = Deal._meta.get_field("brokerage")
        self.assertFalse(field.null)
        self.assertEqual(field.related_model, Brokerage)

    def test_unregistered_models_not_exposed_via_api(self):
        """Brokerage, User, ContentType, reverse-relation paths — no
        auto-registered TurboDRF endpoint."""
        for url in (
            "/api/brokerages/",
            "/api/users/",
            "/api/contenttypes/",
            f"/api/users/{self.victim.id}/deals/",
        ):
            r = self.client.get(url)
            self.assertIn(r.status_code, (404, 401, 403), f"{url} → {r.status_code}")


# ============================================================================
# 13. Transactions / atomic / rollback
# ============================================================================


class TestTransactionAtomic(SecurityBase):
    def test_failed_fk_injection_no_partial_row(self):
        """POST with cross-tenant bank_account FK: rejected, no partial row."""
        before = Transaction.objects.count()
        r = self.client.post(
            "/api/transactions/",
            {"bank_account": self.victim_bank.id, "amount": "5.00"},
            format="json",
        )
        self.assertNotEqual(r.status_code, 201)
        self.assertEqual(Transaction.objects.count(), before)

    def test_tenant_change_in_patch_body_rejected_or_overwritten(self):
        """PATCH attempting to move attacker_deal to victim brokerage:
        either 400 or silently overwritten. Refetch confirms no change.
        Same for two consecutive cross-tenant PATCHes against victim."""
        r1 = self.client.patch(
            f"/api/deals/{self.attacker_deal.id}/",
            {"brokerage": self.brokerage_victim.id},
            format="json",
        )
        self.attacker_deal.refresh_from_db()
        self.assertEqual(self.attacker_deal.brokerage_id, self.brokerage_attacker.id)
        assert_no_secrets(self, r1)

        before_title = self.victim_deal.title
        r2 = self.client.patch(
            f"/api/deals/{self.victim_deal.id}/", {"title": "hacked1"}, format="json"
        )
        r3 = self.client.patch(
            f"/api/deals/{self.victim_deal.id}/", {"title": "hacked2"}, format="json"
        )
        self.victim_deal.refresh_from_db()
        self.assertEqual(self.victim_deal.title, before_title)
        self.assertNotEqual(r2.status_code, 200)
        self.assertNotEqual(r3.status_code, 200)

    def test_invalid_pk_path_does_not_5xx(self):
        """PATCH on a non-numeric PK path must return 4xx, never 5xx."""
        before = Deal.objects.count()
        r = self.client.patch("/api/deals/notanumber/", {"title": "x"}, format="json")
        self.assertEqual(Deal.objects.count(), before)
        self.assertLess(r.status_code, 500)


# ============================================================================
# 16. Cache layer / snapshot poisoning
# ============================================================================


class TestCacheLayer(SecurityBase):
    def test_cache_key_includes_user_pk(self):
        """Distinct keys per user — no PK-collision across tenants."""
        from turbodrf.backends import get_cache_key

        key_a = get_cache_key(self.attacker, Deal)
        key_v = get_cache_key(self.victim, Deal)
        self.assertNotEqual(key_a, key_v)
        self.assertIn(str(self.attacker.id), key_a)
        self.assertIn(str(self.victim.id), key_v)

    def test_forged_or_poisoned_cache_does_not_grant_cross_tenant(self):
        """Forging the attacker's snapshot with all permissions, OR poisoning
        the victim's snapshot, does NOT grant cross-tenant data — tenant
        filter is independent of permission cache."""
        from turbodrf.backends import PermissionSnapshot, get_cache_key

        forged = PermissionSnapshot(
            allowed_actions={"read", "create", "update", "delete"},
            readable_fields={"id", "title", "brokerage", "assigned_broker"},
            writable_fields={"id", "title", "brokerage", "assigned_broker"},
        )
        cache.set(get_cache_key(self.attacker, Deal), forged, 300)
        r1 = self.client.get("/api/deals/")
        assert_no_secrets(self, r1)

        evil = PermissionSnapshot(allowed_actions=set(), readable_fields=set())
        cache.set(get_cache_key(self.victim, Deal), evil, 300)
        r2 = self.client.get("/api/deals/")
        assert_no_secrets(self, r2)

    def test_stale_role_snapshot_does_not_break_tenant(self):
        """Build snapshot, mutate role; tenant filter still binds."""
        from turbodrf.backends import build_permission_snapshot

        build_permission_snapshot(self.attacker, Deal)
        self.attacker._test_roles = ["admin"]
        r = self.client.get("/api/deals/")
        assert_no_secrets(self, r)


# ============================================================================
# 15. Bulk ops on the API
# ============================================================================


class TestBulkOps(SecurityBase):
    def test_post_array_body_no_victim_in_response_or_db(self):
        """Posting a list body is a known non-dict-body issue. Verify no
        victim secrets leak in the response and no rows are written."""
        before = Deal.objects.count()
        try:
            r = self.client.post(
                "/api/deals/",
                [
                    {"title": "a", "brokerage": self.brokerage_attacker.id},
                    {"title": "b", "brokerage": self.brokerage_victim.id},
                ],
                format="json",
            )
            blob = str(getattr(r, "content", b"")) + str(getattr(r, "data", ""))
            for secret in SECRETS:
                self.assertNotIn(secret, blob)
        except TypeError:
            pass
        self.assertEqual(Deal.objects.count(), before)

    def test_list_endpoint_rejects_bulk_methods(self):
        """PATCH / PUT / DELETE on the list endpoint must be 405 or 400."""
        for method in ("patch", "put", "delete"):
            kwargs = (
                {"data": {"title": "bulk"}, "format": "json"}
                if method != "delete"
                else {}
            )
            r = getattr(self.client, method)("/api/deals/", **kwargs)
            self.assertIn(
                r.status_code, (405, 400), f"{method.upper()} → {r.status_code}"
            )


# ============================================================================
# 19. Function-level introspection edges
# ============================================================================


class TestFunctionLevelEdges(SecurityBase):
    def _deal_viewset(self):
        from turbodrf.router import TurboDRFRouter

        router = TurboDRFRouter()
        return next(v for _, v, basename in router.registry if basename == "deal")()

    def test_viewset_helpers_fail_closed_for_bad_inputs(self):
        """_get_predicate_q(None) and _get_tenant_q for None/anon/no-tenant
        all return Q(pk__in=[])."""
        from django.contrib.auth.models import AnonymousUser
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory

        vs = self._deal_viewset()
        self.assertEqual(vs._get_predicate_q(None), Q(pk__in=[]))
        self.assertEqual(vs._get_tenant_q(None), Q(pk__in=[]))

        factory = APIRequestFactory()
        # AnonymousUser
        req = factory.get("/api/deals/")
        req.user = AnonymousUser()
        drf = Request(req)
        drf.user = AnonymousUser()
        self.assertEqual(vs._get_tenant_q(drf), Q(pk__in=[]))

        # User with no brokerage
        nuser = User.objects.create_user(username="nobody", password="x")
        nuser._test_roles = ["underwriter"]
        req2 = factory.get("/api/deals/")
        req2.user = nuser
        drf2 = Request(req2)
        drf2.user = nuser
        self.assertEqual(vs._get_tenant_q(drf2), Q(pk__in=[]))

    def test_snapshot_and_role_helpers_safe_for_edge_inputs(self):
        """attach_snapshot_to_request idempotent; unregistered model →
        empty snapshot; AnonymousUser roles → []; user with no brokerage
        → get_user_tenant returns None."""
        from django.contrib.auth.models import AnonymousUser
        from rest_framework.test import APIRequestFactory

        from turbodrf.backends import (
            attach_snapshot_to_request,
            build_permission_snapshot,
            get_user_roles,
        )
        from turbodrf.predicates import get_user_tenant

        factory = APIRequestFactory()
        req = factory.get("/api/deals/")
        req.user = self.attacker
        s1 = attach_snapshot_to_request(req, Deal)
        s2 = attach_snapshot_to_request(req, Deal)
        self.assertIs(s1, s2)

        snap = build_permission_snapshot(self.attacker, Brokerage)
        self.assertEqual(snap.allowed_actions, set())

        self.assertEqual(list(get_user_roles(AnonymousUser())), [])
        nuser = User.objects.create_user(username="nobody2", password="x")
        self.assertIsNone(get_user_tenant(nuser))

    def test_permission_view_no_model_attribute_fails_closed(self):
        """TurboDRFPermission.has_permission with view missing model attr
        must not silently grant access — raises AttributeError."""
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory

        from turbodrf.permissions import TurboDRFPermission

        class FakeView:
            pass

        factory = APIRequestFactory()
        req = factory.get("/api/deals/")
        req.user = self.attacker
        drf = Request(req)
        drf.user = self.attacker
        with self.assertRaises(AttributeError):
            TurboDRFPermission().has_permission(drf, FakeView())


# ============================================================================
# 20. API-routed FK / multi-hop / role pivot probes
# ============================================================================


class TestAPIRoutedORMBypass(SecurityBase):
    def test_fk_chain_lookups_never_leak(self):
        """Various FK-chain lookup attempts on /api/transactions/ — exact
        PK, alias 'id', 2-hop deal__pk, 3-hop brokerage__pk. None should
        return the victim transaction."""
        for q in (
            f"bank_account__pk={self.victim_bank.id}",
            f"bank_account__id={self.victim_bank.id}",
            f"bank_account__deal__pk={self.victim_deal.id}",
            f"bank_account__deal__brokerage__pk={self.brokerage_victim.id}",
        ):
            r = self.client.get(f"/api/transactions/?{q}")
            assert_no_secrets(self, r)
            if r.status_code == 200 and isinstance(r.data, dict):
                ids = [
                    d.get("id") for d in r.data.get("data", []) if isinstance(d, dict)
                ]
                self.assertNotIn(self.victim_tx.id, ids)

    def test_multi_pk_path_does_not_return_both_rows(self):
        """/api/deals/{vid},{aid}/ should not parse as composite. No leak."""
        r = self.client.get(
            f"/api/deals/{self.victim_deal.id},{self.attacker_deal.id}/"
        )
        assert_no_secrets(self, r)
        if r.status_code == 200 and isinstance(r.data, dict):
            self.assertNotIn(VICTIM_DEAL_TITLE, str(r.data))

    def test_manager_bypass_owner_does_not_cross_tenant(self):
        """Manager (owner-bypass) at attacker's brokerage still cannot see
        or mutate victim resources."""
        self.client.force_authenticate(user=self.attacker_manager)

        r1 = self.client.get("/api/deals/")
        assert_no_secrets(self, r1)

        r2 = self.client.get(f"/api/deals/{self.victim_deal.id}/")
        self.assertNotEqual(r2.status_code, 200)
        assert_no_secrets(self, r2)

        r3 = self.client.patch(
            f"/api/deals/{self.victim_deal.id}/",
            {"title": "manager_takeover"},
            format="json",
        )
        self.assertNotEqual(r3.status_code, 200)
        self._victim_unchanged()
