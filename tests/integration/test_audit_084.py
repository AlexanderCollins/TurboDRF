"""Regressions for the 0.8.4 audit. Each test sends the attack that worked on
0.8.3 and asserts it no longer does."""

from contextlib import contextmanager
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.pagination import PageNumberPagination
from rest_framework.test import APIClient

from tests.fixtures import router_apps
from tests.test_app.apps import _test_user_brokerages, set_test_brokerage
from tests.test_app.models import (
    BankAccount,
    Brokerage,
    Deal,
    Holding,
    HoldingEvent,
    Transaction,
)

User = get_user_model()

TENANT = dict(TURBODRF_TENANT_MODEL="test_app.Brokerage", TURBODRF_REQUIRE_TENANCY=True)


@contextmanager
def _preserve_registries():
    import turbodrf.router as router_mod
    from turbodrf import predicates

    preds = dict(predicates._model_predicates)
    tenants = dict(predicates._model_tenant_fields)
    flag = router_mod._bypass_roles_validated
    try:
        yield
    finally:
        predicates._model_predicates.clear()
        predicates._model_predicates.update(preds)
        predicates._model_tenant_fields.clear()
        predicates._model_tenant_fields.update(tenants)
        router_mod._bypass_roles_validated = flag


def _router(*models_and_configs, **router_kwargs):
    """A router that sees only the given (model, config) pairs."""
    from turbodrf.router import TurboDRFRouter

    with _preserve_registries():
        patches = [
            mock.patch(
                "turbodrf.router.apps",
                router_apps([m for m, _ in models_and_configs]),
            )
        ] + [
            mock.patch.object(m, "turbodrf", classmethod(lambda cls, c=c: dict(c)))
            for m, c in models_and_configs
        ]
        for p in patches:
            p.start()
        try:
            return TurboDRFRouter(**router_kwargs)
        finally:
            for p in patches:
                p.stop()


class _Base(TestCase):
    def setUp(self):
        import tests.urls  # noqa: F401  (the router registers tenancy)

        cache.clear()
        _test_user_brokerages.clear()
        self.addCleanup(_test_user_brokerages.clear)
        self.client = APIClient()
        self.A = Brokerage.objects.create(name="A")
        self.B = Brokerage.objects.create(name="B")

    def _user(self, username, roles, brokerage, **extra):
        user = User.objects.create_user(username=username, password="x", **extra)
        user._test_roles = roles
        if brokerage is not None:
            set_test_brokerage(user, brokerage)
        return user

    def _as(self, user):
        cache.clear()
        self.client.force_authenticate(user)

    def _rows(self, resp):
        self.assertEqual(resp.status_code, 200, resp.content[:500])
        return resp.data["data"]


# ---------------------------------------------------------------------------
# A model with 'enabled': False still needs a tenancy declaration: it's still
# a FK / nested-field / filter target.
# ---------------------------------------------------------------------------


@override_settings(**TENANT)
class TestDisabledModelNeedsTenancy(SimpleTestCase):
    UNSCOPED = {"fields": ["id", "title"]}

    def test_enabled_unscoped_model_is_refused(self):
        with self.assertRaises(ImproperlyConfigured):
            _router((Deal, dict(self.UNSCOPED)))

    def test_disabled_unscoped_model_is_refused(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "declares no tenant"):
            _router((Deal, {"enabled": False, **self.UNSCOPED}))

    def test_disabled_shared_model_boots(self):
        router = _router((Deal, {"enabled": False, "tenancy": "shared"}))
        self.assertEqual(router.registry, [])


# ---------------------------------------------------------------------------
# A read rule on an FK holds from the other side: HoldingEvent.holding has a
# read rule only `analyst` holds (tests/settings.py), so `auditor` can't learn
# which holding an event points at through Holding's reverse `events`.
# ---------------------------------------------------------------------------


class TestReverseRelationHonoursFkReadRule(_Base):
    def setUp(self):
        super().setUp()
        self.auditor = self._user("auditor", ["auditor"], self.A)
        self.h1 = Holding.objects.create(name="Alpha", brokerage=self.A)
        self.h2 = Holding.objects.create(name="Beta", brokerage=self.A)
        self.e1 = HoldingEvent.objects.create(
            status="evt-one", brokerage=self.A, holding=self.h1
        )
        self.e2 = HoldingEvent.objects.create(
            status="evt-two", brokerage=self.A, holding=self.h2
        )

    def _config(self, **changes):
        config = Holding.turbodrf()
        config.update(changes)
        return mock.patch.object(
            Holding, "turbodrf", classmethod(lambda cls: dict(config))
        )

    def test_forward_side_is_hidden(self):
        self._as(self.auditor)
        rows = self._rows(self.client.get("/api/holdingevents/"))
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertNotIn("holding", row)

    def test_reverse_search_doesnt_single_out_the_holding(self):
        self._as(self.auditor)
        with mock.patch.object(
            Holding, "searchable_fields", ["events__status"], create=True
        ):
            r1 = self._rows(self.client.get("/api/holdings/?search=evt-one"))
            r2 = self._rows(self.client.get("/api/holdings/?search=evt-two"))
        self.assertEqual([r["id"] for r in r1], [r["id"] for r in r2])

    def test_bare_reverse_field_is_withheld(self):
        self._as(self.auditor)
        with self._config(fields=["id", "name", "brokerage", "events"]):
            rows = self._rows(self.client.get("/api/holdings/"))
        for row in rows:
            self.assertNotIn("events", row)

    def test_reverse_filter_is_ignored(self):
        self._as(self.auditor)
        with self._config(fields=["id", "name", "brokerage", "events__status"]):
            rows = self._rows(self.client.get("/api/holdings/?events__status=evt-one"))
        self.assertEqual(len(rows), 2)

    def test_property_reverse_hop_is_not_loaded(self):
        self._as(self.auditor)
        with (
            mock.patch.object(
                Holding,
                "event_statuses",
                property(lambda self: sorted(e.status for e in self.events.all())),
                create=True,
            ),
            self._config(
                fields=["id", "name", "event_statuses"],
                property_relations={"event_statuses": ["events"]},
            ),
        ):
            rows = self._rows(self.client.get("/api/holdings/"))
        for row in rows:
            self.assertNotIn(row.get("event_statuses"), (["evt-one"], ["evt-two"]))

    def test_analyst_with_the_grant_still_sees_it(self):
        analyst = self._user("analyst", ["analyst"], self.A)
        self._as(analyst)
        with self._config(fields=["id", "name", "brokerage", "events"]):
            rows = self._rows(self.client.get("/api/holdings/"))
        self.assertEqual(
            {r["id"]: r["events"] for r in rows},
            {self.h1.pk: [self.e1.pk], self.h2.pk: [self.e2.pk]},
        )

    def test_bare_reverse_field_needs_the_related_models_read(self):
        from django.conf import settings as dj_settings

        roles = {**dj_settings.TURBODRF_ROLES, "holder": ["test_app.holding.read"]}
        holder = self._user("holder", ["holder"], self.A)
        with (
            self.settings(TURBODRF_ROLES=roles),
            self._config(fields=["id", "name", "events"]),
        ):
            self._as(holder)
            self.assertEqual(self.client.get("/api/holdingevents/").status_code, 403)
            rows = self._rows(self.client.get("/api/holdings/"))
        for row in rows:
            self.assertNotIn("events", row)


# ---------------------------------------------------------------------------
# Inside an Either, an Owner over a __ path holds on its own: a user who sees
# a row only through the other branch can't create one on a colleague's parent
# or move a colleague's row onto their own.
# ---------------------------------------------------------------------------

BROKER = {
    "broker": [
        "test_app.transaction.read",
        "test_app.transaction.create",
        "test_app.transaction.update",
        "test_app.bankaccount.read",
    ]
}


class _Registry:
    """Swap one model's registered predicates (and tenant_field) for the
    block, as a different turbodrf() config would register them."""

    def __init__(self, model, predicates, tenant_field="keep"):
        self.model, self.predicates = model, predicates
        self.tenant_field = tenant_field

    def __enter__(self):
        from turbodrf.predicates import register_predicates, register_tenant_field

        self.cm = _preserve_registries()
        self.cm.__enter__()
        register_predicates(self.model, self.predicates)
        if self.tenant_field != "keep":
            register_tenant_field(self.model, self.tenant_field)

    def __exit__(self, *exc):
        self.cm.__exit__(*exc)


def _large_ones(request, roles):
    from django.db.models import Q

    return Q(amount__gte=1000)


@override_settings(TURBODRF_ROLES=BROKER)
class TestEitherOwnerPathWrites(_Base):
    def setUp(self):
        super().setUp()
        self.me = self._user("me", ["broker"], self.A)
        self.colleague = self._user("colleague", ["broker"], self.A)
        mine = Deal.objects.create(title="m", brokerage=self.A, assigned_broker=self.me)
        theirs = Deal.objects.create(
            title="t", brokerage=self.A, assigned_broker=self.colleague
        )
        self.my_account = BankAccount.objects.create(name="m", deal=mine)
        self.their_account = BankAccount.objects.create(name="t", deal=theirs)
        self.client.force_authenticate(self.me)

    def _either(self):
        from turbodrf.predicates import Custom, Either, Owner

        return _Registry(
            Transaction,
            [
                Either(
                    Owner("bank_account__deal__assigned_broker"),
                    Custom(_large_ones, write_validator=lambda d, i, r: []),
                )
            ],
        )

    def _theirs(self):
        return Transaction.objects.create(amount=5000, bank_account=self.their_account)

    def test_create_on_a_colleagues_account_is_refused(self):
        body = {"amount": "5000.00", "bank_account": self.their_account.pk}
        with self._either():
            resp = self.client.post("/api/transactions/", body, format="json")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(
            Transaction.objects.filter(bank_account=self.their_account).exists()
        )

    def test_create_on_my_account_works(self):
        body = {"amount": "5.00", "bank_account": self.my_account.pk}
        with self._either():
            resp = self.client.post("/api/transactions/", body, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_moving_a_colleagues_row_onto_mine_is_refused(self):
        row = self._theirs()
        with self._either():
            self.assertEqual(
                self.client.get(f"/api/transactions/{row.pk}/").status_code, 200
            )
            resp = self.client.patch(
                f"/api/transactions/{row.pk}/",
                {"bank_account": self.my_account.pk},
                format="json",
            )
        self.assertEqual(resp.status_code, 400, resp.content)
        row.refresh_from_db()
        self.assertEqual(row.bank_account_id, self.their_account.pk)

    def test_editing_other_fields_through_the_other_branch_works(self):
        row = self._theirs()
        with self._either():
            patch = self.client.patch(
                f"/api/transactions/{row.pk}/", {"amount": "6000.00"}, format="json"
            )
            put = self.client.put(
                f"/api/transactions/{row.pk}/",
                {"amount": "7000.00", "bank_account": self.their_account.pk},
                format="json",
            )
        self.assertEqual(patch.status_code, 200, patch.content)
        self.assertEqual(put.status_code, 200, put.content)


# ---------------------------------------------------------------------------
# A nested path through an FK with to_field is scoped on the column the FK
# stores (the to_field value), not the target's pk.
# ---------------------------------------------------------------------------


class TestToFieldTraversalScope(_Base):
    def setUp(self):
        from django.db.models import Q

        from tests.test_app.models import ParityAllTypes, ParityCurrency
        from turbodrf.predicates import Custom, _model_predicates

        super().setUp()
        self.reader = self._user("reader", ["parity_reader"], self.A)
        self.visible = ParityCurrency.objects.create(code="VIS", name="pub-visible")
        # Hidden by the rule; its code collides with the visible row's pk.
        self.hidden = ParityCurrency.objects.create(
            code=str(self.visible.pk), name="secret-hidden"
        )
        ParityAllTypes.objects.create(char="r1", currency=self.hidden)
        ParityAllTypes.objects.create(char="r2", currency=self.visible)

        rule = Custom(
            lambda request, roles: Q(name__startswith="pub"),
            write_validator=lambda d, i, r: [],
        )
        saved = _model_predicates.get(ParityCurrency)
        _model_predicates[ParityCurrency] = [rule]
        self.addCleanup(
            lambda: (
                _model_predicates.__setitem__(ParityCurrency, saved)
                if saved is not None
                else _model_predicates.pop(ParityCurrency, None)
            )
        )
        config = dict(ParityAllTypes.turbodrf())
        config["fields"] = ["id", "char", "currency", "currency__name"]
        patcher = mock.patch.object(
            ParityAllTypes, "turbodrf", classmethod(lambda cls: dict(config))
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_nested_value_follows_the_target_row(self):
        self._as(self.reader)
        rows = {
            r["char"]: r["currency_name"]
            for r in self._rows(self.client.get("/api/parityalltypess/"))
        }
        self.assertEqual(rows, {"r1": None, "r2": "pub-visible"})

    def test_search_doesnt_match_the_hidden_target(self):
        from tests.test_app.models import ParityAllTypes

        self._as(self.reader)
        with mock.patch.object(
            ParityAllTypes, "searchable_fields", ["currency__name"], create=True
        ):
            rows = self._rows(self.client.get("/api/parityalltypess/?search=secret"))
        self.assertEqual(rows, [])


# ---------------------------------------------------------------------------
# An FK filter naming another tenant's user / tenant answers the same as one
# naming nothing, even next to an invalid filter.
# ---------------------------------------------------------------------------


class TestFkFilterErrorOracle(_Base):
    def setUp(self):
        super().setUp()
        self.under_a = self._user("under_a", ["underwriter"], self.A)
        self.under_b = self._user("under_b", ["underwriter"], self.B)
        Deal.objects.create(title="A", brokerage=self.A, assigned_broker=self.under_a)

    def _same(self, exists_url, missing_url):
        self._as(self.under_a)
        exists = self.client.get(exists_url)
        missing = self.client.get(missing_url)
        self.assertEqual(exists.status_code, missing.status_code)
        self.assertEqual(exists.content, missing.content)

    def test_other_tenants_user(self):
        self._same(
            f"/api/deals/?assigned_broker={self.under_b.pk}&id=x",
            "/api/deals/?assigned_broker=987654&id=x",
        )

    def test_other_tenant(self):
        self._same(
            f"/api/deals/?brokerage={self.B.pk}&id=x",
            "/api/deals/?brokerage=987654&id=x",
        )

    def test_invalid_filter_alone_is_still_a_400(self):
        self._as(self.under_a)
        self.assertEqual(self.client.get("/api/deals/?id=x").status_code, 400)


# ---------------------------------------------------------------------------
# The model's default order (Meta.ordering) applies only where the caller may
# order by it: not on an unreadable column, and not through an FK (which
# sorts by the target's own order through an unscoped JOIN).
# ---------------------------------------------------------------------------


class TestDefaultOrdering(_Base):
    def test_unreadable_column_isnt_the_default_order(self):
        import uuid

        from tests.test_app.models import ParityUuidPk

        # pk order (high, mid, low) is the reverse of rank order.
        for n, (name, rank) in enumerate([("n-high", 99), ("n-mid", 50), ("n-low", 1)]):
            ParityUuidPk.objects.create(id=uuid.UUID(int=n + 1), name=name, rank=rank)
        by_pk = ["n-high", "n-mid", "n-low"]
        with mock.patch.object(ParityUuidPk, "sensitive_fields", ["rank"], create=True):
            cache.clear()
            rows = self._rows(self.client.get("/api/parityuuidpks/"))
        self.assertTrue(all("rank" not in r for r in rows))
        self.assertEqual([r["name"] for r in rows], by_pk)

    def test_readable_column_is_still_the_default_order(self):
        import uuid

        from tests.test_app.models import ParityUuidPk

        for name, rank in [("n-high", 99), ("n-low", 1), ("n-mid", 50)]:
            ParityUuidPk.objects.create(id=uuid.uuid4(), name=name, rank=rank)
        cache.clear()
        rows = self._rows(self.client.get("/api/parityuuidpks/"))
        self.assertEqual([r["name"] for r in rows], ["n-low", "n-mid", "n-high"])

    def test_fk_default_order_doesnt_follow_hidden_target_names(self):
        from tests.test_app.models import ScopeFolder, ScopeItem

        a1 = self._user("a1", ["scoper"], self.A)
        a2 = self._user("a2", ["scoper"], self.A)
        zzz = ScopeFolder.objects.create(name="zzz", brokerage=self.A, owner=a1)
        aaa = ScopeFolder.objects.create(name="aaa", brokerage=self.A, owner=a1)
        i1 = ScopeItem.objects.create(label="i1", brokerage=self.A, folder=zzz)
        i2 = ScopeItem.objects.create(label="i2", brokerage=self.A, folder=aaa)
        roles = {"scoper": ["test_app.scopefolder.read", "test_app.scopeitem.read"]}
        with (
            self.settings(TURBODRF_ROLES=roles),
            mock.patch.object(ScopeItem._meta, "ordering", ["folder"]),
        ):
            self._as(a2)
            first = [r["id"] for r in self._rows(self.client.get("/api/scopeitems/"))]
            ScopeFolder.objects.filter(pk=zzz.pk).update(name="000")
            second = [r["id"] for r in self._rows(self.client.get("/api/scopeitems/"))]
        # Ordered by the stored FK column: renaming a hidden folder changes nothing.
        self.assertEqual(first, second)
        self.assertEqual(
            sorted(first, key=lambda pk: {i1.pk: zzz.pk, i2.pk: aaa.pk}[pk]), first
        )

    def test_descending_order_breaks_ties_descending(self):
        from turbodrf.filter_backends import _with_pk_tiebreak

        self.assertEqual(_with_pk_tiebreak(Deal, ["-title"]), ["-title", "-pk"])
        self.assertEqual(_with_pk_tiebreak(Deal, ["title"]), ["title", "pk"])
        self.assertEqual(_with_pk_tiebreak(Deal, ["-id", "title"]), ["-id", "title"])


# ---------------------------------------------------------------------------
# A generic FK's target is checked like an FK's; a shared host written by a
# caller with a tenant gets the co-tenant check.
# ---------------------------------------------------------------------------


class TestGenericForeignKeyWrites(_Base):
    def _note_model(self):
        from django.contrib.contenttypes.fields import GenericForeignKey
        from django.contrib.contenttypes.models import ContentType
        from django.db import models

        class Note(models.Model):
            brokerage = models.ForeignKey(
                Brokerage, on_delete=models.CASCADE, related_name="+"
            )
            content_type = models.ForeignKey(
                ContentType, on_delete=models.CASCADE, related_name="+"
            )
            object_id = models.PositiveBigIntegerField()
            target = GenericForeignKey("content_type", "object_id")

            class Meta:
                app_label = "test_app"

        return Note

    def _errors(self, model, data):
        from rest_framework.test import APIRequestFactory

        from turbodrf.serializers import _check_fk_injection_writes

        request = APIRequestFactory().post("/")
        request.user = self.me
        return _check_fk_injection_writes(model, data, request, "brokerage")

    def test_other_tenants_row_is_refused(self):
        from django.contrib.contenttypes.models import ContentType
        from django.test.utils import isolate_apps

        self.me = self._user("me", ["underwriter"], self.A)
        theirs = Deal.objects.create(title="B secret", brokerage=self.B)
        mine = Deal.objects.create(title="A", brokerage=self.A, assigned_broker=self.me)
        deal_type = ContentType.objects.get_for_model(Deal)
        with isolate_apps("tests.test_app"):
            Note = self._note_model()
            refused = self._errors(
                Note, {"content_type": deal_type, "object_id": theirs.pk}
            )
            missing = self._errors(
                Note, {"content_type": deal_type, "object_id": 987654}
            )
            ok = self._errors(Note, {"content_type": deal_type, "object_id": mine.pk})
        self.assertEqual(list(refused), ["object_id"])
        self.assertEqual(refused, missing)
        self.assertEqual(ok, {})


@override_settings(
    TURBODRF_ROLES={"underwriter": ["test_app.deal.read", "test_app.deal.create"]}
)
class TestSharedHostCoTenant(_Base):
    def test_shared_host_cant_point_at_another_tenants_user(self):
        foreign = self._user("other", ["underwriter"], self.B)
        me = self._user("me", ["underwriter"], self.A)
        self._as(me)
        with _Registry(Deal, [], tenant_field=None):  # 'tenancy': 'shared'
            refused = self.client.post(
                "/api/deals/",
                {"title": "x", "assigned_broker": foreign.pk},
                format="json",
            )
            ok = self.client.post(
                "/api/deals/", {"title": "y", "assigned_broker": me.pk}, format="json"
            )
        self.assertEqual(refused.status_code, 400, refused.content)
        self.assertEqual(ok.status_code, 201, ok.content)


# ---------------------------------------------------------------------------
# A platform move can't strand rows of the old tenant pointing at the row.
# ---------------------------------------------------------------------------


@override_settings(
    ROOT_URLCONF="tests.platform_urls", TURBODRF_TENANT_MODEL="test_app.Brokerage"
)
class TestPlatformMoveWithReferences(_Base):
    def setUp(self):
        super().setUp()
        self.staff = self._user("staff", [], None, is_staff=True)
        self.staff._test_platform_roles = ["admin"]
        self.client.force_authenticate(self.staff)

    def _move(self, account):
        return self.client.patch(
            f"/console-api/accounts/{account.pk}/",
            {"brokerage": self.B.pk},
            format="json",
        )

    def test_move_with_referencing_rows_is_refused(self):
        from tests.test_app.models import PlatformAccount, PlatformTicket

        account = PlatformAccount.objects.create(brokerage=self.A, name="acc")
        PlatformTicket.objects.create(brokerage=self.A, subject="s", account=account)
        resp = self._move(account)
        self.assertEqual(resp.status_code, 400, resp.content)
        account.refresh_from_db()
        self.assertEqual(account.brokerage_id, self.A.pk)

    def test_move_without_references_works(self):
        from tests.test_app.models import PlatformAccount

        account = PlatformAccount.objects.create(brokerage=self.A, name="acc")
        resp = self._move(account)
        self.assertEqual(resp.status_code, 200, resp.content)
        account.refresh_from_db()
        self.assertEqual(account.brokerage_id, self.B.pk)


# ---------------------------------------------------------------------------
# A several-column owner_field: a create naming no owner is refused (it would
# make a row its writer can't see); naming yourself works.
# ---------------------------------------------------------------------------


@override_settings(
    TURBODRF_ROLES={
        "underwriter": ["test_app.rbactask.read", "test_app.rbactask.create"],
    }
)
class TestMultiColumnOwnerCreate(_Base):
    def test_unowned_create_is_refused(self):
        from tests.test_app.models import RbacTask

        self._as(self._user("me", ["underwriter"], self.A))
        resp = self.client.post("/api/rbactasks/", {"title": "x"}, format="json")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(RbacTask.objects.exists())

    def test_create_naming_yourself_works(self):
        me = self._user("me", ["underwriter"], self.A)
        self._as(me)
        resp = self.client.post(
            "/api/rbactasks/", {"title": "x", "reviewer": me.pk}, format="json"
        )
        self.assertEqual(resp.status_code, 201, resp.content)


# ---------------------------------------------------------------------------
# Database mode: a role deleted and recreated under its name, or permissions
# deleted in bulk, take effect on the next request.
# ---------------------------------------------------------------------------


@override_settings(
    TURBODRF_PERMISSION_MODE="database", TURBODRF_DISABLE_PERMISSIONS=False
)
class TestDatabaseRoleCache(TestCase):
    def setUp(self):
        cache.clear()

    def _rebuild(self, user, actions):
        from turbodrf.models import RolePermission, TurboDRFRole, UserRole

        TurboDRFRole.objects.filter(name="editor").delete()
        role = TurboDRFRole.objects.create(name="editor")
        for action in actions:
            RolePermission.objects.create(
                role=role, app_label="test_app", model_name="samplemodel", action=action
            )
        UserRole.objects.create(user=user, role=role)
        return role

    def test_recreated_role_isnt_served_from_the_old_snapshot(self):
        from tests.test_app.models import SampleModel
        from turbodrf.backends import build_permission_snapshot

        user = User.objects.create_user("ed")
        self._rebuild(user, ["read", "delete"])
        self.assertTrue(
            build_permission_snapshot(user, SampleModel).can_perform_action("delete")
        )
        self._rebuild(user, ["read", "update"])
        snapshot = build_permission_snapshot(user, SampleModel)
        self.assertFalse(snapshot.can_perform_action("delete"))
        self.assertTrue(snapshot.can_perform_action("update"))

    def test_bulk_delete_takes_effect(self):
        from tests.test_app.models import SampleModel
        from turbodrf.backends import build_permission_snapshot
        from turbodrf.models import RolePermission

        user = User.objects.create_user("ed")
        self._rebuild(user, ["read", "delete"])
        self.assertTrue(
            build_permission_snapshot(user, SampleModel).can_perform_action("delete")
        )
        RolePermission.objects.filter(action="delete").delete()
        self.assertFalse(
            build_permission_snapshot(user, SampleModel).can_perform_action("delete")
        )


class TestPermissionModeTypo(SimpleTestCase):
    @override_settings(TURBODRF_PERMISSION_MODE="Database")
    def test_unknown_mode_is_refused(self):
        from tests.test_app.models import SampleModel

        with self.assertRaisesMessage(ImproperlyConfigured, "TURBODRF_PERMISSION_MODE"):
            _router((SampleModel, SampleModel.turbodrf()))


# ---------------------------------------------------------------------------
# The platform router's API root runs its gate in the platform scope.
# ---------------------------------------------------------------------------


class _HasAnyRole:
    def has_permission(self, request, view):
        from turbodrf.backends import get_user_roles

        return bool(get_user_roles(request.user))

    def has_object_permission(self, request, view, obj):
        return True


@override_settings(TURBODRF_TENANT_MODEL="test_app.Brokerage")
class TestPlatformRootGateScope(TestCase):
    def test_tenant_roles_dont_pass_the_platform_root(self):
        from rest_framework.permissions import BasePermission
        from rest_framework.test import APIRequestFactory, force_authenticate

        from turbodrf.router import TurboDRFRouter

        gate = type("HasAnyRole", (_HasAnyRole, BasePermission), {})
        with _preserve_registries():
            router = TurboDRFRouter(
                scope="platform", gate=[gate], include_root_view=True
            )
        root = router.get_api_root_view(api_urls=router.get_urls())

        def call(user):
            request = APIRequestFactory().get("/console/")
            request.resolver_match = mock.Mock(namespace="")
            force_authenticate(request, user=user)
            return root(request).status_code

        tenant_only = User.objects.create_user("tenant_only")
        tenant_only._test_roles = ["viewer"]
        tenant_only._test_platform_roles = []
        staff = User.objects.create_user("staff")
        staff._test_roles = []
        staff._test_platform_roles = ["admin"]
        self.assertEqual(call(tenant_only), 403)
        self.assertEqual(call(staff), 200)


# ---------------------------------------------------------------------------
# Quality of life: a 403 says why (DEBUG only), an empty role map warns, a
# misspelt searchable field fails at startup, a route clash suggests a fix.
# ---------------------------------------------------------------------------


class TestQualityOfLife(_Base):
    def test_403_says_why_with_debug_on(self):
        from tests.test_app.models import SampleModel  # noqa: F401

        nobody = self._user("nobody", [], self.A)
        viewer = self._user("viewer", ["viewer"], self.A)
        with self.settings(DEBUG=True):
            self._as(nobody)
            no_roles = self.client.delete("/api/samplemodels/1/")
            self._as(viewer)
            no_grant = self.client.delete("/api/samplemodels/1/")
        self.assertEqual(no_roles.status_code, 403)
        self.assertIn("has no roles", str(no_roles.content))
        self.assertEqual(no_grant.status_code, 403)
        self.assertIn("test_app.samplemodel.delete", str(no_grant.content))

    def test_403_stays_generic_with_debug_off(self):
        viewer = self._user("viewer", ["viewer"], self.A)
        self._as(viewer)
        resp = self.client.delete("/api/samplemodels/1/")
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn("samplemodel", str(resp.content))

    def test_empty_roles_warn(self):
        from turbodrf.apps import check_roles_configured

        self.assertEqual(check_roles_configured(), [])
        with self.settings(TURBODRF_ROLES={}):
            self.assertEqual(
                [m.id for m in check_roles_configured()], ["turbodrf.W003"]
            )
            with self.settings(TURBODRF_PERMISSION_MODE="database"):
                self.assertEqual(check_roles_configured(), [])


class TestExplicitFieldsCheck(SimpleTestCase):
    def test_all_fields_warns(self):
        from turbodrf.apps import check_explicit_fields

        self.assertEqual(check_explicit_fields(), [])
        with mock.patch.object(
            Deal, "turbodrf", classmethod(lambda cls: {"tenant_field": "brokerage"})
        ):
            messages = check_explicit_fields()
        self.assertEqual([(m.id, m.obj) for m in messages], [("turbodrf.W004", Deal)])


@override_settings(**TENANT)
class TestStartupErrors(SimpleTestCase):
    def test_misspelt_searchable_field_is_refused(self):
        config = {"tenant_field": "brokerage", "fields": ["id", "title"]}
        with mock.patch.object(Deal, "searchable_fields", ["titel"], create=True):
            with self.assertRaisesMessage(ImproperlyConfigured, "'titel'"):
                _router((Deal, config))

    def test_route_clash_suggests_an_app_prefix(self):
        config = {"tenant_field": "brokerage", "fields": ["id"], "endpoint": "things"}
        holding = {"tenant_field": "brokerage", "fields": ["id"], "endpoint": "things"}
        with self.assertRaisesMessage(ImproperlyConfigured, "'endpoint': 'test_app/"):
            _router((Deal, config), (Holding, holding))


# ---------------------------------------------------------------------------
# TURBODRF_ROLE_GETTER and TURBODRF_PAGINATION_CLASS.
# ---------------------------------------------------------------------------


class SmallPages(PageNumberPagination):
    page_size = 1


class TestRoleGetter(_Base):
    @override_settings(TURBODRF_ROLE_GETTER="turbodrf.backends.group_names")
    def test_group_names_getter(self):
        from django.contrib.auth.models import Group

        member = User.objects.create_user("member")
        member.groups.add(Group.objects.create(name="viewer"))
        outsider = User.objects.create_user("outsider")
        self._as(member)
        self.assertEqual(self.client.get("/api/samplemodels/").status_code, 200)
        self._as(outsider)
        self.assertEqual(self.client.get("/api/samplemodels/").status_code, 403)

    @override_settings(TURBODRF_ROLE_GETTER="turbodrf.backends.group_names")
    def test_role_only_identity_keeps_its_roles(self):
        from turbodrf.backends import RoleOnlyIdentity, get_user_roles

        self.assertEqual(get_user_roles(RoleOnlyIdentity(["viewer"])), ["viewer"])

    def test_bad_getter_fails_at_startup(self):
        from tests.test_app.models import SampleModel

        with self.settings(TURBODRF_ROLE_GETTER="nowhere.at_all"):
            with self.assertRaisesMessage(ImproperlyConfigured, "TURBODRF_ROLE_GETTER"):
                _router((SampleModel, SampleModel.turbodrf()))


class TestPaginationClass(_Base):
    @override_settings(
        TURBODRF_PAGINATION_CLASS="tests.integration.test_audit_084.SmallPages"
    )
    def test_own_pagination_class(self):
        from tests.test_app.models import RelatedModel, SampleModel

        related = RelatedModel.objects.create(name="r")
        for title in ("a", "b"):
            SampleModel.objects.create(title=title, price=1, related=related)
        self._as(self._user("viewer", ["viewer"], self.A))
        body = self.client.get("/api/samplemodels/").json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(len(body["results"]), 1)
        options = self.client.options("/api/samplemodels/").json()
        self.assertEqual(options["pagination"]["default_page_size"], 1)

    def test_default_envelope(self):
        self._as(self._user("viewer", ["viewer"], self.A))
        body = self.client.get("/api/samplemodels/").json()
        self.assertIn("pagination", body)
        self.assertIn("data", body)
