"""Security review (0.6.2): the serializer path's relation scoping and writes.

Each class is a finding a review reproduced on 0.6.1, asserted fixed.
"""

import logging
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models import Q
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from tests.integration.test_regressions_052 import EVENTS_URL, _TenantFixture
from tests.integration.test_review_field_rbac import RBAC_ROLES, _Base, _rbac_roles
from tests.integration.test_review_row_scope import SCOPE_ROLES, _Fixture
from tests.test_app.models import (
    ArticleWithCategories,
    Category,
    Deal,
    Holding,
    HoldingEvent,
    ParityAllTypes,
    ParityCurrency,
    Portfolio,
    RbacBoard,
    RbacComment,
    RbacNote,
    RelatedModel,
    ScopeFolder,
    ScopeItem,
    ScopeTag,
    ScopeTagged,
)
from turbodrf.serializers import (
    TurboDRFSerializer,
    TurboDRFSerializerFactory,
    _check_fk_injection_writes,
)
from turbodrf.validation import scoped_target_queryset

User = get_user_model()

COMMENTS = "/api/rbaccomments/"


def _patched_config(model, **extra):
    base = model.turbodrf()
    return mock.patch.object(
        model, "turbodrf", classmethod(lambda cls: {**base, **extra})
    )


def _deal_predicates(predicates):
    """Patch Deal's registered predicates (every other model keeps its own)."""
    from turbodrf import predicates as P

    original = P.get_predicates
    return mock.patch.object(
        P,
        "get_predicates",
        lambda model: predicates if model is Deal else original(model),
    )


def _serializer(model, fields, request, **kwargs):
    meta = type("Meta", (), {"model": model, "fields": fields})
    cls = type(f"{model.__name__}Probe", (TurboDRFSerializer,), {"Meta": meta})
    return cls(context={"request": request}, **kwargs)


# ---------------------------------------------------------------------------
# 2. An integer tenant from TURBODRF_TENANT_USER_FIELD made every write 500
# ---------------------------------------------------------------------------


@override_settings(TURBODRF_ROLES=RBAC_ROLES, TURBODRF_TENANT_USER_FIELD="brokerage_pk")
class TestIntegerTenant(_Base):
    def setUp(self):
        super().setUp()
        prop = property(lambda u: u.brokerage.pk if u.brokerage else None)
        patcher = mock.patch.object(User, "brokerage_pk", prop, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client.force_authenticate(self.under_a)

    def test_create(self):
        resp = self.client.post("/api/deals/", {"title": "new"}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data["brokerage"], self.brokerage_a.pk)
        deal = Deal.objects.get(pk=resp.data["id"])
        self.assertEqual(deal.brokerage_id, self.brokerage_a.pk)

    def test_update(self):
        resp = self.client.patch(
            f"/api/deals/{self.deal_a.pk}/", {"title": "renamed"}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.deal_a.refresh_from_db()
        self.assertEqual(self.deal_a.title, "renamed")
        self.assertEqual(self.deal_a.brokerage_id, self.brokerage_a.pk)

    def test_other_tenant_still_rejected(self):
        resp = self.client.post(
            "/api/deals/",
            {"title": "new", "brokerage": self.brokerage_b.pk},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(Deal.objects.filter(title="new").exists())


# ---------------------------------------------------------------------------
# 3. PATCH / PUT responses used visibility flags computed before the write
# ---------------------------------------------------------------------------


class TestWriteResponseRechecksVisibility(_TenantFixture):
    def test_patch_to_a_holding_whose_portfolio_is_hidden(self):
        secret = Portfolio.objects.create(name="SecretB", brokerage=self.brokerage_b)
        # A holding in tenant A whose portfolio is tenant B's.
        moved = Holding.objects.create(
            name="Hx", brokerage=self.brokerage_a, portfolio=secret
        )
        fields = list(HoldingEvent.turbodrf()["fields"]) + ["holding__portfolio__name"]
        self.client.force_authenticate(self.analyst)
        url = f"{EVENTS_URL}{self.matched.pk}/"
        with _patched_config(HoldingEvent, fields=fields):
            self.assertEqual(
                self.client.get(url).data["holding_portfolio_name"], "Growth"
            )
            for method in (self.client.patch, self.client.put):
                with self.subTest(method=method.__name__):
                    body = {
                        "holding": moved.pk,
                        "status": "matched",
                        "brokerage": self.brokerage_a.pk,
                    }
                    resp = method(url, body, format="json")
                    self.assertEqual(resp.status_code, 200, resp.content)
                    self.assertIsNone(resp.data["holding_portfolio_name"])
                    self.assertNotIn(b"SecretB", resp.content)
            self.assertIsNone(self.client.get(url).data["holding_portfolio_name"])


# ---------------------------------------------------------------------------
# 4. A bare M2M or a reverse relation rendered unscoped
# ---------------------------------------------------------------------------


@override_settings(TURBODRF_ROLES=RBAC_ROLES)
class TestBareManyToManyScoped(_Base):
    def setUp(self):
        super().setUp()
        self.comment = RbacComment.objects.create(body="c", deal=self.deal_a)
        self.comment.linked_deals.set([self.deal_a, self.deal_a2, self.deal_b])
        self.client.force_authenticate(self.under_a)

    def test_detail_and_list(self):
        detail = self.client.get(f"{COMMENTS}{self.comment.pk}/")
        listed = self.client.get(COMMENTS)
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertEqual(detail.data["linked_deals"], [self.deal_a.pk])
        self.assertEqual(listed.data["data"][0]["linked_deals"], [self.deal_a.pk])

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_bypassed_permissions(self):
        detail = self.client.get(f"{COMMENTS}{self.comment.pk}/")
        self.assertEqual(detail.data["linked_deals"], [self.deal_a.pk])

    def test_manager_sees_tenant_deals(self):
        self.client.force_authenticate(self.manager_a)
        detail = self.client.get(f"{COMMENTS}{self.comment.pk}/")
        self.assertEqual(
            sorted(detail.data["linked_deals"]), [self.deal_a.pk, self.deal_a2.pk]
        )


class TestReverseRelationScoped(_Fixture):
    def setUp(self):
        super().setUp()
        self.folder = ScopeFolder.objects.create(
            name="f", brokerage=self.A, owner=self.a1
        )
        self.mine = ScopeItem.objects.create(
            label="mine", brokerage=self.A, folder=self.folder
        )
        self.foreign = ScopeItem.objects.create(
            label="B-secret", brokerage=self.B, folder=self.folder
        )
        self._as(self.a1)

    def _get(self, fields):
        with _patched_config(ScopeFolder, fields=fields):
            return self.client.get(f"/api/scopefolders/{self.folder.pk}/")

    def test_bare_reverse_fk(self):
        resp = self._get(["id", "name", "items"])
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["items"], [self.mine.pk])

    def test_nested_reverse_fk(self):
        with self.assertNoLogs("turbodrf.serializers", level=logging.WARNING):
            resp = self._get(["id", "name", "items__label"])
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["items"], [{"label": "mine"}])
        self.assertNotIn(b"B-secret", resp.content)

    def test_reverse_relation_is_read_only(self):
        """Written, DRF .set() the reverse FK: moving rows between parents.
        (Role-filtered writes already dropped it; bypassed modes didn't.)"""
        other = ScopeFolder.objects.create(name="g", brokerage=self.A, owner=self.a1)
        roles = {
            **SCOPE_ROLES,
            "scoper": SCOPE_ROLES["scoper"] + ["test_app.scopefolder.update"],
        }
        for bypassed in (False, True):
            with self.subTest(bypassed=bypassed):
                with override_settings(
                    TURBODRF_ROLES=roles, TURBODRF_DISABLE_PERMISSIONS=bypassed
                ):
                    with _patched_config(ScopeFolder, fields=["id", "name", "items"]):
                        resp = self.client.patch(
                            f"/api/scopefolders/{other.pk}/",
                            {"name": "g2", "items": [self.mine.pk]},
                            format="json",
                        )
                self.assertEqual(resp.status_code, 200, resp.content)
                self.assertEqual(resp.data["items"], [])
                self.mine.refresh_from_db()
                self.foreign.refresh_from_db()
                self.assertEqual(self.mine.folder_id, self.folder.pk)
                self.assertEqual(self.foreign.folder_id, self.folder.pk)


# ---------------------------------------------------------------------------
# 6. An OR'd to-many predicate on the target duplicated rows
# ---------------------------------------------------------------------------


@override_settings(TURBODRF_ROLES=RBAC_ROLES)
class TestDuplicatePredicateRows(_Base):
    def setUp(self):
        super().setUp()
        # deal_a (owned by under_a) sits on two boards owned by under_a.
        for i in range(2):
            board = RbacBoard.objects.create(title=f"b{i}", owner=self.under_a)
            board.deals.add(self.deal_a)

    def _predicates(self):
        from turbodrf.predicates import Either, Members, Owner

        return _deal_predicates(
            [Either(Owner("assigned_broker"), Members("rbac_boards__owner"))]
        )

    def test_scoped_target_queryset_has_no_duplicates(self):
        request = SimpleNamespace(user=self.under_a)
        with self._predicates():
            scoped = scoped_target_queryset(Deal, request)
            self.assertEqual(
                list(scoped.values_list("pk", flat=True)), [self.deal_a.pk]
            )
            self.assertEqual(
                _check_fk_injection_writes(
                    RbacComment, {"deal": self.deal_a}, request, None
                ),
                {},
            )
            self.assertEqual(
                _check_fk_injection_writes(
                    RbacComment, {"linked_deals": [self.deal_a]}, request, None
                ),
                {},
            )

    def test_create_through_the_api(self):
        self.client.force_authenticate(self.under_a)
        with self._predicates():
            resp = self.client.post(
                COMMENTS,
                {"body": "x", "deal": self.deal_a.pk, "linked_deals": [self.deal_a.pk]},
                format="json",
            )
        self.assertEqual(resp.status_code, 201, resp.content)


# ---------------------------------------------------------------------------
# 7. Bypassed permission modes had no related-field scoping
# ---------------------------------------------------------------------------


@override_settings(TURBODRF_ROLES=RBAC_ROLES, TURBODRF_DISABLE_PERMISSIONS=True)
class TestBypassedModeScoping(_Base):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.under_a)

    def test_browsable_dropdowns(self):
        resp = self.client.get(COMMENTS, HTTP_ACCEPT="text/html")
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("A deal", body)
        self.assertNotIn("B secret deal", body)
        self.assertNotIn("A2 private deal", body)

    def test_m2m_errors_do_not_reveal_existence(self):
        def post(pk):
            return self.client.post(
                COMMENTS,
                {"body": "x", "deal": self.deal_a.pk, "linked_deals": [pk]},
                format="json",
            )

        missing, other_tenant, hidden = (
            post(999999),
            post(self.deal_b.pk),
            post(self.deal_a2.pk),
        )
        for resp in (missing, other_tenant, hidden):
            self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(missing.content, other_tenant.content)
        self.assertEqual(missing.content, hidden.content)
        self.assertIn(b"not found or not accessible", missing.content)

    def test_fk_to_the_tenant_model(self):
        request = SimpleNamespace(user=self.under_a)
        serializer = _serializer(RbacNote, ["id", "body", "brokerage", "deal"], request)
        choices = serializer.fields["brokerage"].get_queryset()
        self.assertEqual(list(choices), [self.brokerage_a])
        deals = serializer.fields["deal"].get_queryset()
        self.assertEqual(list(deals), [self.deal_a])

    def test_programmatic_use_is_unscoped(self):
        serializer = _serializer(RbacNote, ["id", "body", "brokerage", "deal"], None)
        self.assertEqual(serializer.fields["deal"].get_queryset().count(), 3)


@override_settings(TURBODRF_ROLES=RBAC_ROLES)
class TestTenantModelCoTenantRule(_Base):
    def test_plain_fk_to_the_tenant_model_is_checked(self):
        """A tenant-scoped host's other FK to the tenant model (here: the host
        reaches its tenant through `deal`) must name the caller's tenant."""
        request = SimpleNamespace(user=self.under_a)
        errors = _check_fk_injection_writes(
            RbacNote, {"brokerage": self.brokerage_b}, request, "deal__brokerage"
        )
        self.assertEqual(
            errors, {"brokerage": ["Invalid brokerage: not found or not accessible."]}
        )
        self.assertEqual(
            _check_fk_injection_writes(
                RbacNote, {"brokerage": self.brokerage_a}, request, "deal__brokerage"
            ),
            {},
        )

    def test_shared_host_is_not_co_tenant_checked(self):
        request = SimpleNamespace(user=self.under_a)
        self.assertEqual(
            _check_fk_injection_writes(
                RbacNote, {"brokerage": self.brokerage_b}, request, None
            ),
            {},
        )


@override_settings(TURBODRF_ROLES=RBAC_ROLES)
class TestToFieldRelationScoped(_Base):
    """A to_field FK renders as a SlugRelatedField, which was never scoped."""

    def setUp(self):
        super().setUp()
        ParityCurrency.objects.create(code="USD", name="Dollar")
        ParityCurrency.objects.create(code="SEC", name="Secret")
        self.request = SimpleNamespace(user=self.under_a)

    def _scoped_currency(self):
        from turbodrf import predicates as P
        from turbodrf.predicates import Custom

        predicate = Custom(
            q_func=lambda request, roles: Q(code="USD"),
            write_validator=lambda data, instance, request: [],
        )
        original = P.get_predicates
        return mock.patch.object(
            P,
            "get_predicates",
            lambda model: [predicate] if model is ParityCurrency else original(model),
        )

    def test_choices_and_errors(self):
        with self._scoped_currency():
            serializer = _serializer(
                ParityAllTypes, ["id", "char", "currency"], self.request
            )
            choices = serializer.fields["currency"].get_queryset()
            self.assertEqual([c.code for c in choices], ["USD"])

            def errors(code):
                s = _serializer(
                    ParityAllTypes,
                    ["id", "char", "currency"],
                    self.request,
                    data={"char": "x", "currency": code},
                )
                self.assertFalse(s.is_valid())
                return s.errors

            self.assertEqual(errors("SEC"), errors("ZZZ"))
            self.assertEqual(
                errors("SEC"),
                {"currency": ["Invalid currency: not found or not accessible."]},
            )
            valid = _serializer(
                ParityAllTypes,
                ["id", "char", "currency"],
                self.request,
                data={"char": "x", "currency": "USD"},
            )
            self.assertTrue(valid.is_valid(), valid.errors)


# ---------------------------------------------------------------------------
# 8. An M2M update deleted links the caller can't see
# ---------------------------------------------------------------------------


@override_settings(TURBODRF_ROLES=RBAC_ROLES)
class TestM2MUpdateKeepsHiddenLinks(_Base):
    def setUp(self):
        super().setUp()
        self.comment = RbacComment.objects.create(body="c", deal=self.deal_a)
        self.comment.linked_deals.set([self.deal_a, self.deal_a2, self.deal_b])
        self.client.force_authenticate(self.under_a)

    def _links(self):
        return set(self.comment.linked_deals.values_list("pk", flat=True))

    def test_patch_keeps_hidden_links(self):
        resp = self.client.patch(
            f"{COMMENTS}{self.comment.pk}/", {"linked_deals": []}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["linked_deals"], [])
        self.assertEqual(self._links(), {self.deal_a2.pk, self.deal_b.pk})

    def test_patch_adds_a_visible_link(self):
        self.comment.linked_deals.remove(self.deal_a)
        resp = self.client.patch(
            f"{COMMENTS}{self.comment.pk}/",
            {"linked_deals": [self.deal_a.pk]},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["linked_deals"], [self.deal_a.pk])
        self.assertEqual(
            self._links(), {self.deal_a.pk, self.deal_a2.pk, self.deal_b.pk}
        )

    def test_unscoped_target_is_replaced(self):
        author = RelatedModel.objects.create(name="a")
        one, two = Category.objects.create(name="1"), Category.objects.create(name="2")
        article = ArticleWithCategories.objects.create(title="t", author=author)
        article.categories.set([one, two])
        admin = User.objects.create_user(username="adm")
        admin._test_roles = ["admin"]
        request = SimpleNamespace(user=admin)
        serializer = _serializer(
            ArticleWithCategories,
            ["id", "title", "categories"],
            request,
            instance=article,
            data={"categories": [one.pk]},
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        self.assertEqual(list(article.categories.all()), [one])


# ---------------------------------------------------------------------------
# 11. The serializer-path M2M render ran queries per row and per sub-hop
# ---------------------------------------------------------------------------


class TestManyToManyRenderQueries(_Fixture):
    def _make(self, n):
        for i in range(n):
            folder = ScopeFolder.objects.create(
                name=f"f{i}", brokerage=self.A, owner=self.a1
            )
            t1 = ScopeTag.objects.create(name=f"t{i}", brokerage=self.A, folder=folder)
            t2 = ScopeTag.objects.create(name=f"u{i}", brokerage=self.A, folder=folder)
            row = ScopeTagged.objects.create(title=f"r{i}", brokerage=self.A)
            row.tags.set([t1, t2])

    def test_constant_query_count(self):
        self.client.force_authenticate(self.a1)
        self._make(3)
        with CaptureQueriesContext(connection) as small:
            first = self.client.get("/api/scopetaggeds/")
        self._make(10)
        with CaptureQueriesContext(connection) as large:
            second = self.client.get("/api/scopetaggeds/")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(len(second.data["data"]), 13)
        self.assertEqual(len(small), len(large))
        self.assertEqual(
            sorted(second.data["data"][0]["tags"], key=lambda tag: tag["name"]),
            [
                {"name": "t0", "folder__name": "f0"},
                {"name": "u0", "folder__name": "f0"},
            ],
        )

    def test_hidden_sub_hop_still_null(self):
        # a2 can't see a1's folder: the folder name renders null.
        self._make(2)
        self.client.force_authenticate(self.a2)
        resp = self.client.get("/api/scopetaggeds/")
        self.assertEqual(resp.status_code, 200)
        for row in resp.data["data"]:
            for tag in row["tags"]:
                self.assertIsNone(tag["folder__name"])


# ---------------------------------------------------------------------------
# 12. Every FK error was rewritten to "not found or not accessible"
# ---------------------------------------------------------------------------


@override_settings(TURBODRF_ROLES=RBAC_ROLES)
class TestRelationErrorMessages(_Base):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.under_a)

    def test_required_and_wrong_type_kept(self):
        missing = self.client.post(COMMENTS, {"body": "x"}, format="json")
        self.assertEqual(missing.status_code, 400)
        self.assertIn(b"This field is required.", missing.content)
        wrong = self.client.post(COMMENTS, {"body": "x", "deal": "abc"}, format="json")
        self.assertEqual(wrong.status_code, 400)
        self.assertIn(b"Incorrect type", wrong.content)
        not_a_list = self.client.post(
            COMMENTS,
            {"body": "x", "deal": self.deal_a.pk, "linked_deals": 5},
            format="json",
        )
        self.assertEqual(not_a_list.status_code, 400)
        self.assertIn(b"Expected a list of items", not_a_list.content)

    def test_does_not_exist_unified(self):
        def post(**body):
            return self.client.post(COMMENTS, {"body": "x", **body}, format="json")

        fk = [post(deal=999999), post(deal=self.deal_b.pk), post(deal=self.deal_a2.pk)]
        for resp in fk:
            self.assertEqual(resp.status_code, 400)
            self.assertIn(b"Invalid deal: not found or not accessible.", resp.content)
        self.assertEqual(fk[0].content, fk[1].content)
        self.assertEqual(fk[0].content, fk[2].content)
        m2m = [
            post(deal=self.deal_a.pk, linked_deals=[999999]),
            post(deal=self.deal_a.pk, linked_deals=[self.deal_b.pk]),
        ]
        for resp in m2m:
            self.assertEqual(resp.status_code, 400)
        self.assertEqual(m2m[0].content, m2m[1].content)
        self.assertIn(
            b"Invalid linked_deals: not found or not accessible.", m2m[0].content
        )


# ---------------------------------------------------------------------------
# 15. Cleanups with observable behaviour
# ---------------------------------------------------------------------------


class TestFactoryAllIncludesManyToMany(_Base):
    def test_all_includes_m2m(self):
        admin = User.objects.create_user(username="adm")
        admin._test_roles = ["admin"]
        permitted = TurboDRFSerializerFactory._get_permitted_fields_with_snapshot(
            ArticleWithCategories, "__all__", admin
        )
        self.assertIn("categories", permitted)


class TestM2MRenderFailureLogged(_Base):
    def test_failure_is_logged(self):
        author = RelatedModel.objects.create(name="a")
        article = ArticleWithCategories.objects.create(title="t", author=author)
        serializer = _serializer(
            ArticleWithCategories, ["id", "title", "categories"], None
        )
        with mock.patch.object(
            TurboDRFSerializer, "_to_many_relations", side_effect=RuntimeError("boom")
        ):
            with self.assertLogs("turbodrf.serializers", level="WARNING") as logs:
                result = serializer._serialize_m2m_field(
                    article, "categories", ["categories__name"]
                )
        self.assertEqual(result, [])
        self.assertIn("boom", "\n".join(logs.output))


@override_settings(
    TURBODRF_ROLES=_rbac_roles(admin=["test_app.deal.assigned_broker.read"])
)
class TestAttnameFilterPath(_Base):
    """`deal__assigned_broker_id` resolved (get_field also accepts an attname)
    but found no rule for `assigned_broker_id`, so it fell back to model
    read."""

    def setUp(self):
        super().setUp()
        RbacNote.objects.create(body="n1", brokerage=self.brokerage_a, deal=self.deal_a)
        RbacNote.objects.create(
            body="n2", brokerage=self.brokerage_a, deal=self.deal_a2
        )

    def _bodies(self, user, **params):
        self.client.force_authenticate(user)
        resp = self.client.get("/api/rbacnotes/", params)
        self.assertEqual(resp.status_code, 200, resp.content)
        return sorted(row["body"] for row in resp.data["data"])

    def test_attname_filter_needs_the_field_rule(self):
        self.assertEqual(
            self._bodies(self.manager_a, deal__assigned_broker_id=self.under_a2.pk),
            ["n1", "n2"],
        )
        self.assertEqual(
            self._bodies(self.manager_a, deal__assigned_broker=self.under_a2.pk),
            ["n1", "n2"],
        )

    @override_settings(
        TURBODRF_ROLES=RBAC_ROLES, TURBODRF_SENSITIVE_FIELDS=["assigned_broker"]
    )
    def test_attname_filter_hits_the_deny_list(self):
        # No field rule on assigned_broker here: only the deny-list can refuse.
        self.assertEqual(
            self._bodies(self.manager_a, deal__assigned_broker=self.under_a2.pk),
            ["n1", "n2"],
        )
        self.assertEqual(
            self._bodies(self.manager_a, deal__assigned_broker_id=self.under_a2.pk),
            ["n1", "n2"],
        )
