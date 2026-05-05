"""
Unit tests for RLS SQL generation. These verify the shape of generated SQL
without executing it — they run on any backend (no Postgres required).

The end-to-end Postgres tests live in tests/integration/test_rls.py and are
skipped on non-Postgres backends.
"""

from django.db.models import Q
from django.test import TestCase

from turbodrf.predicates import (
    Conditional,
    Custom,
    Either,
    Group,
    Members,
    Owner,
    Tenant,
)


class TestTenantRLS(TestCase):
    def test_simple_field(self):
        clause = Tenant("brokerage").to_rls_using_clause()
        self.assertEqual(clause, "brokerage_id = current_setting('app.tenant_id')::int")

    def test_field_already_with_id_suffix(self):
        clause = Tenant("brokerage_id").to_rls_using_clause()
        self.assertEqual(clause, "brokerage_id = current_setting('app.tenant_id')::int")

    def test_chained_path_raises(self):
        with self.assertRaises(NotImplementedError):
            Tenant("deal__brokerage").to_rls_using_clause()

    def test_full_policy_format(self):
        sql = Tenant("brokerage").to_rls_policy("deal_table")
        self.assertIn("CREATE POLICY", sql)
        self.assertIn("ON deal_table", sql)
        self.assertIn("brokerage_id = current_setting", sql)


class TestOwnerRLS(TestCase):
    def test_simple_owner(self):
        clause = Owner("assigned_to").to_rls_using_clause()
        self.assertEqual(clause, "assigned_to_id = current_setting('app.user_id')::int")

    def test_owner_with_bypass(self):
        clause = Owner("assigned_to", bypass=["admin", "manager"]).to_rls_using_clause()
        self.assertIn("assigned_to_id = current_setting('app.user_id')::int", clause)
        self.assertIn("current_setting('app.user_roles')", clause)
        self.assertIn("admin|manager", clause)

    def test_multi_owner_or(self):
        clause = Owner(["author", "editor"]).to_rls_using_clause()
        self.assertIn("author_id = current_setting('app.user_id')::int", clause)
        self.assertIn("editor_id = current_setting('app.user_id')::int", clause)
        self.assertIn(" OR ", clause)

    def test_chained_owner_raises(self):
        with self.assertRaises(NotImplementedError):
            Owner("deal__author").to_rls_using_clause()


class TestEitherRLS(TestCase):
    def test_or_of_two_tenants(self):
        clause = Either(Tenant("side_a"), Tenant("side_b")).to_rls_using_clause()
        self.assertIn("side_a_id = current_setting", clause)
        self.assertIn("side_b_id = current_setting", clause)
        self.assertIn(" OR ", clause)

    def test_or_with_owner(self):
        clause = Either(Owner("author"), Owner("editor")).to_rls_using_clause()
        self.assertIn("author_id = current_setting('app.user_id')", clause)
        self.assertIn("editor_id = current_setting('app.user_id')", clause)


class TestUnsupportedPredicates(TestCase):
    def test_members_raises(self):
        with self.assertRaises(NotImplementedError):
            Members("collaborators").to_rls_using_clause()

    def test_group_raises(self):
        with self.assertRaises(NotImplementedError):
            Group("team").to_rls_using_clause()

    def test_conditional_raises(self):
        with self.assertRaises(NotImplementedError):
            Conditional(
                when=Q(public=True), require_roles=["admin"]
            ).to_rls_using_clause()

    def test_custom_raises(self):
        with self.assertRaises(NotImplementedError):
            Custom(lambda r, u: Q()).to_rls_using_clause()


class TestEmitRLSCommand(TestCase):
    """Smoke-test the management command output shape."""

    def test_emit_includes_alter_table(self):
        from io import StringIO

        from django.core.management import call_command
        from django.test.utils import override_settings

        with override_settings(TURBODRF_TENANT_MODEL="test_app.Brokerage"):
            out = StringIO()
            call_command("turbodrf_emit_rls", "--model", "Deal", stdout=out)
            output = out.getvalue()

        self.assertIn("ALTER TABLE test_app_deal ENABLE ROW LEVEL SECURITY", output)
        self.assertIn("CREATE POLICY", output)
        self.assertIn("brokerage_id", output)
        self.assertIn("assigned_broker_id", output)

    def test_emit_skips_chained_tenants_with_comment(self):
        from io import StringIO

        from django.core.management import call_command
        from django.test.utils import override_settings

        with override_settings(TURBODRF_TENANT_MODEL="test_app.Brokerage"):
            out = StringIO()
            call_command("turbodrf_emit_rls", "--model", "Transaction", stdout=out)
            output = out.getvalue()

        # Transaction has chained tenant_field — should be commented as skipped
        self.assertIn("ALTER TABLE test_app_transaction", output)
        self.assertIn("SKIPPED", output)
