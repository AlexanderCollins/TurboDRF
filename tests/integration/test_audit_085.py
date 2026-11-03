"""Regressions for the 0.8.5 audit. Each test sends the attack that worked on
0.8.4 and asserts it no longer does."""

import json
from decimal import Decimal
from unittest import mock, skipUnless

from django.core.exceptions import ImproperlyConfigured
from django.db.models import Q
from django.test import SimpleTestCase, override_settings
from rest_framework.pagination import PageNumberPagination

from tests.integration.test_audit_084 import (
    TENANT,
    _Base,
    _preserve_registries,
    _router,
)
from tests.test_app.models import (
    BankAccount,
    Brokerage,
    Deal,
    Holding,
    ParityAllTypes,
    ParityCurrency,
    ParityTag,
    ParityTarget,
    Portfolio,
    ScopeLedger,
    Transaction,
)


class StdPages(PageNumberPagination):
    page_size = 20


def _patch(test, *args, **kwargs):
    patcher = mock.patch.object(*args, **kwargs)
    patcher.start()
    test.addCleanup(patcher.stop)


def _patch_config(test, model, **changes):
    config = dict(model.turbodrf())
    config.update(changes)
    _patch(test, model, "turbodrf", classmethod(lambda cls: dict(config)))


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@override_settings(
    TURBODRF_PAGINATION_CLASS="tests.integration.test_audit_085.StdPages"
)
class TestNoBrowsableFilterForm(_Base):
    def test_other_tenants_and_users_arent_listed(self):
        me = self._user("me", ["manager"], self.A)
        self._user("victimuserxyz", ["underwriter"], self.B)
        Brokerage.objects.create(name="VictimCoXYZ")
        Deal.objects.create(title="mine", brokerage=self.A, assigned_broker=me)
        self._as(me)
        html = self.client.get("/api/deals/?format=api").content.decode()
        self.assertNotIn("VictimCoXYZ", html)
        self.assertNotIn("victimuserxyz", html)


class TestFilterAndSearchBounds(_Base):
    def test_many_or_values_match_nothing_not_a_500(self):
        me = self._user("me", ["manager"], self.A)
        Deal.objects.create(title="v1", brokerage=self.A)
        self._as(me)
        query = "&".join(f"title_or=v{i}" for i in range(999))
        resp = self.client.get(f"/api/deals/?{query}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["data"], [])

    def test_search_terms_are_bounded_together(self):
        from tests.test_app.models import RelatedModel, SampleModel

        related = RelatedModel.objects.create(name="r")
        SampleModel.objects.create(title="a" * 150, price=1, related=related)
        self._as(self._user("viewer", ["viewer"], self.A))
        one = self.client.get("/api/samplemodels/?search=" + "a" * 150)
        two = self.client.get(
            "/api/samplemodels/?search=" + "a" * 150 + " " + "a" * 149
        )
        self.assertEqual(len(one.data["data"]), 1)
        self.assertEqual(two.data["data"], [])


class TestToFieldFkValue(_Base):
    def setUp(self):
        super().setUp()
        usd = ParityCurrency.objects.create(code="USD", name="Dollar")
        ParityCurrency.objects.create(code="EUR", name="Euro")
        ParityAllTypes.objects.create(char="r1", currency=usd)
        ParityAllTypes.objects.create(char="r2")
        _patch(self, ParityCurrency, "sensitive_fields", ["code"], create=True)
        _patch_config(
            self, ParityAllTypes, fields=["id", "char", "currency", "currency__name"]
        )
        self._as(self._user("lim", ["parity_limited"], self.A))

    def test_the_fk_isnt_rendered(self):
        rows = self._rows(self.client.get("/api/parityalltypess/"))
        self.assertTrue(all("currency" not in row for row in rows))

    def test_the_fk_isnt_a_filter(self):
        hit = self._rows(self.client.get("/api/parityalltypess/?currency=USD"))
        miss = self._rows(self.client.get("/api/parityalltypess/?currency=EUR"))
        self.assertEqual(len(hit), len(miss))


class TestOrderOfRelatedRows(_Base):
    def test_a_property_hop_follows_readable_order(self):
        auditor = self._user("aud", ["auditor"], self.A)
        portfolio = Portfolio.objects.create(name="P", brokerage=self.A)
        ids = [
            Holding.objects.create(name=name, brokerage=self.A, portfolio=portfolio).pk
            for name in ("zzz", "aaa", "mmm")
        ]
        _patch(self, Holding._meta, "ordering", ["name"])
        _patch(self, Holding, "sensitive_fields", ["name"], create=True)
        _patch(
            self,
            Portfolio,
            "holding_ids",
            property(lambda s: [h.id for h in s.holdings.all()]),
            create=True,
        )
        _patch_config(
            self,
            Portfolio,
            fields=["id", "name", "holding_ids"],
            property_relations={"holding_ids": ["holdings"]},
        )
        self._as(auditor)
        row = self._rows(self.client.get("/api/portfolios/"))[0]
        self.assertEqual(row["holding_ids"], ids)

    def test_a_to_many_array_doesnt_follow_its_managers_hidden_order(self):
        from django.db import models

        from tests.test_app.models import ParityActiveManager

        row = ParityAllTypes.objects.create(char="r")
        tags = [
            ParityTag.objects.create(name=name, weight=Decimal(weight))
            for name, weight in (("heavy", 9), ("light", 1), ("mid", 5))
        ]
        row.tags.add(*tags)
        _patch(self, ParityTag._meta, "ordering", [])
        _patch(
            self,
            ParityActiveManager,
            "get_queryset",
            lambda m: models.Manager.get_queryset(m)
            .filter(is_deleted=False)
            .order_by("weight"),
        )
        _patch_config(self, ParityAllTypes, fields=["id", "char", "tags__name"])
        self._as(self._user("lim", ["parity_limited"], self.A))
        names = [
            t["name"]
            for t in self._rows(self.client.get("/api/parityalltypess/"))[0]["tags"]
        ]
        self.assertEqual(names, ["heavy", "light", "mid"])


@skipUnless(
    __import__("importlib").util.find_spec("drf_spectacular"), "no drf-spectacular"
)
class TestOpenAPIView(_Base):
    def test_a_model_without_objects_manager(self):
        self._as(self._user("an", ["analyst"], self.A))
        self.assertEqual(self.client.get("/openapi/").status_code, 200)

    def test_lang_and_version_come_from_settings_not_the_caller(self):
        from django.utils.translation import trans_real

        self._as(self._user("an", ["analyst"], self.A))
        before = set(trans_real._translations)
        resp = self.client.get('/openapi/?lang=xx-made-up&version=a";b')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(set(trans_real._translations) - before, set())
        self.assertNotIn('a";b', resp.get("Content-Disposition", ""))


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


class TestWriteErrorsDontTellHiddenFromMissing(_Base):
    def test_chained_tenant_first_hop(self):
        roles = {
            "clerk": [
                "test_app.scopeentry.read",
                "test_app.scopeentry.create",
                "test_app.scopeledger.read",
            ]
        }
        theirs = ScopeLedger.objects.create(name="B books", firm=self.B)
        with self.settings(TURBODRF_ROLES=roles):
            self._as(self._user("clerk", ["clerk"], self.A))
            hidden = self.client.post(
                "/api/scopeentrys/", {"memo": "x", "ledger": theirs.pk}, format="json"
            )
            missing = self.client.post(
                "/api/scopeentrys/", {"memo": "x", "ledger": 987654}, format="json"
            )
        self.assertEqual(hidden.status_code, 400)
        self.assertEqual(hidden.content, missing.content)

    def test_owner_column_naming_another_tenants_user(self):
        other = self._user("other", ["underwriter"], self.B)
        self._as(self._user("me", ["underwriter"], self.A))
        hidden = self.client.post(
            "/api/deals/", {"title": "x", "assigned_broker": other.pk}, format="json"
        )
        missing = self.client.post(
            "/api/deals/", {"title": "x", "assigned_broker": 987654}, format="json"
        )
        self.assertEqual(hidden.status_code, 400)
        self.assertEqual(hidden.content, missing.content)


def _collaborator_q(request, roles):
    return Q(title__startswith="shared") if "collaborator" in roles else Q(pk__in=[])


def _no_collaborator_writes(data, instance, request):
    from turbodrf.backends import get_user_roles

    return ["read only"] if "collaborator" in get_user_roles(request.user) else []


@override_settings(
    TURBODRF_ROLES={
        "collaborator": [
            "test_app.deal.read",
            "test_app.deal.update",
            "test_app.deal.delete",
        ],
        "member": ["test_app.deal.read", "test_app.deal.delete"],
    }
)
class TestDeleteRunsWriteChecks(_Base):
    def _rules(self):
        """The rules on the registry and on the router-built viewset (which
        holds its own copy)."""
        from contextlib import ExitStack

        from django.urls import resolve

        from turbodrf.predicates import Custom, Either, Owner, register_predicates

        rules = [
            Either(
                Owner("assigned_broker"),
                Custom(_collaborator_q, write_validator=_no_collaborator_writes),
            )
        ]
        stack = ExitStack()
        stack.enter_context(_preserve_registries())
        register_predicates(Deal, rules)
        # The viewset that serves the URL now (another test may have rebuilt
        # the router since tests.urls was imported).
        viewset = resolve("/api/deals/1/").func.cls
        stack.enter_context(
            mock.patch.object(viewset, "_predicates", rules, create=True)
        )
        return stack

    def test_a_blocked_writer_cant_delete(self):
        owner = self._user("owner", ["member"], self.A)
        deal = Deal.objects.create(
            title="shared", brokerage=self.A, assigned_broker=owner
        )
        self._as(self._user("collab", ["collaborator"], self.A))
        with self._rules():
            self.assertEqual(self.client.get(f"/api/deals/{deal.pk}/").status_code, 200)
            self.assertEqual(
                self.client.delete(f"/api/deals/{deal.pk}/").status_code, 403
            )
        self.assertTrue(Deal.objects.filter(pk=deal.pk).exists())

    def test_the_owner_can(self):
        owner = self._user("owner", ["member"], self.A)
        deal = Deal.objects.create(
            title="mine", brokerage=self.A, assigned_broker=owner
        )
        self._as(owner)
        with self._rules():
            self.assertEqual(
                self.client.delete(f"/api/deals/{deal.pk}/").status_code, 204
            )


class TestWritableNeedsTheAction(SimpleTestCase):
    def test_a_field_grant_doesnt_stand_in_for_the_action(self):
        from turbodrf.serializers import TurboDRFSerializer

        class Snapshot:
            def can_perform_action(self, action):
                return action == "create"

            def has_write_rule(self, name):
                return name == "title"

            def can_write_field(self, name):
                return True

        request = mock.Mock(user=mock.Mock(is_authenticated=True))
        serializer = TurboDRFSerializer(context={"request": request})
        serializer._permission_snapshot = Snapshot()
        with mock.patch(
            "turbodrf.permissions.permissions_bypassed", return_value=False
        ):
            self.assertEqual(serializer._writable({"title": "x"}, Deal, "update"), {})
            self.assertEqual(
                serializer._writable({"title": "x"}, Deal, "create"), {"title": "x"}
            )


class TestGenericFkDefaultContentType(_Base):
    def test_a_defaulted_content_type_is_checked(self):
        from django.contrib.contenttypes.fields import GenericForeignKey
        from django.contrib.contenttypes.models import ContentType
        from django.db import models
        from django.test.utils import isolate_apps
        from rest_framework.test import APIRequestFactory

        from turbodrf.serializers import _check_fk_injection_writes

        deal_type = ContentType.objects.get_for_model(Deal)
        theirs = Deal.objects.create(title="B", brokerage=self.B)
        with isolate_apps("tests.test_app"):

            class Note(models.Model):
                brokerage = models.ForeignKey(
                    Brokerage, models.CASCADE, related_name="+"
                )
                content_type = models.ForeignKey(
                    ContentType, models.CASCADE, related_name="+", default=deal_type.pk
                )
                object_id = models.PositiveBigIntegerField()
                target = GenericForeignKey("content_type", "object_id")

                class Meta:
                    app_label = "test_app"

            request = APIRequestFactory().post("/")
            request.user = self._user("me", ["underwriter"], self.A)
            errors = _check_fk_injection_writes(
                Note, {"object_id": theirs.pk}, request, "brokerage"
            )
        self.assertEqual(list(errors), ["object_id"])


class TestProxyOfARegisteredModel(_Base):
    def test_rules_follow_the_concrete_model(self):
        from tests.integration.test_register import registered
        from turbodrf.predicates import get_tenant_field

        proxy = type(
            "AuditLedgerProxy",
            (ScopeLedger,),
            {
                "__module__": __name__,
                "Meta": type("Meta", (), {"proxy": True, "app_label": "test_app"}),
            },
        )
        with registered(
            ScopeLedger, {"tenant_field": "firm", "fields": ["id", "name"]}
        ):
            self.assertEqual(get_tenant_field(proxy), "firm")


class TestPlatformMoveFollowsRows(_Base):
    def test_a_reference_to_a_row_that_moves_along(self):
        from turbodrf.predicates import register_tenant_field
        from turbodrf.serializers import _platform_referenced

        deal = Deal.objects.create(title="d", brokerage=self.A)
        account = BankAccount.objects.create(name="a", deal=deal)
        Transaction.objects.create(amount=1, bank_account=account)
        rows = Deal.objects.filter(pk=deal.pk)
        with _preserve_registries():
            register_tenant_field(Transaction, "bank_account__deal__brokerage")
            self.assertFalse(_platform_referenced(Deal, rows))  # it moves too
            register_tenant_field(Transaction, "some_other__brokerage")
            self.assertTrue(_platform_referenced(Deal, rows))  # it stays behind


# ---------------------------------------------------------------------------
# HTTP edges
# ---------------------------------------------------------------------------


class TestHttpEdges(_Base):
    def test_format_suffix_on_a_custom_action(self):
        from tests.test_app.models import Widget

        widget = Widget.objects.create(name="w")
        resp = self.client.get(f"/api/widgets/{widget.pk}/ping.json")
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_a_deeply_nested_body_is_a_400(self):
        self._as(self._user("me", ["underwriter"], self.A))
        resp = self.client.post(
            "/api/deals/", data="[" * 100_000, content_type="application/json"
        )
        self.assertEqual(resp.status_code, 400)

    def test_a_long_m2m_list_is_a_query_per_500_ids(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from tests.test_app.models import ArticleWithCategories, Category

        categories = [Category.objects.create(name=f"c{i}") for i in range(20)]
        ids = [categories[i % 20].pk for i in range(2000)]
        self._as(self._user("admin", ["admin"], self.A))
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.post(
                "/api/articlewithcategoriess/",
                {"title": "t", "content": "c", "categories": ids},
                format="json",
            )
        self.assertEqual(resp.status_code, 201, resp.content[:300])
        self.assertLess(len(ctx.captured_queries), 60)
        article = ArticleWithCategories.objects.latest("pk")
        self.assertEqual(article.categories.count(), 20)


class TestCacheIdentity(SimpleTestCase):
    def test_a_string_pk_never_shares_the_anonymous_key(self):
        from django.contrib.auth.models import AnonymousUser

        from tests.test_app.models import SampleModel
        from turbodrf.backends import get_cache_key

        user = mock.Mock(is_authenticated=True, pk="anonymous", roles=[])
        with mock.patch("turbodrf.backends.get_user_roles", return_value=[]):
            self.assertNotEqual(
                get_cache_key(user, SampleModel),
                get_cache_key(AnonymousUser(), SampleModel),
            )


# ---------------------------------------------------------------------------
# Startup checks
# ---------------------------------------------------------------------------


@override_settings(**TENANT)
class TestStartupChecks(SimpleTestCase):
    DEAL = {"tenant_field": "brokerage", "fields": ["id", "title"]}

    def test_a_property_of_a_related_model_is_refused(self):
        _patch(
            self,
            ParityTarget,
            "amount_text",
            property(lambda s: str(s.amount)),
            create=True,
        )
        with self.assertRaisesMessage(ImproperlyConfigured, "property sandbox"):
            _router(
                (
                    ParityAllTypes,
                    {"tenancy": "shared", "fields": ["id", "target__amount_text"]},
                )
            )

    def test_a_misspelt_sensitive_field_is_refused(self):
        _patch(self, Deal, "sensitive_fields", ["titel"], create=True)
        with self.assertRaisesMessage(ImproperlyConfigured, "sensitive_fields"):
            _router((Deal, self.DEAL))

    def test_a_sensitive_field_by_column_name_is_refused(self):
        _patch(self, Deal, "sensitive_fields", ["assigned_broker_id"], create=True)
        with self.assertRaisesMessage(ImproperlyConfigured, "assigned_broker_id"):
            _router((Deal, self.DEAL))

    def test_a_string_boolean_is_refused(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "'public_access'"):
            _router((Deal, {**self.DEAL, "public_access": "false"}))

    def test_an_owner_column_name_is_refused(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "'assigned_broker'"):
            _router((Deal, {**self.DEAL, "owner_field": "assigned_broker_id"}))

    def test_a_tenant_path_through_a_shared_model_is_refused(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "runs through Deal"):
            _router(
                (Deal, {"tenancy": "shared", "fields": ["id", "title"]}),
                (BankAccount, {"tenant_field": "deal__brokerage", "fields": ["id"]}),
            )

    def test_a_sensitive_lookup_field_is_refused(self):
        from turbodrf.router import _validate_fields_and_lookup

        _patch(self, ParityCurrency, "sensitive_fields", ["code"], create=True)
        with self.assertRaisesMessage(ImproperlyConfigured, "lookup_field"):
            _validate_fields_and_lookup(
                ParityCurrency, {"fields": ["code"], "lookup_field": "code"}
            )

    def test_registering_twice_is_refused(self):
        from tests.integration.test_register import registered
        from tests.test_app.models import NoTurboDRFModel
        from turbodrf import register

        with registered(NoTurboDRFModel, {"tenancy": "shared", "fields": ["id"]}):
            with self.assertRaisesMessage(ImproperlyConfigured, "already registered"):
                register(NoTurboDRFModel, {"tenancy": "shared", "fields": ["name"]})


# ---------------------------------------------------------------------------
# full_clean on PUT
# ---------------------------------------------------------------------------


class TestFullCleanPut(_Base):
    def test_put_doesnt_echo_a_hidden_column(self):
        roles = {
            "pa_editor": [
                "test_app.parityalltypes.read",
                "test_app.parityalltypes.update",
            ]
        }
        row = ParityAllTypes.objects.create(char="r")
        ParityAllTypes.objects.filter(pk=row.pk).update(status="hidden-legacy-v")
        _patch(self, ParityAllTypes, "sensitive_fields", ["status"], create=True)
        _patch_config(
            self, ParityAllTypes, fields=["id", "char", "status"], full_clean=True
        )
        with self.settings(TURBODRF_ROLES=roles):
            self._as(self._user("ed", ["pa_editor"], self.A))
            put = self.client.put(
                f"/api/parityalltypess/{row.pk}/", {"char": "x"}, format="json"
            )
        self.assertNotIn("hidden-legacy-v", put.content.decode())
        json.loads(put.content)


class _ReadsElsewhere:
    """A router sending Deal reads to a replica this test project doesn't have."""

    def db_for_read(self, model, **hints):
        return "replica" if model is Deal else None

    def db_for_write(self, model, **hints):
        return None

    def allow_relation(self, *args, **kwargs):
        return True


class TestPostWriteCheckUsesTheWriteDatabase(_Base):
    def test_it_reads_the_row_where_it_was_written(self):
        from rest_framework.test import APIRequestFactory

        from turbodrf.predicates import Conditional, register_predicates
        from turbodrf.serializers import _verify_written_row

        deal = Deal.objects.create(title="x", brokerage=self.A)
        request = APIRequestFactory().get("/")
        request.user = self._user("me", ["underwriter"], self.A)
        with _preserve_registries():
            register_predicates(
                Deal,
                [
                    Conditional(
                        when=Q(title__startswith="STAFF"), require_roles=["staff"]
                    )
                ],
            )
            with override_settings(DATABASE_ROUTERS=[_ReadsElsewhere()]):
                # Read through the router, this would ask for "replica".
                _verify_written_row(Deal, deal, request, (), "default")
