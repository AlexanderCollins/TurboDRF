"""Regressions for the views / filters / compiled-path / OPTIONS audit.

Each test names the finding it pins. Behaviour, not implementation: the tests
go through the HTTP layer wherever the finding is visible there.
"""

import builtins
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.models import Exists, OuterRef, Q
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from tests.integration.parity_fixtures import ALL_URL, ParityFixture, parity_config
from tests.test_app.apps import _test_user_brokerages, set_test_brokerage
from tests.test_app.models import (
    ArticleWithCategories,
    Brokerage,
    Category,
    Deal,
    Gadget,
    Holding,
    HoldingEvent,
    ParityAllTypes,
    ParityTarget,
    Portfolio,
    RbacBoard,
    RelatedModel,
    SampleModel,
    Widget,
)
from turbodrf.views import Authorization, TurboDRFViewSet

User = get_user_model()

FIELDS = ["id", "char", "integer", "target", "target__name", "tags__name"]
TYPED_FIELDS = [
    "id",
    "char",
    "integer",
    "span",
    "uid",
    "flag",
    "amount",
    "target",
    "target__name",
]


def _crud(model):
    return [f"test_app.{model}.{a}" for a in ("read", "create", "update", "delete")]


ROLES = {role: list(perms) for role, perms in settings.TURBODRF_ROLES.items()}
ROLES["underwriter"] += _crud("rbacboard")
ROLES["admin"] += _crud("widget")


def _ids(body):
    return [row["id"] for row in body["data"]]


# ---------------------------------------------------------------------------
# Filters (findings 1, 4, 5, 21, 22)
# ---------------------------------------------------------------------------


class TestNestedFilterNeedsConfiguredPath(ParityFixture):
    """Finding 1: a nested filter's resolved path must be a configured path.
    ParityTarget's seen_at / uid columns are exposed by no endpoint."""

    PROBES = (
        {"target__seen_at__year": "2024"},
        {"target__seen_at__year": "2023"},
        {"target__uid": "12345678-1234-5678-1234-567812345678"},
        {"target__rows__char": "Hello"},  # through a reverse hop
    )

    def _assert_no_oracle(self, users):
        with parity_config(ParityAllTypes, {"fields": FIELDS}):
            for user in users:
                self.client.force_authenticate(user)
                status, body = self._get(ALL_URL)
                self.assertEqual(status, 200)
                for params in self.PROBES:
                    with self.subTest(user=str(user), params=params):
                        status, probed = self._get(ALL_URL, params)
                        self.assertEqual(status, 200)
                        self.assertEqual(_ids(probed), _ids(body))

    def test_unconfigured_nested_column_is_ignored(self):
        self._assert_no_oracle((None, self.reader))

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_unconfigured_nested_column_is_ignored_when_bypassed(self):
        self._assert_no_oracle((None,))

    def test_configured_nested_path_still_filters(self):
        with parity_config(ParityAllTypes, {"fields": FIELDS}):
            self.client.force_authenticate(self.reader)
            status, body = self._get(ALL_URL, {"target__name": "alpha"})
        self.assertEqual(status, 200)
        self.assertEqual(_ids(body), [self.row_full.pk])


class TestFilterValues(ParityFixture):
    """Finding 21: filter values are parsed by the model field (400 when they
    can't be), `__range` takes exactly two values, and a filter is never
    silently dropped into "every row"."""

    def _probe(self, params):
        with parity_config(ParityAllTypes, {"fields": TYPED_FIELDS}):
            self.client.force_authenticate(self.reader)
            return self._get(ALL_URL, params)

    def test_unparseable_values_are_400(self):
        for params in (
            {"integer_or": "abc"},
            {"uid_or": "zzz"},
            {"flag_or": "maybe"},
            {"amount_or": "abc"},
            {"target__isnull_or": "garbage"},
            {"integer__in": "1,x"},
            {"integer__range": "19"},
        ):
            with self.subTest(params=params):
                status, body = self._probe(params)
                self.assertEqual(status, 400, body)

    def test_parsed_values_filter(self):
        cases = [
            ({"integer__in_or": "42,3"}, {self.row_full.pk, self.row_mid.pk}),
            (
                {"integer__range": "1,50"},
                {self.row_full.pk, self.row_mid.pk, self.row_leafnull.pk},
            ),
            ({"span": "1 02:03:04"}, {self.row_full.pk}),
            ({"span__gte": "01:00:00"}, {self.row_full.pk}),
            ({"flag_or": "true"}, {self.row_full.pk}),
        ]
        for params, expected in cases:
            with self.subTest(params=params):
                status, body = self._probe(params)
                self.assertEqual(status, 200, body)
                self.assertEqual(set(_ids(body)), expected)

    def test_length_guard_only_on_filters(self):
        status, body = self._probe({"fields": "x" * 1001})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["data"]), 4)
        status, body = self._probe({"char__icontains": "x" * 1001})
        self.assertEqual((status, body["data"]), (200, []))


class TestMultiValuedFilters(ParityFixture):
    """Finding 22: a filter across an M2M matches each row once."""

    def test_nested_m2m_filter_does_not_repeat_rows(self):
        for user in (None, self.reader):
            with self.subTest(user=str(user)):
                with parity_config(ParityAllTypes, {"fields": FIELDS}):
                    self.client.force_authenticate(user)
                    status, body = self._get(ALL_URL, {"tags__name__icontains": "a"})
                self.assertEqual(status, 200)
                ids = _ids(body)
                self.assertEqual(len(ids), len(set(ids)))
                self.assertIn(self.row_full.pk, ids)
                self.assertEqual(body["pagination"]["total_items"], len(ids))


@override_settings(TURBODRF_ROLES=ROLES)
class TestM2MRelationFilters(TestCase):
    """Finding 4: M2M filters only match targets the caller can see, don't
    repeat rows, and the broken lookups aren't offered."""

    def setUp(self):
        cache.clear()
        _test_user_brokerages.clear()
        self.client = APIClient()
        self.ba = Brokerage.objects.create(name="A")
        self.bb = Brokerage.objects.create(name="B")
        self.ua = self._user("ua", self.ba)
        other = self._user("ua2", self.ba)
        self.visible = Deal.objects.create(
            title="vis", brokerage=self.ba, assigned_broker=self.ua
        )
        self.hidden = Deal.objects.create(
            title="hid", brokerage=self.ba, assigned_broker=other
        )
        self.other_tenant = Deal.objects.create(title="B", brokerage=self.bb)
        self.board = RbacBoard.objects.create(title="hidden-links", owner=self.ua)
        self.board.deals.set([self.hidden, self.other_tenant])
        self.empty = RbacBoard.objects.create(title="empty", owner=self.ua)
        self.linked = RbacBoard.objects.create(title="visible-link", owner=self.ua)
        self.linked.deals.set([self.visible])
        self.client.force_authenticate(self.ua)

    def tearDown(self):
        _test_user_brokerages.clear()

    def _user(self, name, brokerage):
        user = User.objects.create_user(username=name, password="x")
        user._test_roles = ["underwriter"]
        set_test_brokerage(user, brokerage)
        return user

    def _titles(self, params=None):
        resp = self.client.get("/api/rbacboards/", params or {})
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        return [row["title"] for row in resp.data["data"]]

    def test_isnull_does_not_join_hidden_links(self):
        everything = self._titles()
        for value in ("false", "true"):
            with self.subTest(value=value):
                self.assertEqual(self._titles({"deals__isnull": value}), everything)

    def test_or_and_exact_lookups_are_no_oracle(self):
        everything = self._titles()
        for key in ("deals_or", "deals__exact", "deals__in", "deals__in_or"):
            for pk in (self.hidden.pk, self.other_tenant.pk, 999999):
                with self.subTest(key=key, pk=pk):
                    self.assertEqual(self._titles({key: pk}), everything)

    def test_exact_filter_matches_visible_targets_only(self):
        self.assertEqual(self._titles({"deals": self.visible.pk}), ["visible-link"])
        self.assertEqual(self._titles({"deals": self.hidden.pk}), [])
        self.assertEqual(self._titles({"deals": 999999}), [])

    def test_ordering_by_m2m_is_not_offered(self):
        """Finding 3: ordering by an M2M JOINed hidden links (repeating rows,
        leaking their order); it's ignored now."""
        self.assertEqual(self._titles({"ordering": "deals"}), self._titles())
        self.assertEqual(self._titles({"ordering": "-deals"}), self._titles())
        options = self.client.options("/api/rbacboards/")
        self.assertNotIn("deals", options.data["capabilities"]["ordering_fields"])
        self.assertNotIn("deals__isnull", str(options.data["capabilities"]))


class TestOrdering(ParityFixture):
    """Finding 3: only concrete, single-valued columns are orderable."""

    def test_ordering_by_m2m_repeats_no_rows(self):
        with parity_config(ParityAllTypes, {"fields": FIELDS}):
            self.client.force_authenticate(self.reader)
            status, body = self._get(ALL_URL, {"ordering": "tags"})
        self.assertEqual(status, 200)
        ids = _ids(body)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(body["pagination"]["total_items"], 4)

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_property_and_reverse_terms_are_ignored_when_bypassed(self):
        # A @property isn't a column: ordering by it was a FieldError (500).
        Gadget.objects.create(name="a", qty=1)
        resp = self.client.get("/api/gadgets/", {"ordering": "label"})
        self.assertEqual(resp.status_code, 200)
        # Nor is the base of a nested path through a reverse relation.
        view = TurboDRFViewSet()
        view.model = ParityTarget
        config = {**ParityTarget.turbodrf(), "fields": ["id", "name", "rows__char"]}
        with mock.patch.object(
            ParityTarget, "turbodrf", classmethod(lambda cls: dict(config))
        ):
            self.assertEqual(view.ordering_fields, ["id", "name"])


class TestSearchBounds(ParityFixture):
    """Finding 5: search terms are de-duplicated and capped; an over-length
    or over-cap search matches nothing instead of widening or 500ing."""

    def _search(self, term):
        config = {"fields": FIELDS, "searchable_fields": ["char", "target__name"]}
        with parity_config(ParityAllTypes, config):
            self.client.force_authenticate(self.reader)
            return self._get(ALL_URL, {"search": term})

    def test_too_many_terms_match_nothing(self):
        status, body = self._search(" ".join(f"t{i}" for i in range(1100)))
        self.assertEqual((status, body["data"]), (200, []))

    def test_over_length_term_matches_nothing(self):
        status, body = self._search("a" * 1001)
        self.assertEqual((status, body["data"]), (200, []))

    def test_repeated_terms_count_once(self):
        _, once = self._search("alpha")
        status, repeated = self._search(" ".join(["alpha"] * 50))
        self.assertEqual(status, 200)
        self.assertEqual(_ids(repeated), _ids(once))
        self.assertEqual(_ids(once), [self.row_full.pk])


class TestSearchFieldValidation(TestCase):
    """Finding 20: a searchable entry ending on a relation is dropped (it
    500ed every search), DRF prefixes work, `$` (regex) is refused."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        rel = RelatedModel.objects.create(name="r")
        self.hit = SampleModel.objects.create(
            title="Item one", price=Decimal("1"), related=rel
        )
        SampleModel.objects.create(title="Other item", price=Decimal("1"), related=rel)
        admin = User.objects.create_user(username="adm", password="x")
        admin._test_roles = ["admin"]
        self.client.force_authenticate(admin)

    def _titles(self, searchable, term):
        with mock.patch.object(SampleModel, "searchable_fields", searchable):
            resp = self.client.get("/api/samplemodels/", {"search": term})
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        return sorted(row["title"] for row in resp.data["data"])

    def test_relation_entry_is_dropped(self):
        self.assertEqual(self._titles(["title", "related"], "one"), ["Item one"])

    def test_prefixes(self):
        self.assertEqual(self._titles(["^title"], "item"), ["Item one"])
        self.assertEqual(self._titles(["=title"], "one"), [])  # iexact
        self.assertEqual(self._titles(["title"], "one"), ["Item one"])
        # `$` isn't accepted: the entry is dropped, so nothing is searched.
        self.assertEqual(self._titles(["$title"], "^Item"), ["Item one", "Other item"])


# ---------------------------------------------------------------------------
# ?fields= (finding 19)
# ---------------------------------------------------------------------------


class TestClientFieldsSelection(ParityFixture):
    def test_m2m_subfield_selection(self):
        for params in ({"fields": "tags.name"}, {"fields": "tags_name"}):
            with self.subTest(params=params):
                with parity_config(ParityAllTypes, {"fields": FIELDS}):
                    self.client.force_authenticate(self.reader)
                    status, body = self._get(ALL_URL, params)
                self.assertEqual(status, 200)
                rows = body["data"]
                self.assertTrue(rows)
                for row in rows:
                    self.assertEqual(set(row), {"tags"})
                    for tag in row["tags"]:
                        self.assertEqual(set(tag), {"name"})

    def test_unconfigured_pk_selects_nothing(self):
        fields = ["char", "integer", "target__name"]
        with parity_config(ParityAllTypes, {"fields": fields}):
            status, body = self._get(ALL_URL, {"fields": "id"})
        self.assertEqual(
            set(body["data"][0]), {"char", "integer", "target", "target_name"}
        )


class TestClientFieldsNestedKey(TestCase):
    def setUp(self):
        cache.clear()
        _test_user_brokerages.clear()
        brokerage = Brokerage.objects.create(name="A")
        portfolio = Portfolio.objects.create(name="P", brokerage=brokerage)
        holding = Holding.objects.create(
            name="H", brokerage=brokerage, portfolio=portfolio
        )
        self.event = HoldingEvent.objects.create(brokerage=brokerage, holding=holding)
        user = User.objects.create_user(username="an", password="x")
        user._test_roles = ["analyst"]
        set_test_brokerage(user, brokerage)
        self.client = APIClient()
        self.client.force_authenticate(user)

    def tearDown(self):
        _test_user_brokerages.clear()

    def test_nested_key_selects_only_itself(self):
        params = {"fields": "holding_name"}
        row = self.client.get("/api/holdingevents/", params).data["data"][0]
        detail = self.client.get(f"/api/holdingevents/{self.event.pk}/", params).data
        self.assertEqual(set(row), {"holding", "holding_name"})
        self.assertEqual(set(row), set(detail))


# ---------------------------------------------------------------------------
# Serializer / views (findings 2, 10, 11, 12, 13, 24, 35)
# ---------------------------------------------------------------------------


class TestUnauthenticatedUserNone(TestCase):
    """Finding 2: with DRF's UNAUTHENTICATED_USER = None, an anonymous caller
    still gets the field-filtered serializer."""

    def setUp(self):
        cache.clear()
        rel = RelatedModel.objects.create(name="r", description="rd")
        self.obj = SampleModel.objects.create(
            title="t", price=Decimal("9.99"), related=rel, secret_field="SECRET"
        )

    def test_anonymous_none_user_is_field_gated(self):
        rest = {**settings.REST_FRAMEWORK, "UNAUTHENTICATED_USER": None}
        with override_settings(REST_FRAMEWORK=rest):
            detail = APIClient().get(f"/api/samplemodels/{self.obj.pk}/")
            listed = APIClient().get("/api/samplemodels/")
        self.assertEqual(detail.status_code, 200)
        for body in (detail.data, listed.data["data"][0]):
            self.assertNotIn("secret_field", body)
            self.assertNotIn("price", body)


@override_settings(TURBODRF_ROLES=ROLES)
class TestErrorsAndWrites(TestCase):
    def setUp(self):
        cache.clear()
        _test_user_brokerages.clear()
        self.brokerage = Brokerage.objects.create(name="A")
        self.user = User.objects.create_user(username="u", password="x")
        self.user._test_roles = ["underwriter"]
        set_test_brokerage(self.user, self.brokerage)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def tearDown(self):
        _test_user_brokerages.clear()

    def test_404_is_generic(self):
        """Finding 10: the code is not_found and the model isn't named."""
        resp = self.client.get("/api/deals/999999/")
        self.assertEqual(resp.status_code, 404)
        error = resp.json()["error"]
        self.assertEqual(error["code"], "not_found")
        self.assertNotIn("Deal", error["message"])

    def test_multipart_create_with_large_file(self):
        """Finding 12: a file spooled to disk can't be deep-copied."""
        big = SimpleUploadedFile("big.bin", b"x" * (3 * 1024 * 1024))
        resp = self.client.post(
            "/api/deals/", {"title": "x", "attachment": big}, format="multipart"
        )
        self.assertEqual(resp.status_code, 201, resp.content[:300])
        self.assertEqual(resp.data["brokerage"], self.brokerage.pk)

    def test_put_without_tenant(self):
        """Finding 24: PUT gets the tenant FK pre-filled, as POST does."""
        created = self.client.post("/api/deals/", {"title": "x"}, format="json")
        self.assertEqual(created.status_code, 201)
        resp = self.client.put(
            f"/api/deals/{created.data['id']}/", {"title": "y"}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertEqual(resp.data["title"], "y")
        self.assertEqual(resp.data["brokerage"], self.brokerage.pk)


class TestTupleFields(TestCase):
    """Finding 13: a tuple `fields` config is a list of fields."""

    def setUp(self):
        cache.clear()
        rel = RelatedModel.objects.create(name="r")
        self.obj = SampleModel.objects.create(
            title="t", price=Decimal("1"), related=rel, secret_field="S"
        )
        self.base = dict(SampleModel.turbodrf())

    def _detail(self, fields):
        config = {**self.base, "fields": fields}
        with mock.patch.object(
            SampleModel, "turbodrf", classmethod(lambda cls: dict(config))
        ):
            return APIClient().get(f"/api/samplemodels/{self.obj.pk}/")

    @override_settings(
        TURBODRF_DISABLE_PERMISSIONS=True, TURBODRF_SENSITIVE_FIELDS=["secret_field"]
    )
    def test_sensitive_field_stripped_from_tuple(self):
        resp = self._detail(("title", "secret_field"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(set(resp.data), {"title"})

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_nested_entry_in_tuple(self):
        resp = self._detail(("title", "related__name"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["related_name"], "r")


class TestAuthorizationScope(TestCase):
    def test_expression_child_uses_subquery(self):
        """Finding 11: a predicate Q holding an expression (Custom returning
        Q(Exists(...))) was a TypeError on every request."""
        brokerage = Brokerage.objects.create(name="A")
        deal = Deal.objects.create(title="d", brokerage=brokerage)
        q = Q(Exists(Deal.objects.filter(pk=OuterRef("pk"))))
        rows = Authorization(None, q).scope(Deal.objects.all())
        self.assertEqual(list(rows), [deal])


@override_settings(TURBODRF_ROLES=ROLES)
class TestHandWrittenSubclassIsScoped(TestCase):
    """A TurboDRFViewSet subclass written by hand (not built by the router)
    gets its model's tenant boundary and predicates."""

    class DealViewSet(TurboDRFViewSet):
        model = Deal
        queryset = Deal.objects.all()

    def test_subclass_is_tenant_scoped(self):
        _test_user_brokerages.clear()
        mine = Brokerage.objects.create(name="A")
        theirs = Brokerage.objects.create(name="B")
        user = User.objects.create_user(username="m", password="x")
        user._test_roles = ["manager"]
        set_test_brokerage(user, mine)
        Deal.objects.create(title="mine", brokerage=mine)
        Deal.objects.create(title="VICTIM", brokerage=theirs)
        request = APIRequestFactory().get("/")
        force_authenticate(request, user=user)
        view = self.DealViewSet.as_view({"get": "list"})
        resp = view(request)
        _test_user_brokerages.clear()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([row["title"] for row in resp.data["data"]], ["mine"])


# ---------------------------------------------------------------------------
# OPTIONS (findings 14-18)
# ---------------------------------------------------------------------------


@override_settings(TURBODRF_ROLES=ROLES)
class TestOptions(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.admin = User.objects.create_user(username="adm", password="x")
        self.admin._test_roles = ["admin"]
        rel = RelatedModel.objects.create(name="r")
        self.obj = SampleModel.objects.create(
            title="t", price=Decimal("1"), related=rel
        )

    def test_detail_route_describes_detail_fields(self):
        """Finding 14."""
        self.client.force_authenticate(self.admin)
        detail = self.client.options(f"/api/samplemodels/{self.obj.pk}/")
        listed = self.client.options("/api/samplemodels/")
        self.assertIn("description", detail.data["model"]["fields"])
        self.assertNotIn("description", listed.data["model"]["fields"])

    def test_read_only_resource_advertises_no_writes(self):
        """Finding 15."""
        Widget.objects.create(name="w")
        self.client.force_authenticate(self.admin)
        actions = self.client.options("/api/widgets/").data["actions"]
        self.assertTrue(actions["list"])
        for action in ("create", "update", "partial_update", "destroy"):
            self.assertFalse(actions[action], action)
        self.assertEqual(
            self.client.post("/api/widgets/", {"name": "x"}).status_code, 405
        )

    @override_settings(TURBODRF_USE_DEFAULT_PERMISSIONS=True)
    def test_default_permissions_advertise_django_perms(self):
        """Finding 15: in default-permissions mode, Django's model perms."""
        user = User.objects.create_user(username="viewer", password="x")
        user.user_permissions.add(Permission.objects.get(codename="view_samplemodel"))
        user = User.objects.get(pk=user.pk)
        self.client.force_authenticate(user)
        actions = self.client.options("/api/samplemodels/").data["actions"]
        self.assertTrue(actions["list"])
        self.assertFalse(actions["create"])
        self.assertFalse(actions["destroy"])

    def test_options_does_not_fetch_the_object(self):
        """Finding 16: DRF's determine_actions ran get_object() and built
        serializers only for the result to be replaced."""
        self.client.force_authenticate(self.admin)
        with mock.patch.object(
            TurboDRFViewSet, "get_object", side_effect=AssertionError("fetched")
        ):
            resp = self.client.options(f"/api/samplemodels/{self.obj.pk}/")
        self.assertEqual(resp.status_code, 200)

    def test_all_fields_include_m2m(self):
        """Finding 17: `fields: "__all__"` describes M2M fields, as detail
        renders them."""
        article = ArticleWithCategories.objects.create(title="t")
        article.categories.add(Category.objects.create(name="c"))
        config = {**ArticleWithCategories.turbodrf(), "fields": "__all__"}
        self.client.force_authenticate(self.admin)
        with mock.patch.object(
            ArticleWithCategories, "turbodrf", classmethod(lambda cls: dict(config))
        ):
            options = self.client.options("/api/articlewithcategoriess/")
            detail = self.client.get(f"/api/articlewithcategoriess/{article.pk}/")
        self.assertIn("categories", detail.data)
        self.assertIn("categories", options.data["model"]["fields"])

    def test_no_tenancy_block(self):
        """Finding 18: the block exposed the tenant path and the predicate
        classes to any reader, anonymous callers included."""
        anonymous = APIClient().options("/api/samplemodels/")
        self.assertEqual(anonymous.status_code, 200)
        self.assertNotIn("tenancy", anonymous.data)


# ---------------------------------------------------------------------------
# Performance (findings 28-31, 33) and the compiled path itself (34)
# ---------------------------------------------------------------------------


class _EventsFixture(TestCase):
    ROWS = 5

    def setUp(self):
        cache.clear()
        _test_user_brokerages.clear()
        self.brokerage = Brokerage.objects.create(name="A")
        portfolio = Portfolio.objects.create(name="P", brokerage=self.brokerage)
        for i in range(self.ROWS):
            holding = Holding.objects.create(
                name=f"H{i}", brokerage=self.brokerage, portfolio=portfolio
            )
            HoldingEvent.objects.create(brokerage=self.brokerage, holding=holding)
        self.client = APIClient()

    def tearDown(self):
        _test_user_brokerages.clear()

    def _analyst(self, name="an"):
        user = User.objects.create_user(username=name, password="x")
        user._test_roles = ["analyst"]
        set_test_brokerage(user, self.brokerage)
        return user


class TestPerRequestOverhead(_EventsFixture):
    def test_no_postgres_import_per_field(self):
        """Finding 28: `from django.contrib.postgres.fields import JSONField`
        ran (and, without psycopg, failed) for every field, three times."""
        self.client.force_authenticate(self._analyst())
        self.client.get("/api/holdingevents/")  # warm
        real_import = builtins.__import__
        seen = []

        def spy(name, *args, **kwargs):
            if name.startswith("django.contrib.postgres"):
                seen.append(name)
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", spy):
            resp = self.client.get("/api/holdingevents/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(seen, [])

    def test_filterset_fields_computed_once_per_request(self):
        """Finding 28."""
        user = self._analyst()
        request = APIRequestFactory().get("/")
        request.user = user
        view = TurboDRFViewSet()
        view.model = HoldingEvent
        view.request = request
        self.assertIs(view.get_filterset_fields(), view.get_filterset_fields())

    def test_static_mode_cache_reads_per_list(self):
        """Finding 29: 58 cache reads per list; now a key-part read plus one
        snapshot read per model the request touches."""
        self.client.force_authenticate(self._analyst())
        self.client.get("/api/holdingevents/")  # warm
        reads = []
        original = cache.get, cache.get_many
        with (
            mock.patch.object(
                cache,
                "get",
                side_effect=lambda *a, **k: reads.append(a) or original[0](*a, **k),
            ),
            mock.patch.object(
                cache,
                "get_many",
                side_effect=lambda *a, **k: reads.append(a) or original[1](*a, **k),
            ),
        ):
            resp = self.client.get("/api/holdingevents/", {"holding__name": "H1"})
        self.assertEqual(resp.status_code, 200)
        self.assertLessEqual(len(reads), 5)


@override_settings(TURBODRF_PERMISSION_MODE="database")
class TestDatabaseModeQueries(_EventsFixture):
    """Findings 29-30: in database mode a warm list ran 34 queries, 30 of
    them role lookups; nested filters multiplied them."""

    def setUp(self):
        super().setUp()
        from turbodrf.models import RolePermission, TurboDRFRole, UserRole

        role = TurboDRFRole.objects.create(name="analyst_db")
        for model in ("holdingevent", "holding", "portfolio"):
            RolePermission.objects.create(
                role=role, app_label="test_app", model_name=model, action="read"
            )
        self.user = User.objects.create_user(username="db", password="x")
        UserRole.objects.create(user=self.user, role=role)
        set_test_brokerage(self.user, self.brokerage)
        self.client.force_authenticate(self.user)

    def _role_queries(self, params=None):
        self.client.get("/api/holdingevents/", params or {})  # warm the cache
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get("/api/holdingevents/", params or {})
        self.assertEqual(resp.status_code, 200)
        return [q["sql"] for q in ctx.captured_queries if "turbodrf_" in q["sql"]]

    def test_one_role_lookup_per_request(self):
        for params in (
            None,
            {
                "holding__name": "H1",
                "holding__name__icontains": "H",
                "holding__name__istartswith": "H",
                "status": "new",
            },
        ):
            with self.subTest(params=params):
                queries = self._role_queries(params)
                self.assertEqual(
                    sum("turbodrf_user_role" in sql for sql in queries), 1, queries
                )
                self.assertLessEqual(len(queries), 2, queries)


class TestListQueryShape(_EventsFixture):
    def _list_queries(self, params=None):
        self.client.force_authenticate(self._analyst())
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get("/api/holdingevents/", params or {})
        self.assertEqual(resp.status_code, 200)
        return [q["sql"] for q in ctx.captured_queries]

    def test_one_visibility_flag_per_relation(self):
        """Finding 31: holding__name and holding__portfolio cross the same
        relation; its scope is selected once, not once per nested field."""
        data = [sql for sql in self._list_queries() if "test_app_holdingevent" in sql]
        values_sql = next(sql for sql in data if "COUNT(" not in sql)
        self.assertEqual(values_sql.count("CASE WHEN"), 1, values_sql)

    def test_count_has_no_nested_value_joins(self):
        """Finding 31: COUNT runs on the filtered rows, not the page query
        with its JOINs and visibility subqueries."""
        count_sql = next(sql for sql in self._list_queries() if "COUNT(" in sql)
        self.assertNotIn("JOIN", count_sql)
        self.assertNotIn("CASE", count_sql)

    def test_delete_skips_serializer_prep(self):
        """Finding 33: a delete renders no instance, so get_queryset skips
        the visibility annotations (and select_related)."""
        user = self._analyst()
        roles = {
            **ROLES,
            "analyst": [*ROLES["analyst"], "test_app.holdingevent.delete"],
        }
        self.client.force_authenticate(user)
        event = HoldingEvent.objects.first()
        with (
            override_settings(TURBODRF_ROLES=roles),
            mock.patch.object(
                TurboDRFViewSet,
                "_annotate_nested_visibility",
                side_effect=AssertionError("annotated"),
            ),
        ):
            resp = self.client.delete(f"/api/holdingevents/{event.pk}/")
        self.assertEqual(resp.status_code, 204)
