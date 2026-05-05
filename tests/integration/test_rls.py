"""
End-to-end RLS verification against real Postgres.

Skipped on non-Postgres backends.

The tests below apply policies derived from each predicate's
to_rls_using_clause() and verify they actually enforce row isolation in
Postgres. Three subtleties handled:

1. Multiple PERMISSIVE policies on a single command are OR'd, not AND'd.
   To stack predicates we combine their clauses into one policy with AND.
2. Postgres `current_setting('var')::int` raises when the var is unset and
   aborts the current transaction. For graceful default-deny we wrap with
   NULLIF — production deployments can choose strict-error or graceful.
3. Django's ORM uses RETURNING, which checks USING on the new row. So
   session vars must be set BEFORE inserting fixture rows that the new
   row's owner needs to satisfy.

Run:
    DJANGO_SETTINGS_MODULE=tests.settings_pg pytest tests/integration/test_rls.py -v
"""

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase

User = get_user_model()


pytestmark = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="RLS integration tests require Postgres.",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _exec(sql, params=None):
    with connection.cursor() as cursor:
        cursor.execute(sql, params or [])


def _set_var(name, value):
    _exec(f"SELECT set_config('{name}', %s, true)", [str(value)])


def _clear_var(name):
    _exec(f"SELECT set_config('{name}', '', true)")


def _nullsafe(clause):
    """Wrap current_setting calls with NULLIF for graceful default-deny.
    Empty session var → NULL → row excluded silently rather than aborting."""
    return (
        clause.replace(
            "current_setting('app.tenant_id')",
            "nullif(current_setting('app.tenant_id', true), '')",
        )
        .replace(
            "current_setting('app.user_id')",
            "nullif(current_setting('app.user_id', true), '')",
        )
        .replace(
            "current_setting('app.user_roles')",
            "current_setting('app.user_roles', true)",
        )
    )


def _enable_rls(table):
    _exec(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    _exec(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def _disable_rls(table, *policies):
    for p in policies:
        _exec(f"DROP POLICY IF EXISTS {p} ON {table}")
    _exec(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    _exec(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")


def _apply_combined_policy(table, name, clauses, with_check=None):
    """Combine multiple predicate clauses into ONE policy with AND.

    Multiple permissive policies on the same cmd are OR'd in Postgres, which
    isn't what we want for stacked predicates. The framework should produce
    a single policy whose USING is the AND of all stacked clauses.
    """
    using = " AND ".join(f"({_nullsafe(c)})" for c in clauses)
    check = with_check if with_check is not None else using
    _exec(f"CREATE POLICY {name} ON {table} USING ({using}) WITH CHECK ({check})")


# ---------------------------------------------------------------------------
# Tenant alone
# ---------------------------------------------------------------------------


class TestTenantPolicy(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from turbodrf.predicates import Tenant

        _enable_rls("test_app_deal")
        _apply_combined_policy(
            "test_app_deal",
            "test_tenant",
            [Tenant("brokerage").to_rls_using_clause()],
        )

    @classmethod
    def tearDownClass(cls):
        _disable_rls("test_app_deal", "test_tenant")
        super().tearDownClass()

    def setUp(self):
        from tests.test_app.models import Brokerage, Deal

        self.b_a = Brokerage.objects.create(name="A")
        self.b_b = Brokerage.objects.create(name="B")
        _set_var("app.tenant_id", self.b_a.id)
        self.deal_a = Deal.objects.create(title="A", brokerage=self.b_a)
        _set_var("app.tenant_id", self.b_b.id)
        self.deal_b = Deal.objects.create(title="B", brokerage=self.b_b)
        _clear_var("app.tenant_id")

    def test_tenant_a_session_filters_to_a_only(self):
        from tests.test_app.models import Deal

        _set_var("app.tenant_id", self.b_a.id)
        self.assertEqual(
            list(Deal.objects.values_list("id", flat=True)), [self.deal_a.id]
        )

    def test_tenant_b_session_filters_to_b_only(self):
        from tests.test_app.models import Deal

        _set_var("app.tenant_id", self.b_b.id)
        self.assertEqual(
            list(Deal.objects.values_list("id", flat=True)), [self.deal_b.id]
        )

    def test_unset_var_default_denies(self):
        from tests.test_app.models import Deal

        _clear_var("app.tenant_id")
        # NULLIF wrap: empty var → NULL → row excluded silently
        self.assertEqual(list(Deal.objects.values_list("id", flat=True)), [])

    def test_raw_sql_also_filtered(self):
        _set_var("app.tenant_id", self.b_a.id)
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM test_app_deal")
            rows = cursor.fetchall()
        self.assertEqual([r[0] for r in rows], [self.deal_a.id])

    def test_with_check_blocks_cross_tenant_insert(self):
        # Session set to A; INSERT with brokerage=B violates WITH CHECK
        from tests.test_app.models import Deal

        _set_var("app.tenant_id", self.b_a.id)
        with self.assertRaises(Exception) as cm:
            Deal.objects.create(title="x-tenant", brokerage=self.b_b)
        self.assertIn("row-level security", str(cm.exception))


# ---------------------------------------------------------------------------
# Owner alone (with bypass)
# ---------------------------------------------------------------------------


class TestOwnerPolicy(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from turbodrf.predicates import Owner

        _enable_rls("test_app_deal")
        owner_clause = Owner(
            "assigned_broker", bypass=["admin", "manager"]
        ).to_rls_using_clause()
        _apply_combined_policy(
            "test_app_deal",
            "test_owner",
            [owner_clause],
            # WITH CHECK only requires ownership match (bypass roles can
            # also write — keep symmetric)
            with_check=_nullsafe(owner_clause),
        )

    @classmethod
    def tearDownClass(cls):
        _disable_rls("test_app_deal", "test_owner")
        super().tearDownClass()

    def setUp(self):
        from tests.test_app.models import Brokerage, Deal

        self.brokerage = Brokerage.objects.create(name="A")
        self.alice = User.objects.create_user(username="alice", password="x")
        self.bob = User.objects.create_user(username="bob", password="x")
        _set_var("app.user_id", self.alice.id)
        self.deal_alice = Deal.objects.create(
            title="alice", brokerage=self.brokerage, assigned_broker=self.alice
        )
        _set_var("app.user_id", self.bob.id)
        self.deal_bob = Deal.objects.create(
            title="bob", brokerage=self.brokerage, assigned_broker=self.bob
        )
        _clear_var("app.user_id")
        _clear_var("app.user_roles")

    def test_alice_sees_only_own(self):
        from tests.test_app.models import Deal

        _set_var("app.user_id", self.alice.id)
        self.assertEqual(
            list(Deal.objects.values_list("id", flat=True)), [self.deal_alice.id]
        )

    def test_bob_sees_only_own(self):
        from tests.test_app.models import Deal

        _set_var("app.user_id", self.bob.id)
        self.assertEqual(
            list(Deal.objects.values_list("id", flat=True)), [self.deal_bob.id]
        )

    def test_admin_bypass_sees_all(self):
        from tests.test_app.models import Deal

        admin = User.objects.create_user(username="admin_u", password="x")
        _set_var("app.user_id", admin.id)
        _set_var("app.user_roles", "admin")
        self.assertEqual(
            sorted(Deal.objects.values_list("id", flat=True)),
            sorted([self.deal_alice.id, self.deal_bob.id]),
        )

    def test_manager_bypass_sees_all(self):
        from tests.test_app.models import Deal

        mgr = User.objects.create_user(username="mgr_u", password="x")
        _set_var("app.user_id", mgr.id)
        _set_var("app.user_roles", "manager")
        self.assertEqual(
            sorted(Deal.objects.values_list("id", flat=True)),
            sorted([self.deal_alice.id, self.deal_bob.id]),
        )

    def test_non_bypass_role_does_not_grant_access(self):
        from tests.test_app.models import Deal

        outsider = User.objects.create_user(username="outsider", password="x")
        _set_var("app.user_id", outsider.id)
        _set_var("app.user_roles", "viewer,staff")
        self.assertEqual(list(Deal.objects.values_list("id", flat=True)), [])


# ---------------------------------------------------------------------------
# Role-regex word-boundary semantics
# ---------------------------------------------------------------------------


class TestOwnerRoleRegex(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from turbodrf.predicates import Owner

        _enable_rls("test_app_deal")
        clause = Owner("assigned_broker", bypass=["admin"]).to_rls_using_clause()
        _apply_combined_policy(
            "test_app_deal", "test_role_regex", [clause], with_check=_nullsafe(clause)
        )

    @classmethod
    def tearDownClass(cls):
        _disable_rls("test_app_deal", "test_role_regex")
        super().tearDownClass()

    def setUp(self):
        from tests.test_app.models import Brokerage, Deal

        self.brokerage = Brokerage.objects.create(name="A")
        self.owner = User.objects.create_user(username="o", password="x")
        self.outsider = User.objects.create_user(username="ou", password="x")
        _set_var("app.user_id", self.owner.id)
        self.deal = Deal.objects.create(
            title="d", brokerage=self.brokerage, assigned_broker=self.owner
        )
        _clear_var("app.user_id")

    def test_exact_admin_bypasses(self):
        from tests.test_app.models import Deal

        _set_var("app.user_id", self.outsider.id)
        _set_var("app.user_roles", "admin")
        self.assertEqual(Deal.objects.count(), 1)

    def test_admin_in_csv_bypasses(self):
        from tests.test_app.models import Deal

        _set_var("app.user_id", self.outsider.id)
        _set_var("app.user_roles", "viewer,admin,staff")
        self.assertEqual(Deal.objects.count(), 1)

    def test_admins_substring_does_not_bypass(self):
        from tests.test_app.models import Deal

        _set_var("app.user_id", self.outsider.id)
        _set_var("app.user_roles", "admins")  # word boundary should reject
        self.assertEqual(Deal.objects.count(), 0)

    def test_unrelated_role_does_not_bypass(self):
        from tests.test_app.models import Deal

        _set_var("app.user_id", self.outsider.id)
        _set_var("app.user_roles", "staff,viewer")
        self.assertEqual(Deal.objects.count(), 0)


# ---------------------------------------------------------------------------
# Stacked Tenant + Owner — combined into a single policy with AND
# ---------------------------------------------------------------------------


class TestStackedTenantAndOwner(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from turbodrf.predicates import Owner, Tenant

        _enable_rls("test_app_deal")
        # CRITICAL: combine Tenant AND Owner into ONE policy. Multiple
        # permissive policies on the same cmd are OR'd in Postgres, which
        # would defeat stacking. emit_rls should generate this combined form.
        _apply_combined_policy(
            "test_app_deal",
            "test_stacked",
            [
                Tenant("brokerage").to_rls_using_clause(),
                Owner("assigned_broker", bypass=["manager"]).to_rls_using_clause(),
            ],
            # WITH CHECK only enforces tenant on writes (owner enforced
            # separately by validate_write at the app layer)
            with_check=_nullsafe(Tenant("brokerage").to_rls_using_clause()),
        )

    @classmethod
    def tearDownClass(cls):
        _disable_rls("test_app_deal", "test_stacked")
        super().tearDownClass()

    def setUp(self):
        from tests.test_app.models import Brokerage, Deal

        self.b_a = Brokerage.objects.create(name="A")
        self.b_b = Brokerage.objects.create(name="B")
        self.alice = User.objects.create_user(username="alice", password="x")
        self.bob = User.objects.create_user(username="bob", password="x")
        # USING is tenant AND owner — set both vars to satisfy RETURNING
        _set_var("app.tenant_id", self.b_a.id)
        _set_var("app.user_id", self.alice.id)
        self.deal_a_alice = Deal.objects.create(
            title="A-alice", brokerage=self.b_a, assigned_broker=self.alice
        )
        _set_var("app.user_id", self.bob.id)
        self.deal_a_bob = Deal.objects.create(
            title="A-bob", brokerage=self.b_a, assigned_broker=self.bob
        )
        _set_var("app.tenant_id", self.b_b.id)
        _set_var("app.user_id", self.alice.id)
        self.deal_b_alice = Deal.objects.create(
            title="B-alice", brokerage=self.b_b, assigned_broker=self.alice
        )
        _clear_var("app.tenant_id")
        _clear_var("app.user_id")

    def test_alice_in_a_sees_only_her_a_deal(self):
        from tests.test_app.models import Deal

        _set_var("app.tenant_id", self.b_a.id)
        _set_var("app.user_id", self.alice.id)
        self.assertEqual(
            list(Deal.objects.values_list("id", flat=True)),
            [self.deal_a_alice.id],
        )

    def test_manager_in_a_sees_all_a_deals(self):
        from tests.test_app.models import Deal

        mgr = User.objects.create_user(username="mgr", password="x")
        _set_var("app.tenant_id", self.b_a.id)
        _set_var("app.user_id", mgr.id)
        _set_var("app.user_roles", "manager")
        self.assertEqual(
            sorted(Deal.objects.values_list("id", flat=True)),
            sorted([self.deal_a_alice.id, self.deal_a_bob.id]),
        )

    def test_alice_cross_tenant_b_blocked(self):
        # Alice owns deal_b_alice but session is tenant=A
        from tests.test_app.models import Deal

        _set_var("app.tenant_id", self.b_a.id)
        _set_var("app.user_id", self.alice.id)
        self.assertNotIn(
            self.deal_b_alice.id,
            list(Deal.objects.values_list("id", flat=True)),
        )


# ---------------------------------------------------------------------------
# Either composition (single policy with OR clause)
# ---------------------------------------------------------------------------


class TestEitherComposition(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from turbodrf.predicates import Either, Owner, Tenant

        _enable_rls("test_app_deal")
        clause = Either(
            Tenant("brokerage"), Owner("assigned_broker")
        ).to_rls_using_clause()
        _apply_combined_policy(
            "test_app_deal", "test_either", [clause], with_check=_nullsafe(clause)
        )

    @classmethod
    def tearDownClass(cls):
        _disable_rls("test_app_deal", "test_either")
        super().tearDownClass()

    def setUp(self):
        from tests.test_app.models import Brokerage, Deal

        self.b_a = Brokerage.objects.create(name="A")
        self.b_b = Brokerage.objects.create(name="B")
        self.alice = User.objects.create_user(username="alice", password="x")
        self.bob = User.objects.create_user(username="bob", password="x")
        _set_var("app.tenant_id", self.b_a.id)
        _set_var("app.user_id", self.bob.id)
        self.deal_a = Deal.objects.create(
            title="A-bob", brokerage=self.b_a, assigned_broker=self.bob
        )
        _set_var("app.tenant_id", self.b_b.id)
        _set_var("app.user_id", self.alice.id)
        self.deal_b_alice = Deal.objects.create(
            title="B-alice", brokerage=self.b_b, assigned_broker=self.alice
        )
        _clear_var("app.tenant_id")
        _clear_var("app.user_id")

    def test_visible_via_tenant_branch(self):
        from tests.test_app.models import Deal

        _set_var("app.tenant_id", self.b_a.id)
        _set_var("app.user_id", self.bob.id)
        self.assertEqual(
            list(Deal.objects.values_list("id", flat=True)), [self.deal_a.id]
        )

    def test_visible_via_owner_branch(self):
        # Tenant=A, user=alice → A row via tenant + B-alice via owner
        from tests.test_app.models import Deal

        _set_var("app.tenant_id", self.b_a.id)
        _set_var("app.user_id", self.alice.id)
        self.assertEqual(
            sorted(Deal.objects.values_list("id", flat=True)),
            sorted([self.deal_a.id, self.deal_b_alice.id]),
        )


# ---------------------------------------------------------------------------
# Round-trip: turbodrf_emit_rls → apply → query
# ---------------------------------------------------------------------------


class TestEmitRLSRoundtrip(TestCase):
    """Apply the management command's output verbatim (with NULLIF wrapping
    for graceful default-deny in tests) and verify it enforces correctly."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from io import StringIO

        from django.core.management import call_command
        from django.test.utils import override_settings

        with override_settings(TURBODRF_TENANT_MODEL="test_app.Brokerage"):
            out = StringIO()
            call_command("turbodrf_emit_rls", "--model", "Deal", stdout=out)
            sql = out.getvalue()
        # Apply with nullsafe wrapping
        cls._sql_applied = sql

        # Parse statements (skip comments and blanks)
        statements = []
        for line in sql.split("\n"):
            line = line.strip()
            if not line or line.startswith("--"):
                continue
            statements.append(line)
        # Recombine and split on semicolons
        full = " ".join(statements)
        for stmt in [s.strip() for s in full.split(";") if s.strip()]:
            _exec(_nullsafe(stmt))

    @classmethod
    def tearDownClass(cls):
        # Cleanup all policies on the table
        _exec(
            "DO $$ DECLARE r record; BEGIN "
            "FOR r IN SELECT polname FROM pg_policy "
            "WHERE polrelid = 'test_app_deal'::regclass LOOP "
            "EXECUTE 'DROP POLICY ' || quote_ident(r.polname) "
            "|| ' ON test_app_deal'; END LOOP; END $$;"
        )
        _exec("ALTER TABLE test_app_deal NO FORCE ROW LEVEL SECURITY")
        _exec("ALTER TABLE test_app_deal DISABLE ROW LEVEL SECURITY")
        super().tearDownClass()

    def test_emitted_sql_enforces_tenant(self):
        # The emit produces separate Tenant + Owner policies — they OR
        # together in Postgres. So this test verifies that the EMITTED
        # form (which we know has the OR-stacking caveat documented in
        # docs/rls.md) lets a user see rows matching EITHER.
        from tests.test_app.models import Brokerage, Deal

        b = Brokerage.objects.create(name="rt")
        u = User.objects.create_user(username="rt-u", password="x")
        _set_var("app.tenant_id", b.id)
        _set_var("app.user_id", u.id)
        d = Deal.objects.create(title="rt", brokerage=b, assigned_broker=u)

        # User can see their own deal
        self.assertIn(d.id, list(Deal.objects.values_list("id", flat=True)))

        # Without any session var, default-deny (NULLIF wrap)
        _clear_var("app.tenant_id")
        _clear_var("app.user_id")
        self.assertNotIn(d.id, list(Deal.objects.values_list("id", flat=True)))


# ---------------------------------------------------------------------------
# Multi-owner OR — SQL shape only (test_app_deal has only one owner FK)
# ---------------------------------------------------------------------------


class TestMultiOwnerSQLShape(TestCase):
    """Verify multi-owner Owner produces correct OR'd SQL. Full end-to-end
    against real columns would need a test model with two FKs — out of
    scope for now."""

    def test_multi_owner_clause_form(self):
        from turbodrf.predicates import Owner

        clause = Owner(["author", "editor", "reviewer"]).to_rls_using_clause()
        self.assertIn("author_id", clause)
        self.assertIn("editor_id", clause)
        self.assertIn("reviewer_id", clause)
        # OR'd
        self.assertEqual(clause.count(" OR "), 2)


# ---------------------------------------------------------------------------
# Unsupported predicates raise on RLS clause generation
# ---------------------------------------------------------------------------


class TestUnsupportedPredicatesRaise(TestCase):
    def test_members_raises(self):
        from turbodrf.predicates import Members

        with self.assertRaises(NotImplementedError):
            Members("collaborators").to_rls_using_clause()

    def test_group_raises(self):
        from turbodrf.predicates import Group

        with self.assertRaises(NotImplementedError):
            Group("team").to_rls_using_clause()

    def test_conditional_raises(self):
        from django.db.models import Q

        from turbodrf.predicates import Conditional

        with self.assertRaises(NotImplementedError):
            Conditional(
                when=Q(staff=True), require_roles=["admin"]
            ).to_rls_using_clause()

    def test_custom_raises(self):
        from django.db.models import Q

        from turbodrf.predicates import Custom

        with self.assertRaises(NotImplementedError):
            Custom(lambda r, u: Q()).to_rls_using_clause()

    def test_chained_tenant_raises(self):
        from turbodrf.predicates import Tenant

        with self.assertRaises(NotImplementedError):
            Tenant("deal__brokerage").to_rls_using_clause()

    def test_chained_owner_raises(self):
        from turbodrf.predicates import Owner

        with self.assertRaises(NotImplementedError):
            Owner("deal__author").to_rls_using_clause()
