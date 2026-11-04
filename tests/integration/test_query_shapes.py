"""The 0.8.4 query shapes: a predicate's to-many lookups become ``pk__in`` the
rows they match (views.without_to_many_joins), and a filter or search through
a single-valued relation checks the target's scope on the joined row
(validation._scope_on_joined_row). Both must match exactly the rows the
older shapes did."""

from decimal import Decimal

from django.db.models import F, Q
from django.test import TestCase

from tests.test_app.models import ParityAllTypes, ParityTag
from turbodrf.views import (
    Authorization,
    RowsMatching,
    _q_spans_multivalued,
    without_to_many_joins,
)


class TestWithoutToManyJoins(TestCase):
    """r1 has tags (a, 2) and (b, 1); r2 has (a, 1); r3 has none."""

    @classmethod
    def setUpTestData(cls):
        def tag(name, weight):
            return ParityTag.objects.create(name=name, weight=Decimal(weight))

        cls.r1 = ParityAllTypes.objects.create(char="r1")
        cls.r2 = ParityAllTypes.objects.create(char="r2")
        cls.r3 = ParityAllTypes.objects.create(char="r3")
        cls.r1.tags.set([tag("a", 2), tag("b", 1)])
        cls.r2.tags.set([tag("a", 1)])

    def _rows(self, q):
        """The rows ``q`` scopes to, and whether any repeats."""
        pks = list(
            Authorization(None, q)
            .scope(ParityAllTypes.objects.all())
            .values_list("char", flat=True)
        )
        return sorted(pks), len(pks) != len(set(pks))

    def _wrapped(self, q):
        """What the whole-predicate pk__in wrap matches (the 0.8.3 shape)."""
        rows = ParityAllTypes.objects.filter(
            pk__in=ParityAllTypes.objects.filter(q).values("pk")
        )
        return sorted(rows.values_list("char", flat=True))

    def assertSameRows(self, q):
        rows, repeats = self._rows(q)
        self.assertFalse(repeats)
        self.assertEqual(rows, self._wrapped(q))
        return rows

    def test_a_to_many_lookup_becomes_rows_matching(self):
        flat = without_to_many_joins(ParityAllTypes, Q(tags__name="a"))
        self.assertIsInstance(flat.children[0][1], RowsMatching)
        self.assertEqual(self.assertSameRows(Q(tags__name="a")), ["r1", "r2"])

    def test_or_with_a_column(self):
        q = Q(tags__name="b") | Q(char="r3")
        self.assertIsNotNone(without_to_many_joins(ParityAllTypes, q))
        self.assertEqual(self.assertSameRows(q), ["r1", "r3"])

    def test_two_lookups_through_one_relation_keep_one_join(self):
        # One tag must be both "a" and weight 1: only r2. Rewriting each
        # lookup on its own would also match r1 (a tag "a", another weight 1).
        q = Q(tags__name="a", tags__weight=1)
        self.assertEqual(without_to_many_joins(ParityAllTypes, q), q)  # left as is
        self.assertEqual(self.assertSameRows(q), ["r2"])

    def test_negated_lookups_sharing_a_relation_are_each_their_own(self):
        # Under a NOT, Django matches each lookup on its own: r1 has a tag "a"
        # and a tag of weight 1 (different tags), so it's excluded too.
        q = ~Q(tags__name="a", tags__weight=1)
        flat = without_to_many_joins(ParityAllTypes, q)
        self.assertFalse(_q_spans_multivalued(ParityAllTypes, flat))
        self.assertEqual(self.assertSameRows(q), ["r3"])

    def test_negated(self):
        q = ~Q(tags__name="a")
        self.assertIsNotNone(without_to_many_joins(ParityAllTypes, q))
        self.assertEqual(self.assertSameRows(q), ["r3"])

    def test_and_with_a_column(self):
        q = Q(tags__name="a") & Q(char="r1")
        self.assertEqual(self.assertSameRows(q), ["r1"])

    def test_an_f_inside_a_list_isnt_rewritten(self):
        # The F reads the tag the first lookup joined: splitting them apart
        # would read a different row.
        # (Django 4.2 can't run this filter at all, so only the refusal is
        # checked here.)
        q = Q(tags__name="b") & Q(char_null__in=[F("tags__name")])
        self.assertIsNone(without_to_many_joins(ParityAllTypes, q))

    def test_xor_isnt_rewritten(self):
        q = Q(tags__name="a") ^ Q(char="r1")
        self.assertIsNone(without_to_many_joins(ParityAllTypes, q))
        self.assertSameRows(q)

    def test_an_expression_through_the_relation_isnt_rewritten(self):
        q = Q(tags__weight__gte=F("tags__weight"))
        self.assertIsNone(without_to_many_joins(ParityAllTypes, q))
        self.assertSameRows(q)


class TestPrefixed(TestCase):
    def test_moves_lookups_and_f(self):
        from turbodrf.views import prefixed

        moved = prefixed(Q(name="a") | ~Q(weight__gt=F("weight")), "target")
        self.assertEqual(
            str(moved),
            str(Q(target__name="a") | ~Q(target__weight__gt=F("target__weight"))),
        )

    def test_refuses_what_it_cant_move(self):
        from django.db.models import Exists, OuterRef

        from turbodrf.views import prefixed

        for q in (
            Q(amount__in=[F("amount")]),
            Q(amount__range=(F("amount"), 5)),
            Q(name="a") ^ Q(weight=1),
            Q(Exists(ParityTag.objects.filter(pk=OuterRef("pk")))),
        ):
            self.assertIsNone(prefixed(q, "target"), q)


class TestScopeOnJoinedRow(TestCase):
    def test_a_tenant_rule_is_moved_onto_the_joined_row(self):
        from unittest.mock import Mock

        from django.contrib.auth import get_user_model

        import tests.urls  # noqa: F401  (the router registers tenancy)
        from tests.test_app.apps import set_test_brokerage
        from tests.test_app.models import Brokerage, HoldingEvent
        from turbodrf.validation import build_traversal_scope_q

        user = get_user_model().objects.create_user("u")
        set_test_brokerage(user, Brokerage.objects.create(name="A"))
        request = Mock()
        request.user = user
        q = build_traversal_scope_q(HoldingEvent, "holding__name", request)
        self.assertIn("holding__brokerage", str(q))
        self.assertNotIn("holding__in", str(q))


class TestPermissionCostsPerRequest(TestCase):
    def test_static_digest_is_reused_and_still_sees_changes(self):
        from turbodrf.backends import _static_digest

        roles = {"viewer": ["a.b.read"], "editor": ["a.b.read", "a.b.update"]}
        first = _static_digest(roles)
        self.assertEqual(_static_digest(roles), first)
        roles["editor"].append("a.b.delete")  # in place, as a test might
        second = _static_digest(roles)
        self.assertNotEqual(second, first)
        roles["viewer"] = ["a.b.update"]  # same length, new list
        third = _static_digest(roles)
        self.assertNotEqual(third, second)
        roles["viewer"][0] = "a.b.create"  # same list, same length
        fourth = _static_digest(roles)
        self.assertNotEqual(fourth, third)
        del roles["viewer"]
        roles["viewer"] = ["a.b.delete"]  # may reuse the old list's id
        self.assertNotEqual(_static_digest(roles), fourth)
        self.assertEqual(_static_digest(dict(roles)), _static_digest(roles))

    def test_database_mode_reads_roles_once_per_request(self):
        from django.contrib.auth import get_user_model
        from django.core.cache import cache
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from rest_framework.test import APIClient

        from turbodrf.models import RolePermission, TurboDRFRole, UserRole

        cache.clear()
        user = get_user_model().objects.create_user("db")
        role = TurboDRFRole.objects.create(name="reader")
        RolePermission.objects.create(
            role=role, app_label="test_app", model_name="samplemodel", action="read"
        )
        UserRole.objects.create(user=user, role=role)
        client = APIClient()
        client.force_authenticate(user)
        with self.settings(TURBODRF_PERMISSION_MODE="database"):
            self.assertEqual(client.get("/api/samplemodels/").status_code, 200)
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(client.get("/api/samplemodels/").status_code, 200)
        role_queries = [q for q in ctx.captured_queries if "turbodrf_" in q["sql"]]
        self.assertEqual(len(role_queries), 1, [q["sql"] for q in role_queries])


class TestEitherWithAnUnrestrictedBranch(TestCase):
    def test_a_branch_with_no_condition_lifts_the_either(self):
        # Q(Q()) holds no condition; OR'ed with another branch, Django drops
        # it and the Either became the other branch alone.
        from unittest.mock import Mock

        from django.contrib.auth.models import User

        from turbodrf.predicates import Custom, Either, Owner

        either = Either(
            Custom(lambda r, roles: Q(Q()), write_validator=lambda d, i, r: []),
            Owner("assigned_broker"),
        )
        request = Mock(user=User(pk=1, username="u"))
        self.assertEqual(either.q(request, set()), Q())
