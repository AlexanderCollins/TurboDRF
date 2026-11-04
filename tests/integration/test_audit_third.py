"""Third audit (0.8.5): each test sends the attack and asserts the secure
behaviour, so it failed before the fix it names."""

import base64
import datetime
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.db import models as dj_models
from django.db.models import Q
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext, isolate_apps
from django.urls import include, path
from rest_framework.pagination import CursorPagination
from rest_framework.test import APIClient

from tests.integration.test_audit_084 import _Base
from tests.integration.test_audit_085 import _patch, _patch_config
from tests.integration.test_review_row_scope import SCOPE_ROLES
from tests.test_app.models import (
    Deal,
    ParityAllTypes,
    ParityCurrency,
    ParityTarget,
    PlatformAccount,
    PlatformTicket,
    RelatedModel,
    SampleModel,
    ScopeFolder,
    ScopeTag,
    ScopeTagged,
)

urlpatterns = []  # a freshly built router, per test


def _hide_alltypes_not_starting_with_pub():
    from turbodrf import predicates
    from turbodrf.predicates import Custom

    rule = Custom(
        lambda request, roles: Q(char__startswith="pub"),
        write_validator=lambda d, i, r: [],
    )
    return mock.patch.dict(predicates._model_predicates, {ParityAllTypes: [rule]})


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


class TestIsnullThroughARelation(_Base):
    """`isnull=true` through a relation told a related row the caller can't
    see from no row: it's accepted on the model's own fields only."""

    def test_to_many(self):
        me = self._user("me", ["account_admin"], self.A)
        colleague = self._user("colleague", ["account_admin"], self.A)
        hidden = PlatformAccount.objects.create(name="hidden", brokerage=self.A)
        PlatformAccount.objects.create(name="none", brokerage=self.A)
        PlatformTicket.objects.create(
            brokerage=self.A, account=hidden, assignee=colleague, subject="s"
        )
        _patch_config(
            self, PlatformAccount, fields=["id", "name", "plan", "tickets__subject"]
        )
        self._as(me)
        url = "/api/platform-accounts/?tickets__subject__isnull=true"
        names = {r["name"] for r in self._rows(self.client.get(url))}
        self.assertEqual(names, {"hidden", "none"})

    @override_settings(TURBODRF_ROLES=SCOPE_ROLES)
    def test_through_a_hidden_single_valued_hop(self):
        owner = self._user("owner", ["scoper"], self.A)
        me = self._user("me", ["scoper"], self.A)
        private = ScopeFolder.objects.create(
            name="private", brokerage=self.A, owner=owner
        )
        in_private = ScopeTagged.objects.create(title="in-private", brokerage=self.A)
        in_private.tags.add(
            ScopeTag.objects.create(name="t1", brokerage=self.A, folder=private)
        )
        no_folder = ScopeTagged.objects.create(title="no-folder", brokerage=self.A)
        no_folder.tags.add(ScopeTag.objects.create(name="t2", brokerage=self.A))
        self._as(me)
        url = "/api/scopetaggeds/?tags__folder__name__isnull=true"
        titles = {r["title"] for r in self._rows(self.client.get(url))}
        self.assertEqual(titles, {"in-private", "no-folder"})


class TestReverseOneToOneBaseFilter(_Base):
    """A configured `twin_row__char` made its base `twin_row` a filter with no
    scope (and a 500 for `?twin_row=1`)."""

    def setUp(self):
        super().setUp()
        patcher = _hide_alltypes_not_starting_with_pub()
        patcher.start()
        self.addCleanup(patcher.stop)
        twin = ParityTarget.objects.create(name="hidden-twin")
        ParityTarget.objects.create(name="no-twin")
        ParityAllTypes.objects.create(char="hidden-row", twin=twin)
        _patch_config(self, ParityTarget, fields=["id", "name", "twin_row__char"])
        self._as(self._user("reader", ["parity_reader"], self.A))

    def test_isnull_is_ignored(self):
        rows = self._rows(self.client.get("/api/paritytargets/?twin_row__isnull=false"))
        self.assertEqual(len(rows), 2)

    def test_exact_is_ignored_not_a_500(self):
        rows = self._rows(self.client.get("/api/paritytargets/?twin_row=1"))
        self.assertEqual(len(rows), 2)


class TestFkFilterOnAnotherTenant(_Base):
    """The tenant model has no row rules: another tenant's pk was valid (one
    more query than a missing pk, a timing oracle). It now fails like a
    missing one, as a written FK value does. (So does another tenant's user,
    where TURBODRF_TENANT_USER_FIELD is a column; the test app's is a
    property.)"""

    def setUp(self):
        super().setUp()
        self.me = self._user("me", ["manager"], self.A)
        Deal.objects.create(title="mine", brokerage=self.A, assigned_broker=self.me)
        self._as(self.me)
        self.client.get("/api/deals/")  # warm caches

    def _queries(self, url):
        with CaptureQueriesContext(connection) as ctx:
            rows = self._rows(self.client.get(url))
        return rows, len(ctx.captured_queries)

    def test_tenant(self):
        missing = self.B.pk + 1000
        theirs = self._queries(f"/api/deals/?brokerage={self.B.pk}")
        self.assertEqual(theirs, self._queries(f"/api/deals/?brokerage={missing}"))
        mine = self._rows(self.client.get(f"/api/deals/?brokerage={self.A.pk}"))
        self.assertEqual(len(mine), 1)


class TestTextFiltersBoundedTogether(_Base):
    """Each LIKE costs its pattern's length: a request's text-filter values
    are bounded together (200 characters by default), as search's terms."""

    def setUp(self):
        super().setUp()
        _patch_config(self, ParityAllTypes, fields=["id", "char", "text"])
        ParityAllTypes.objects.create(char="r", text="a" * 400)
        self._as(self._user("reader", ["parity_reader"], self.A))

    def _matches(self, *params):
        url = "/api/parityalltypess/?" + "&".join(params)
        return len(self._rows(self.client.get(url)))

    def test_over_the_bound_matches_nothing(self):
        a120, a90 = "a" * 120, "a" * 90
        self.assertEqual(
            self._matches(f"text__contains={a120}", f"text__icontains_or={a90}"), 0
        )
        self.assertEqual(
            self._matches(f"text__contains_or={a120}", f"text__contains_or={a90}"), 0
        )

    def test_under_it_matches(self):
        a100, a90 = "a" * 100, "a" * 90
        self.assertEqual(
            self._matches(f"text__contains={a100}", f"text__icontains_or={a90}"), 1
        )


class TestYearOutOfRange(_Base):
    """Django builds a date from a `__year` value: one out of range was a 500."""

    def test_is_a_400(self):
        SampleModel.objects.create(
            title="t", price=1, related=RelatedModel.objects.create(name="r")
        )
        self._as(self._user("viewer", ["viewer"], self.A))
        for query in (
            "created_at__year=99999",  # django-filter's filter
            "created_at__year=0",
            "created_at__year__gt=99999",  # the OR backend's
            "created_at__year_or=10000",
        ):
            resp = self.client.get(f"/api/samplemodels/?{query}")
            self.assertEqual(resp.status_code, 400, query)
        resp = self.client.get("/api/samplemodels/?created_at__year=2000")
        self.assertEqual(resp.status_code, 200)


class CreatedCursor(CursorPagination):
    ordering = "-created_at"
    page_size = 1


@override_settings(
    TURBODRF_PAGINATION_CLASS="tests.integration.test_audit_third.CreatedCursor"
)
class TestCursorOrdering(_Base):
    """A cursor's links carry its ordering column's values: it orders only by
    a column the caller may order by."""

    def setUp(self):
        super().setUp()
        roles = {
            **settings.TURBODRF_ROLES,
            "blind": ["test_app.samplemodel.read", "test_app.samplemodel.title.read"],
        }
        patcher = override_settings(TURBODRF_ROLES=roles)
        patcher.enable()
        self.addCleanup(patcher.disable)
        related = RelatedModel.objects.create(name="r")
        self.rows = [
            SampleModel.objects.create(title=f"t{i}", price=1, related=related)
            for i in range(2)
        ]

    def _cursor(self):
        resp = self.client.get("/api/samplemodels/")
        self.assertEqual(resp.status_code, 200)
        cursor = parse_qs(urlparse(resp.data["next"]).query)["cursor"][0]
        return parse_qs(base64.b64decode(cursor.encode()).decode())["p"][0]

    def test_without_read_on_the_column(self):
        self._as(self._user("blind", ["blind"], self.A))
        self.assertNotIn(self._cursor(), {str(r.created_at) for r in self.rows})

    def test_with_read_on_it(self):
        self._as(self._user("admin", ["admin"], self.A))
        self.assertIn(self._cursor(), {str(r.created_at) for r in self.rows})


# ---------------------------------------------------------------------------
# Property sandbox
# ---------------------------------------------------------------------------


class TestSandboxToFieldValue(_Base):
    """An FK with to_field holds the target's column: a property reading it
    needs that column's read, as the FK itself does (0.8.5)."""

    def setUp(self):
        super().setUp()
        usd = ParityCurrency.objects.create(code="USD", name="Dollar")
        ParityAllTypes.objects.create(char="r1", currency=usd)
        _patch(self, ParityCurrency, "sensitive_fields", ["code"], create=True)
        _patch(
            self,
            ParityAllTypes,
            "currency_raw",
            property(lambda s: s.currency_id),
            create=True,
        )
        _patch(
            self,
            ParityAllTypes,
            "currency_hop",
            property(lambda s: s.currency.code if s.currency else None),
            create=True,
        )
        _patch_config(
            self,
            ParityAllTypes,
            fields=["id", "char", "currency", "currency_raw", "currency_hop"],
            property_relations={"currency_hop": ["currency"]},
        )
        self._as(self._user("lim", ["parity_limited"], self.A))

    def test_neither_property_gets_it(self):
        (row,) = self._rows(self.client.get("/api/parityalltypess/"))
        self.assertNotIn("currency", row)
        self.assertNotEqual(row.get("currency_raw"), "USD")
        self.assertNotEqual(row.get("currency_hop"), "USD")


@override_settings(TURBODRF_ROLES=SCOPE_ROLES)
class TestSandboxHopManager(_Base):
    """A to-many hop's rows are chosen by the target's default manager but
    loaded without its select_related / annotations, which brought in a row
    (or a value of one) the caller can't see."""

    def setUp(self):
        super().setUp()
        self.a1 = self._user("a1", ["scoper"], self.A)
        self.a2 = self._user("a2", ["scoper"], self.A)
        folder = ScopeFolder.objects.create(
            name="A1-private-plan",
            date=datetime.date(2024, 3, 15),
            brokerage=self.A,
            owner=self.a1,
        )
        tag = ScopeTag.objects.create(name="t", brokerage=self.A, folder=folder)
        row = ScopeTagged.objects.create(title="x", brokerage=self.A)
        row.tags.add(tag)

    def _manager(self, change):
        manager = ScopeTag._default_manager
        _patch(
            self,
            manager,
            "get_queryset",
            lambda: change(dj_models.QuerySet(ScopeTag, using=manager._db)),
        )

    def _property(self, read):
        _patch(
            self,
            ScopeTagged,
            "tag_folders",
            property(lambda s: [read(t) for t in s.tags.all()]),
            create=True,
        )

    def _tag_folders(self, paths, user=None):
        _patch_config(
            self,
            ScopeTagged,
            fields=["id", "title", "tag_folders"],
            property_relations={"tag_folders": paths},
        )
        self._as(user or self.a2)
        (row,) = self._rows(self.client.get("/api/scopetaggeds/"))
        return row.get("tag_folders") or []

    def _folder_name(self, tag):
        return tag.folder.name if tag.folder else None

    def test_declared_hop_with_a_select_related_manager(self):
        self._manager(lambda qs: qs.select_related("folder"))
        self._property(self._folder_name)
        self.assertEqual(self._tag_folders(["tags", "tags__folder"]), [None])

    def test_undeclared_hop_with_a_select_related_manager(self):
        self._manager(lambda qs: qs.select_related("folder"))
        self._property(self._folder_name)
        self.assertEqual(self._tag_folders(["tags"]), [])  # the query is blocked

    def test_unreadable_column_through_a_select_related_manager(self):
        self._manager(lambda qs: qs.select_related("folder"))
        self._property(self._folder_name)
        _patch(self, ScopeFolder, "sensitive_fields", ["name"], create=True)
        self.assertNotIn("A1-private-plan", self._tag_folders(["tags"], self.a1))

    def test_a_manager_annotation(self):
        self._manager(lambda qs: qs.annotate(folder_title=dj_models.F("folder__name")))
        self._property(lambda t: getattr(t, "folder_title", None))
        self.assertEqual(self._tag_folders(["tags"]), [None])


# ---------------------------------------------------------------------------
# Startup checks
# ---------------------------------------------------------------------------


class TestM2MThroughModelWithRowRules(SimpleTestCase):
    """An M2M read the link rows without its through model's row rules: a
    path across it is refused (the through model's reverse FK keeps them)."""

    def test_refused(self):
        from turbodrf import predicates
        from turbodrf.predicates import Conditional
        from turbodrf.router import _validate_through_rules

        with isolate_apps("tests.test_app"):

            class Person(dj_models.Model):
                username = dj_models.CharField(max_length=10)

                class Meta:
                    app_label = "test_app"

            class Team(dj_models.Model):
                members = dj_models.ManyToManyField(Person, through="Link")

                class Meta:
                    app_label = "test_app"

            class Link(dj_models.Model):
                team = dj_models.ForeignKey(Team, on_delete=dj_models.CASCADE)
                user = dj_models.ForeignKey(Person, on_delete=dj_models.CASCADE)
                confidential = dj_models.BooleanField(default=False)

                class Meta:
                    app_label = "test_app"

            rule = Conditional(when=Q(confidential=True), require_roles=["hr"])
            configs = (
                {"fields": ["id", "members"]},
                {"fields": {"list": ["id"], "detail": ["members__username"]}},
                {"fields": "__all__"},
                {"fields": ["id"], "property_relations": {"p": ["members"]}},
            )
            with mock.patch.dict(predicates._model_predicates, {Link: [rule]}):
                for config in configs:
                    with self.assertRaises(ImproperlyConfigured, msg=config):
                        _validate_through_rules(Team, config)
                _validate_through_rules(Team, {"fields": ["id", "link__user"]})
                # From the other side too.
                with self.assertRaises(ImproperlyConfigured):
                    _validate_through_rules(Person, {"fields": ["team__id"]})
            # No row rules on the through model: nothing to bypass.
            _validate_through_rules(Team, {"fields": ["id", "members"]})


def _router(models_):
    from tests.fixtures import router_apps
    from turbodrf.router import TurboDRFRouter

    with mock.patch("turbodrf.router.apps", router_apps(models_)):
        return TurboDRFRouter()


class TestInheritedConfig(SimpleTestCase):
    """A proxy or multi-table child of a TurboDRF model was served at its own
    endpoint with the parent's public_access and none of its field rules."""

    def test_proxy(self):
        with isolate_apps("tests.test_app"):

            class ArchivedSample(SampleModel):
                class Meta:
                    proxy = True
                    app_label = "test_app"

            with self.assertRaisesMessage(ImproperlyConfigured, "ArchivedSample"):
                _router([RelatedModel, SampleModel, ArchivedSample])

            class OwnSample(SampleModel):
                class Meta:
                    proxy = True
                    app_label = "test_app"

                @classmethod
                def turbodrf(cls):
                    return {"enabled": False, "tenancy": "shared"}

            _router([RelatedModel, SampleModel, OwnSample])

    def test_multi_table_child(self):
        with isolate_apps("tests.test_app"):

            class PremiumSample(SampleModel):
                tier = dj_models.CharField(max_length=10)

                class Meta:
                    app_label = "test_app"

            with self.assertRaisesMessage(ImproperlyConfigured, "PremiumSample"):
                _router([RelatedModel, SampleModel, PremiumSample])


@override_settings(ROOT_URLCONF=__name__)
class TestMappingRoles(TestCase):
    """A role source returning a mapping held every key, whatever its value."""

    def test_refused(self):
        from django.contrib.auth import get_user_model

        router = _router([RelatedModel, SampleModel])
        urlpatterns[:] = [path("api/", include(router.urls))]
        self.addCleanup(urlpatterns.clear)
        row = SampleModel.objects.create(
            title="t", price=1, related=RelatedModel.objects.create(name="r")
        )
        user = get_user_model().objects.create_user("m")
        user._test_roles = {"admin": False, "viewer": True}
        client = APIClient()
        client.force_authenticate(user)
        with self.assertRaisesMessage(ImproperlyConfigured, "mapping"):
            client.get(f"/api/samplemodels/{row.pk}/")


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _serve(test, *models_):
    """This module's URLconf: a router that sees only ``models_``."""
    urlpatterns[:] = [path("api/", include(_router(list(models_)).urls))]
    test.addCleanup(urlpatterns.clear)


def _register(test, model, config):
    from tests.integration.test_register import registered

    cm = registered(model, config)
    cm.__enter__()
    test.addCleanup(cm.__exit__, None, None, None)


def _no_write_rule(data, instance, request):
    return []


def _partner_a(request, roles):
    return Q(partner_of__name="a")


LEAF_ROLES = {
    role: [
        "test_app.parityleaf.read",
        "test_app.parityleaf.create",
        "test_app.parityleaf.update",
    ]
    for role in ("clerk", "boss")
}


@override_settings(ROOT_URLCONF=__name__, TURBODRF_ROLES=LEAF_ROLES)
class TestPostWriteCheckRewrite(_Base):
    """The post-write check ran the rules as a plain filter, Django's
    split_exclude path the read scope avoids (0.8.5): a Conditional's write
    check passed a write that hid the row from its writer."""

    def test_a_clerk_cant_move_a_leaf_into_the_boss_only_set(self):
        from tests.test_app.models import ParityLeaf
        from turbodrf.predicates import Conditional, Custom

        _register(
            self,
            ParityLeaf,
            {
                "endpoint": "leaves",
                "fields": ["id", "name"],
                "visibility": [
                    Custom(_partner_a, write_validator=_no_write_rule),
                    Conditional(
                        when=Q(
                            name="x",
                            paritytarget__name="hidden",
                            paritytarget__amount=5,
                        ),
                        require_roles=["boss"],
                    ),
                ],
            },
        )
        _serve(self, ParityLeaf)
        leaf = ParityLeaf.objects.create(name="y")
        ParityTarget.objects.create(name="a", partner=leaf)
        ParityTarget.objects.create(name="hidden", amount=5, leaf=leaf)
        self._as(self._user("clerk", ["clerk"], self.A))
        url = f"/api/leaves/{leaf.pk}/"
        patch = self.client.patch(url, {"name": "x"}, format="json")
        self.assertEqual(patch.status_code, 400, patch.content)
        self.assertEqual(self.client.get(url).status_code, 200)
        leaf.refresh_from_db()
        self.assertEqual(leaf.name, "y")


@override_settings(
    ROOT_URLCONF=__name__,
    TURBODRF_ROLES={
        "clerk": [
            "test_app.ledgerinvoice.read",
            "test_app.ledgerinvoice.create",
            "test_app.ledgerinvoice.update",
            "test_app.scopeledger.read",
        ]
    },
)
class TestPlainHopOnATenantPath(_Base):
    """ScopeLedger is a plain model on ScopeEntry's tenant path
    ('ledger__firm'): its rows are tenant data, scoped by that path wherever
    the API reaches them. Another model's FK could name another tenant's."""

    def setUp(self):
        super().setUp()
        from tests.test_app.models import LedgerInvoice, ScopeLedger

        self.Invoice = LedgerInvoice
        _register(
            self,
            LedgerInvoice,
            {
                "endpoint": "invoices",
                "tenant_field": "brokerage",
                "fields": ["id", "memo", "brokerage", "ledger", "ledger__name"],
            },
        )
        _serve(self, LedgerInvoice)
        self.theirs = ScopeLedger.objects.create(name="B payroll", firm=self.B)
        self.mine = ScopeLedger.objects.create(name="A payroll", firm=self.A)
        self._as(self._user("clerk", ["clerk"], self.A))

    def test_another_tenants_row_is_refused(self):
        resp = self.client.post(
            "/api/invoices/", {"memo": "x", "ledger": self.theirs.pk}, format="json"
        )
        missing = self.client.post(
            "/api/invoices/", {"memo": "x", "ledger": 987654}, format="json"
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.content, missing.content)
        ok = self.client.post(
            "/api/invoices/", {"memo": "x", "ledger": self.mine.pk}, format="json"
        )
        self.assertEqual(ok.status_code, 201, ok.content)

    def test_nor_rendered_through_a_row_pointing_at_it(self):
        row = self.Invoice.objects.create(
            memo="m", brokerage=self.A, ledger=self.theirs
        )
        body = self.client.get(f"/api/invoices/{row.pk}/").content.decode()
        self.assertNotIn("B payroll", body)


@override_settings(
    TURBODRF_ROLES={
        "broker": ["test_app.deal.read", "test_app.deal.create", "test_app.deal.update"]
    }
)
class TestTenantModelWithoutTheSetting(_Base):
    """Without TURBODRF_TENANT_MODEL, a model with no tenant_field took any
    tenant in its FK to the tenant model: the tenant model is the one every
    tenant_field ends on."""

    def test_refused(self):
        from django.urls import resolve

        from tests.integration.test_audit_084 import _preserve_registries
        from turbodrf.predicates import (
            Owner,
            register_predicates,
            register_tenant_field,
        )

        rules = [Owner("assigned_broker")]
        self._as(self._user("me", ["broker"], self.A))
        viewset = resolve("/api/deals/1/").func.cls
        with (
            _preserve_registries(),
            mock.patch.object(viewset, "_predicates", rules, create=True),
            mock.patch.object(viewset, "_tenant_field", None, create=True),
        ):
            register_predicates(Deal, rules)
            register_tenant_field(Deal, None)
            resp = self.client.post(
                "/api/deals/", {"title": "x", "brokerage": self.B.pk}, format="json"
            )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(Deal.objects.filter(brokerage=self.B).exists())


def _published(request, roles):
    return Q(title__startswith="pub")


@override_settings(
    TURBODRF_ROLES={
        "writer": [
            "test_app.scopemultiowner.read",
            "test_app.scopemultiowner.create",
            "test_app.scopemultiowner.update",
        ]
    }
)
class TestSeveralColumnOwnerInsideEither(_Base):
    """A create must name the caller in one of a several-column Owner's
    columns, inside an Either too."""

    def test_refused(self):
        from django.urls import resolve

        from tests.integration.test_audit_084 import _preserve_registries
        from tests.test_app.models import ScopeMultiOwner
        from turbodrf.predicates import Custom, Either, Owner, register_predicates

        rules = [
            Either(
                Owner(["owner", "editor"]),
                Custom(_published, write_validator=_no_write_rule),
            )
        ]
        me = self._user("w", ["writer"], self.A)
        self._as(me)
        viewset = resolve("/api/scopemultiowners/1/").func.cls
        with (
            _preserve_registries(),
            mock.patch.object(viewset, "_predicates", rules, create=True),
        ):
            register_predicates(ScopeMultiOwner, rules)
            url = "/api/scopemultiowners/"
            resp = self.client.post(url, {"title": "pub-1"}, format="json")
            self.assertEqual(resp.status_code, 400, resp.content)
            data = {"title": "pub-2", "editor": me.pk}
            resp = self.client.post(url, data, format="json")
            self.assertEqual(resp.status_code, 201, resp.content)


@override_settings(
    ROOT_URLCONF=__name__,
    TURBODRF_ROLES={
        "boarder": ["test_app.scopeboard.read", "test_app.scopeboard.update"]
    },
)
class TestM2MWriteKeepsOtherTenantsUsers(_Base):
    """With TURBODRF_TENANT_USER_FIELD a property, another tenant's user
    linked by back-office code was dropped by a write that couldn't name it."""

    def test_kept(self):
        from tests.integration.test_audit_084 import _preserve_registries
        from tests.test_app.models import ScopeBoard

        _patch_config(
            self,
            ScopeBoard,
            read_only=False,
            fields=["id", "title", "owner", "watchers"],
        )
        with _preserve_registries():
            _serve(self, ScopeBoard)
        me = self._user("me", ["boarder"], self.A)
        theirs = self._user("their_user", ["boarder"], self.B)
        board = ScopeBoard.objects.create(title="t", brokerage=self.A, owner=me)
        board.watchers.set([me, theirs])
        self._as(me)
        url = f"/api/scopeboards/{board.pk}/"
        refused = self.client.patch(
            url, {"watchers": [me.pk, theirs.pk]}, format="json"
        )
        self.assertEqual(refused.status_code, 400)
        resp = self.client.patch(url, {"watchers": [me.pk]}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn(theirs, board.watchers.all())


# ---------------------------------------------------------------------------
# HTML forms
# ---------------------------------------------------------------------------


class TestHtmlUserChoices(_Base):
    """With TURBODRF_TENANT_USER_FIELD a property, every HTML page read every
    tenant's users (three times) to narrow a form's user choices: it offers
    none instead."""

    def test_no_scan(self):
        member = self._user("member", ["underwriter"], self.A)
        for i in range(5):
            self._user(f"other_{i}", ["underwriter"], self.B)
        Deal.objects.create(title="mine", brokerage=self.A, assigned_broker=member)
        self._as(member)
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get("/api/deals/?format=api")
        self.assertEqual(resp.status_code, 200)
        scans = [
            q["sql"]
            for q in ctx.captured_queries
            if 'FROM "auth_user"' in q["sql"] and " WHERE " not in q["sql"]
        ]
        self.assertEqual(scans, [])
        self.assertNotIn("other_0", resp.content.decode())


# ---------------------------------------------------------------------------
# More startup checks and request bounds
# ---------------------------------------------------------------------------


class TestPathsThatOnlyFail(SimpleTestCase):
    """A generic FK, or a nested path ending on a to-many relation, rendered
    the related row unscoped or a manager: a 500 on every request."""

    def test_refused(self):
        from turbodrf.router import _field_path_resolves

        self.assertFalse(_field_path_resolves(ScopeTagged, "tags__folder__tags"))
        self.assertFalse(_field_path_resolves(ScopeTag, "folder__tags"))
        self.assertTrue(_field_path_resolves(ScopeTagged, "tags"))
        self.assertTrue(_field_path_resolves(ScopeTagged, "tags__folder__name"))
        self.assertTrue(_field_path_resolves(ScopeTag, "folder"))

    def test_generic_fk(self):
        from django.contrib.contenttypes.fields import GenericForeignKey
        from django.contrib.contenttypes.models import ContentType

        from turbodrf.router import _field_path_resolves

        with isolate_apps("tests.test_app"):

            class Note(dj_models.Model):
                content_type = dj_models.ForeignKey(
                    ContentType, on_delete=dj_models.CASCADE
                )
                object_id = dj_models.PositiveIntegerField()
                content_object = GenericForeignKey()

                class Meta:
                    app_label = "test_app"

            self.assertFalse(_field_path_resolves(Note, "content_object"))
            self.assertTrue(_field_path_resolves(Note, "object_id"))


class TestFilterValues(_Base):
    def setUp(self):
        super().setUp()
        self._as(self._user("mgr", ["manager"], self.A))
        self.client.raise_request_exception = False

    def test_nul_is_a_400(self):
        """A NUL reached the database (a 500 on PostgreSQL)."""
        for query in (
            "title_or=%00",
            "title__contains=%00",
            "title__startswith=a%00",
            "title__in=a,%00",
            "title__iexact=%00",
        ):
            resp = self.client.get(f"/api/deals/?{query}")
            self.assertEqual(resp.status_code, 400, query)

    def test_a_repeated_parameter_is_bounded(self):
        """django-filter ORs a repeated parameter's values: as many as an
        `_or` key takes (a deep OR tree is a 500 on SQLite)."""
        Deal.objects.create(title="d", brokerage=self.A)
        many = "&".join(f"title_or=d{i}" for i in range(19)) + "&title_or=d"
        self.assertEqual(len(self._rows(self.client.get(f"/api/deals/?{many}"))), 1)
        more = "&".join(f"brokerage={self.A.pk}" for _ in range(21))
        self.assertEqual(self._rows(self.client.get(f"/api/deals/?{more}")), [])
